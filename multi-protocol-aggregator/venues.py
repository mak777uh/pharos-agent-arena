"""Yield venues on Pharos mainnet behind one adapter interface: Avalon (Aave-V3 fork), Morpho Blue markets, ERC-4626 vaults.

Every read goes through the hosted Pharos Chain MCP (https://mcp.pharos.xyz/mcp, read-only) and is pinned to one
block. Each adapter knows how to:
  load()                 read rate (APY), size, exit liquidity, deposit headroom, fees; record sources
  position(wallet)       the wallet's balance in the venue (underlying units, plus shares where relevant)
  apy_after(amount)      supply APY after an extra deposit of `amount` (dilution estimate, see README)
  fees(amount)           entry / exit fee in bps, measured with previewDeposit/previewRedeem where they exist
  build_deposit / build_withdraw   UNSIGNED transaction dicts {chainId, from, to, data, value} for the user to sign

Nothing here holds a key, signs, or broadcasts: the code never calls send_raw_transaction.
"""
from __future__ import annotations

import json
import math
import os
import statistics
import time
from decimal import Decimal, getcontext

from Crypto.Hash import keccak

from pharos_mcp import MCPError, PharosMCP, check_address

getcontext().prec = 60
CHAIN_ID = 1672
MORPHO = "0x18573fa18fd17ddfd790b4a5b5b2977aad3b4efb"
AVALON_AUSDC = "0xe2ec2f3933ad2f83de9a9dda844558354384e87d"   # "Avalon USDC" aToken; POOL() -> lending pool
PROS_USD_FEED = "0x371ed36aee5bffbed1a2aa9b7ded7f4eb30f5466"  # Chainlink PROS/USD
TOKENS = {  # Pharos token registry (docs.pharos.xyz/getting-started/token-registry)
    "WPROS": "0x52c48d4213107b20bc583832b0d951fb9ca8f0b0",
    "USDC": "0xc879c018db60520f4355c26ed1a6d572cdac1815",
    "WETH": "0x1f4b7011ee3d53969bb67f59428a9ec0477856e9",
}
WAD, RAY, YEAR = 10 ** 18, 10 ** 27, 365 * 86400
UINT_MAX = 2 ** 256 - 1
HERE = os.path.dirname(os.path.abspath(__file__))
# Used only when neither estimate_gas nor recent receipts give a figure (labelled "assumed" in the output).
DEFAULT_GAS = {"approve": 60_000, "deposit": 300_000, "withdraw": 300_000}


def sel(sig: str) -> str:
    return keccak.new(digest_bits=256, data=sig.encode()).hexdigest()[:8]


def words(h: str) -> list[int]:
    h = h[2:] if h.startswith("0x") else h
    return [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]


def h2i(v) -> int:
    if v is None:
        return 0
    if isinstance(v, int):
        return v
    s = str(v)
    return int(s, 16) if s.startswith("0x") else int(s)


def w(x: int | str) -> str:
    """ABI word: int or address."""
    if isinstance(x, str):
        return x[2:].lower().rjust(64, "0")
    return format(x, "064x")


def amt(raw: int, dec: int) -> Decimal:
    return Decimal(raw) / (Decimal(10) ** dec)


def short(a: str) -> str:
    return a[:6] + "…" + a[-4:]


S = {k: sel(k) for k in [
    "balanceOf(address)", "symbol()", "decimals()", "asset()", "name()", "totalAssets()", "totalSupply()",
    "convertToAssets(uint256)", "convertToShares(uint256)", "previewDeposit(uint256)", "previewRedeem(uint256)",
    "performanceFee()", "managementFee()", "deposit(uint256,address)", "redeem(uint256,address,address)",
    "approve(address,uint256)", "allowance(address,address)", "latestRoundData()",
    "POOL()", "getReservesList()", "getReserveData(address)", "supply(address,uint256,address,uint16)",
    "withdraw(address,uint256,address)", "idToMarketParams(bytes32)", "market(bytes32)", "position(bytes32,address)",
    "supply((address,address,address,address,uint256),uint256,uint256,address,bytes)",
    "withdraw((address,address,address,address,uint256),uint256,uint256,address,address)"]}
S_RATE = sel("borrowRateView((address,address,address,address,uint256),(uint128,uint128,uint128,uint128,uint128,uint128))")


class VenueUnavailable(RuntimeError):
    pass


class Chain:
    """One block, one MCP session, small caches. `fail` = protocol names to treat as unreachable (outage drill)."""

    def __init__(self, mcp: PharosMCP, explorer=None, fail: set[str] | None = None):
        self.m, self.ex, self.fail = mcp, explorer, set(fail or ())
        self.block = mcp.block_number()
        self.ts = h2i(mcp.call_tool("get_block", {"block": self.block})["timestamp"])
        self._tok: dict = {}
        self._gas_obs: dict = {}
        self.gas_price = h2i(mcp.call_tool("get_gas_price")["gasPriceWei"])
        r = words(self.call(PROS_USD_FEED, "latestRoundData()"))
        self.pros_usd = amt(r[1], h2i(self.call(PROS_USD_FEED, "decimals()")))
        self.prices = {TOKENS["USDC"]: (Decimal(1), "USDC counted as $1.00 (assumption)"),
                       TOKENS["WPROS"]: (self.pros_usd, f"Chainlink PROS/USD {PROS_USD_FEED} latestRoundData() @ block {self.block:,}")}

    def call(self, to: str, fn: str, args: str = "", block: int | None = None, frm: str | None = None) -> str:
        data = "0x" + (S.get(fn) or fn) + args
        last = None
        for attempt in range(3):
            try:
                if frm:
                    out = self.m.call_tool("eth_call", {"transaction": {"from": frm, "to": to, "data": data},
                                                        "block": self.block if block is None else block})
                    return out["result"] if isinstance(out, dict) else out
                return self.m.eth_call(to, data, self.block if block is None else block)
            except MCPError as e:
                last = e
                if "revert" in str(e).lower() or "execution" in str(e).lower():
                    raise
                time.sleep(1 + attempt)
        raise last

    def try_call(self, to, fn, args="", block=None, frm=None):
        try:
            out = self.call(to, fn, args, block, frm)
            return out if out and out != "0x" else None
        except MCPError:
            return None

    def token(self, a: str) -> tuple[str, int]:
        if a not in self._tok:
            dec = self.try_call(a, "decimals()")
            if dec is None:
                raise VenueUnavailable(f"token {a} has no decimals()")
            sym = next((k for k, v in TOKENS.items() if v == a), None) or self.string(a, "symbol()") or short(a)
            self._tok[a] = (sym, h2i(dec))
        return self._tok[a]

    def string(self, a: str, fn: str) -> str | None:
        raw = self.try_call(a, fn)
        if not raw:
            return None
        b = bytes.fromhex(raw[2:])
        try:
            if len(b) >= 96:
                return b[64:64 + int.from_bytes(b[32:64], "big")].decode("utf-8", "replace") or None
            return b.rstrip(b"\0").decode() or None
        except UnicodeDecodeError:
            return None

    def block_at(self, t: int) -> int:
        """Last block with timestamp <= t (rate estimate + narrow binary search)."""
        guess = max(1, self.block - int((self.ts - t) / 1.04))
        lo, hi = max(1, guess - 20_000), min(self.block, guess + 20_000)
        if h2i(self.m.call_tool("get_block", {"block": lo})["timestamp"]) > t:
            lo = 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if h2i(self.m.call_tool("get_block", {"block": mid})["timestamp"]) <= t:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def observed_gas(self, contract: str, method_id: str) -> tuple[int, str] | None:
        """Median gasUsed of recent successful txs calling `method_id` on `contract` (public explorer index)."""
        key = (contract, method_id)
        if key not in self._gas_obs:
            self._gas_obs[key] = None
            if self.ex is not None:
                try:
                    _, txs = self.ex.transactions(contract, 300)
                    g = [int(t["receipt_gas_used"]) for t in txs if t.get("method_id") == method_id
                         and t.get("receipt_status") == 1 and t.get("receipt_gas_used")]
                    if g:
                        self._gas_obs[key] = (int(statistics.median(g[:50])),
                                              f"median gasUsed of {min(len(g), 50)} recent {method_id} txs to {short(contract)} (explorer)")
                except Exception:
                    pass
        return self._gas_obs[key]

    def estimate(self, tx: dict) -> int | None:
        try:
            out = self.m.call_tool("estimate_gas", {"transaction": {"from": tx["from"], "to": tx["to"], "data": tx["data"]}})
            v = out.get("gas", out.get("gasEstimate", out.get("result"))) if isinstance(out, dict) else out
            return h2i(v)
        except (MCPError, TypeError, ValueError):
            return None

    def usd(self, token: str, amount: Decimal) -> Decimal | None:
        p = self.prices.get(token)
        return amount * p[0] if p else None


class Venue:
    protocol = "?"
    kind = "?"

    def __init__(self, ch: Chain, asset: str):
        self.ch, self.asset = ch, asset
        self.sym, self.dec = ch.token(asset)
        self.apy: Decimal | None = None
        self.apy_basis = ""
        self.tvl = Decimal(0)             # underlying units supplied in the venue
        self.liquidity: Decimal | None = None   # underlying that can leave right now
        self.headroom: Decimal | None = None    # underlying that can still be deposited (None = no cap known)
        self.src: list[str] = []
        self.notes: list[str] = []
        self.spender = ""
        self.deposit_method = self.withdraw_method = ""
        self.target = ""                  # contract the deposit/withdraw tx goes to

    # ---- identity
    @property
    def key(self) -> str:
        return f"{self.protocol}:{self.label}"

    def guard(self):
        if self.protocol.split()[0].lower() in self.ch.fail:
            raise VenueUnavailable(f"{self.protocol} marked unavailable (--simulate-outage drill)")

    def s(self, contract: str, fn: str, block: int | None = None) -> str:
        line = f"{contract} {fn} @ block {(block or self.ch.block):,}"
        if line not in self.src:
            self.src.append(line)
        return line

    # ---- model
    def apy_after(self, add: Decimal) -> Decimal | None:
        """Pool-style venues: supply APY = borrow APY x utilisation x (1 - fee); new money lowers utilisation.
        Borrow rate is held constant, which slightly overstates the post-deposit APY (the IRM would lower it)."""
        if self.apy is None or self.tvl + add <= 0:
            return self.apy
        return self.apy * self.tvl / (self.tvl + add)

    def fees(self, amount: Decimal) -> tuple[Decimal, Decimal, str]:
        return Decimal(0), Decimal(0), "no deposit/withdraw fee in the protocol"

    def gas(self, action: str, tx: dict | None = None) -> tuple[int, str]:
        if tx is not None:
            g = self.ch.estimate(tx)
            if g:
                return g, "estimate_gas (simulated from the wallet)"
        obs = self.ch.observed_gas(self.target, "0x" + (self.deposit_method if action == "deposit" else self.withdraw_method))
        if obs:
            return obs
        return DEFAULT_GAS[action], "assumed default (no estimate / receipts available)"

    def tx(self, frm: str, to: str, data: str, what: str) -> dict:
        return {"chainId": CHAIN_ID, "from": frm, "to": to, "value": "0x0", "data": data, "description": what}

    def approve_tx(self, frm: str, raw: int) -> dict:
        return self.tx(frm, self.asset, "0x" + S["approve(address,uint256)"] + w(self.spender) + w(raw),
                       f"approve {amt(raw, self.dec)} {self.sym} to {self.protocol} {short(self.spender)}")

    def summary(self) -> dict:
        return {"key": self.key, "protocol": self.protocol, "kind": self.kind, "label": self.label, "address": self.address,
                "asset": self.sym, "apy": None if self.apy is None else float(self.apy), "apy_basis": self.apy_basis,
                "tvl": str(self.tvl), "liquidity": None if self.liquidity is None else str(self.liquidity),
                "headroom": None if self.headroom is None else str(self.headroom), "notes": self.notes, "sources": self.src}


class AvalonReserve(Venue):
    protocol, kind = "Avalon", "lending pool (Aave V3 fork)"

    def __init__(self, ch, asset, pool, rd):
        super().__init__(ch, asset)
        self.pool, self.rd = pool, rd
        self.a_token = "0x%040x" % rd[8]
        self.label, self.address = f"{self.sym} reserve", pool
        self.spender = self.target = pool
        self.deposit_method, self.withdraw_method = S["supply(address,uint256,address,uint16)"], S["withdraw(address,uint256,address)"]

    def load(self):
        self.guard()
        cfg = self.rd[0]
        active, frozen, paused = (cfg >> 56) & 1, (cfg >> 57) & 1, (cfg >> 60) & 1
        rf = Decimal((cfg >> 64) & 0xFFFF) / 10_000
        cap = (cfg >> 116) & ((1 << 36) - 1)
        self.s(self.pool, f"getReserveData({self.sym})")
        apr = Decimal(self.rd[2]) / RAY
        self.apy = Decimal(math.expm1(float(apr)))
        self.apy_basis = "current liquidity rate (getReserveData.currentLiquidityRate, APR→APY)"
        self.tvl = amt(h2i(self.ch.call(self.a_token, "totalSupply()")), self.dec)
        self.s(self.a_token, "totalSupply()")
        self.liquidity = amt(h2i(self.ch.call(self.asset, "balanceOf(address)", w(self.a_token))), self.dec)
        self.s(self.asset, "balanceOf(aToken)")
        self.headroom = max(Decimal(cap) - self.tvl, Decimal(0)) if cap else None
        self.notes.append(f"supply cap {cap:,} {self.sym}" if cap else "no supply cap")
        self.notes.append(f"reserve factor {rf * 100:.0f}% (already inside the rate)")
        if not active or frozen or paused:
            self.headroom = Decimal(0)
            self.notes.append("reserve inactive/frozen/paused: no deposits")
        return self

    def position(self, wallet):
        raw = h2i(self.ch.call(self.a_token, "balanceOf(address)", w(wallet)))
        self.s(self.a_token, "balanceOf(wallet)")
        return amt(raw, self.dec), raw

    def build_deposit(self, wallet, raw):
        return [self.approve_tx(wallet, raw),
                self.tx(wallet, self.pool, "0x" + self.deposit_method + w(self.asset) + w(raw) + w(wallet) + w(0),
                        f"Avalon supply({self.sym}, {amt(raw, self.dec)}, onBehalfOf=you, referral=0)")]

    def build_withdraw(self, wallet, raw, everything=False):
        n = UINT_MAX if everything else raw
        return [self.tx(wallet, self.pool, "0x" + self.withdraw_method + w(self.asset) + w(n) + w(wallet),
                        f"Avalon withdraw({self.sym}, {'MAX' if everything else amt(raw, self.dec)}, to=you)")]


class MorphoMarket(Venue):
    protocol, kind = "Morpho Blue", "isolated lending market"

    def __init__(self, ch, asset, mid, params, name):
        super().__init__(ch, asset)
        self.mid, self.p = mid, params
        self.label, self.address = f"{name} market {mid[:10]}…", MORPHO
        self.spender = self.target = MORPHO
        self.deposit_method = S["supply((address,address,address,address,uint256),uint256,uint256,address,bytes)"]
        self.withdraw_method = S["withdraw((address,address,address,address,uint256),uint256,uint256,address,address)"]

    def load(self):
        self.guard()
        mk = words(self.ch.call(MORPHO, "market(bytes32)", self.mid[2:]))
        self.mk = mk
        self.s(MORPHO, f"market({short(self.mid)})")
        irm = "0x%040x" % self.p[3]
        rate = 0
        if self.p[3] and mk[2]:
            data = "".join(format(x, "064x") for x in self.p[:5]) + "".join(format(x, "064x") for x in mk[:6])
            rate = h2i(self.ch.call(irm, S_RATE, data))
            self.s(irm, "borrowRateView()")
        self.borrow_apy = Decimal(math.expm1(rate * YEAR / WAD))
        util = Decimal(mk[2]) / Decimal(mk[0]) if mk[0] else Decimal(0)
        self.fee = Decimal(mk[5]) / WAD
        self.apy = self.borrow_apy * util * (1 - self.fee)
        self.apy_basis = "borrow APY (IRM borrowRateView) × utilisation × (1 − market fee)"
        self.tvl = amt(mk[0], self.dec)
        self.borrowed = amt(mk[2], self.dec)
        self.liquidity = amt(mk[0] - mk[2], self.dec)
        self.headroom = None
        col = "0x%040x" % self.p[1]
        self.notes.append(f"collateral {self.ch.token(col)[0] if self.p[1] else 'none'}, LLTV {Decimal(self.p[4]) / WAD * 100:.1f}%, "
                          f"utilisation {util * 100:.1f}%, market fee {self.fee * 100:.0f}%")
        return self

    def apy_after(self, add):
        if self.apy is None or self.tvl + add <= 0:
            return self.apy
        return self.borrow_apy * self.borrowed / (self.tvl + add) * (1 - self.fee)

    def mp(self) -> str:
        return "".join(format(x, "064x") for x in self.p[:5])

    def position(self, wallet):
        pos = words(self.ch.call(MORPHO, "position(bytes32,address)", self.mid[2:] + w(wallet)))
        self.s(MORPHO, f"position({short(self.mid)}, wallet)")
        shares = pos[0]
        raw = shares * (self.mk[0] + 1) // (self.mk[1] + 10 ** 6) if shares else 0   # SharesMathLib, round down
        self._shares = shares
        return amt(raw, self.dec), raw

    def build_deposit(self, wallet, raw):
        data = "0x" + self.deposit_method + self.mp() + w(raw) + w(0) + w(wallet) + w(9 * 32) + w(0)
        return [self.approve_tx(wallet, raw),
                self.tx(wallet, MORPHO, data, f"Morpho supply(market {short(self.mid)}, assets={amt(raw, self.dec)}, onBehalf=you)")]

    def build_withdraw(self, wallet, raw, everything=False):
        if everything:
            self.position(wallet)
            a, sh = 0, self._shares
        else:
            a, sh = raw, 0
        data = "0x" + self.withdraw_method + self.mp() + w(a) + w(sh) + w(wallet) + w(wallet)
        what = f"all shares ({sh})" if everything else f"assets={amt(raw, self.dec)}"
        return [self.tx(wallet, MORPHO, data, f"Morpho withdraw(market {short(self.mid)}, {what}, to=you)")]


class Erc4626Vault(Venue):
    protocol, kind = "ERC-4626 vault", "curated vault"

    def __init__(self, ch, asset, vault):
        super().__init__(ch, asset)
        self.vault = self.address = self.spender = self.target = vault
        self.label = ch.string(vault, "name()") or short(vault)
        self.share_sym, self.share_dec = ch.token(vault)
        self.deposit_method, self.withdraw_method = S["deposit(uint256,address)"], S["redeem(uint256,address,address)"]

    def load(self, window_days: int = 7):
        self.guard()
        ta = h2i(self.ch.call(self.vault, "totalAssets()"))
        self.s(self.vault, "totalAssets()")
        self.tvl = amt(ta, self.dec)
        one = w(10 ** self.share_dec)
        now = h2i(self.ch.call(self.vault, "convertToAssets(uint256)", one))
        self.s(self.vault, "convertToAssets(1 share)")
        then_block = self.ch.block_at(self.ch.ts - window_days * 86400)
        old = self.ch.try_call(self.vault, "convertToAssets(uint256)", one, then_block)
        if old and h2i(old):
            self.s(self.vault, "convertToAssets(1 share)", then_block)
            dt = self.ch.ts - h2i(self.ch.m.call_tool("get_block", {"block": then_block})["timestamp"])
            g = Decimal(now) / Decimal(h2i(old))
            self.apy = Decimal(math.exp(math.log(float(g)) * YEAR / dt)) - 1 if g > 0 else None
            self.apy_basis = f"realised share-price growth over the last {window_days} days, annualised (net of vault fees)"
        else:
            self.apy_basis = f"unknown: vault younger than {window_days} days or history unreadable"
            self.notes.append("no share-price history → excluded from recommendations")
        idle = h2i(self.ch.call(self.asset, "balanceOf(address)", w(self.vault)))
        self.s(self.asset, "balanceOf(vault)")
        self.idle = amt(idle, self.dec)
        self.liquidity = None   # exit is checked by simulating redeem() from the holder
        for fn in ("performanceFee()", "managementFee()"):
            raw = self.ch.try_call(self.vault, fn)
            if raw is not None:
                v = h2i(raw)
                if v == 0 or 10 ** 14 <= v <= 6 * 10 ** 17:   # WAD-scaled (Morpho Vault V2 style)
                    self.notes.append(f"{fn[:-2]} {Decimal(v) / WAD * 100:.1f}% (already netted in share price)")
                    self.s(self.vault, fn)
                else:
                    self.notes.append(f"{fn[:-2]} raw {v} (scale not recognised, not used)")
        self.notes.append(f"idle {self.sym} in the vault contract: {self.idle:,.2f}")
        return self

    def fees(self, amount):
        raw = int(amount * (Decimal(10) ** self.dec))
        if raw <= 0:
            return Decimal(0), Decimal(0), "n/a"
        pd = self.ch.try_call(self.vault, "previewDeposit(uint256)", w(raw))
        cs = self.ch.try_call(self.vault, "convertToShares(uint256)", w(raw))
        entry = exit_ = Decimal(0)
        how = []
        if pd and cs and h2i(cs):
            entry = max(Decimal(0), (1 - Decimal(h2i(pd)) / Decimal(h2i(cs))) * 10_000)
            how.append("entry = 1 − previewDeposit/convertToShares")
            self.s(self.vault, "previewDeposit(amount)")
            shares = h2i(cs)
            pr = self.ch.try_call(self.vault, "previewRedeem(uint256)", w(shares))
            ca = self.ch.try_call(self.vault, "convertToAssets(uint256)", w(shares))
            if pr and ca and h2i(ca):
                exit_ = max(Decimal(0), (1 - Decimal(h2i(pr)) / Decimal(h2i(ca))) * 10_000)
                how.append("exit = 1 − previewRedeem/convertToAssets")
                self.s(self.vault, "previewRedeem(shares)")
        else:
            how.append("previewDeposit not readable: fees unknown, assumed 0")
        return entry, exit_, "; ".join(how)

    def apy_after(self, add):
        """New money joins the vault's pool: the same yield spread over more assets (upper bound on dilution;
        the curator may allocate it at similar rates, so treat this as a conservative estimate)."""
        if self.apy is None or self.tvl + add <= 0:
            return self.apy
        return self.apy * self.tvl / (self.tvl + add)

    def position(self, wallet):
        sh = h2i(self.ch.call(self.vault, "balanceOf(address)", w(wallet)))
        self.s(self.vault, "balanceOf(wallet)")
        self._shares = sh
        if not sh:
            return Decimal(0), 0
        raw = h2i(self.ch.call(self.vault, "convertToAssets(uint256)", w(sh)))
        self.s(self.vault, "convertToAssets(shares)")
        return amt(raw, self.dec), raw

    def build_deposit(self, wallet, raw):
        return [self.approve_tx(wallet, raw),
                self.tx(wallet, self.vault, "0x" + self.deposit_method + w(raw) + w(wallet),
                        f"{self.label}: deposit({amt(raw, self.dec)} {self.sym}, receiver=you)")]

    def build_withdraw(self, wallet, raw, everything=False):
        if everything:
            self.position(wallet)
            sh = self._shares
        else:
            sh = h2i(self.ch.call(self.vault, "convertToShares(uint256)", w(raw)))
        return [self.tx(wallet, self.vault, "0x" + self.withdraw_method + w(sh) + w(wallet) + w(wallet),
                        f"{self.label}: redeem({sh} shares, receiver=you, owner=you)")]


# ------------------------------------------------------------------ discovery --
def load_registry() -> dict:
    with open(os.path.join(HERE, "venues.json"), encoding="utf-8") as f:
        return json.load(f)


def discover(ch: Chain, asset: str, extra_vaults: list[str] | None = None, protocols: set[str] | None = None):
    """Return (venues, unavailable) for `asset`. A protocol that errors is reported, the others keep working."""
    reg = load_registry()
    venues, down = [], []
    want = protocols or {"avalon", "morpho", "vault"}

    def attempt(name, fn):
        try:
            fn()
        except (VenueUnavailable, MCPError, OSError, ValueError, KeyError, IndexError) as e:
            down.append({"venue": name, "error": str(e)[:200]})

    if "avalon" in want:
        def av():
            if "avalon" in ch.fail:
                raise VenueUnavailable("Avalon marked unavailable (--simulate-outage drill)")
            pool = "0x" + ch.call(AVALON_AUSDC, "POOL()")[-40:]
            lst = words(ch.call(pool, "getReservesList()"))
            for r in ["0x%040x" % x for x in lst[2:2 + lst[1]]]:
                if r == asset:
                    rd = words(ch.call(pool, "getReserveData(address)", w(r)))
                    venues.append(AvalonReserve(ch, asset, pool, rd).load())
        attempt("Avalon", av)
    if "morpho" in want:
        for mid, name in reg["morpho_markets"].items():
            def mo(mid=mid, name=name):
                if "morpho" in ch.fail:
                    raise VenueUnavailable("Morpho Blue marked unavailable (--simulate-outage drill)")
                p = words(ch.call(MORPHO, "idToMarketParams(bytes32)", mid[2:]))
                if "0x%040x" % p[0] == asset:
                    venues.append(MorphoMarket(ch, asset, mid, p, name).load())
            attempt(f"Morpho Blue {name} {mid[:10]}…", mo)
    if "vault" in want:
        for v in list(reg["erc4626_vaults"]) + [check_address(x) for x in (extra_vaults or [])]:
            def va(v=v):
                if "vault" in ch.fail:
                    raise VenueUnavailable("ERC-4626 vaults marked unavailable (--simulate-outage drill)")
                a = ch.try_call(v, "asset()")
                if a and "0x" + a[-40:] == asset:
                    venues.append(Erc4626Vault(ch, asset, v).load())
            attempt(f"ERC-4626 {v}", va)
    return venues, down


def resolve_token(t: str) -> str:
    t = (t or "").strip()
    if t.upper() in TOKENS:
        return TOKENS[t.upper()]
    return check_address(t)


def to_raw(x: Decimal, dec: int) -> int:
    return int((x * (Decimal(10) ** dec)).to_integral_value(rounding="ROUND_DOWN"))


def simulate(ch: Chain, tx: dict) -> tuple[bool, str]:
    """eth_call the unsigned tx from the user's address at the pinned block (no state change, nothing sent)."""
    is_err, text = ch.m.call_tool_raw("eth_call", {"transaction": {"from": tx["from"], "to": tx["to"], "data": tx["data"]},
                                                  "block": ch.block})
    if not is_err:
        return True, "ok"
    try:
        msg = json.loads(text).get("error", {}).get("message", text)
    except (json.JSONDecodeError, AttributeError):
        msg = text
    return False, str(msg)[:160]
