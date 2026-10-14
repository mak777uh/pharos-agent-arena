#!/usr/bin/env python3
"""Portfolio Assistant for Pharos mainnet: ask plain-English questions about a wallet, get answers from chain.

    python portfolio_assistant.py 0xWALLET "what am I earning?" "where is my USDC?" "what's my biggest position?"

Step 1 - READ (one snapshot, every eth_call pinned to the same block, via the hosted Pharos Chain MCP
https://mcp.pharos.xyz/mcp; read-only):
  wallet       native PROS (get_balance) + ERC-20 balanceOf for the Pharos token registry tokens
               (docs.pharos.xyz/getting-started/token-registry: WPROS, USDC, LINK, WETH) and every other token
               the wallet ever received (token list from the Pharosscan explorer index)
  Morpho Blue  0x18573fa1… position(id, wallet) in every market -> supplied / borrowed / collateral, supply and
               borrow APY from the market's IRM borrowRateView(), health factor from the market oracle and LLTV
  Avalon       Aave-V3-style lending pool (aToken "Avalon USDC" -> POOL()) : getReservesList(), getReserveData()
               -> aToken / variable-debt balances and current supply / borrow APR, getUserAccountData() -> HF,
               asset prices from the pool's own oracle (ADDRESSES_PROVIDER().getPriceOracle())
  ERC-4626     any token the wallet holds that answers asset() + convertToAssets() (e.g. Morpho Vault V2 vaults)
  vaults       -> underlying value; yield = share-price change over the last 7 days, annualised
Step 2 - ANSWER: a small rule-based intent parser (no LLM, no guessing) maps the question to one of 9 question
types and answers only from the snapshot. Every figure carries its source: contract, function, block.
Anything it cannot read or price is said explicitly ("can't read" / "no price"), never estimated.
Nothing is signed or sent.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from decimal import Decimal, getcontext

from Crypto.Hash import keccak

from pharos_explorer import EXPLORER_UI, PharosExplorer
from pharos_mcp import DEFAULT_ENDPOINT, MCPError, PharosMCP, check_address

getcontext().prec = 60
__version__ = "1.0.0"
MORPHO = "0x18573fa18fd17ddfd790b4a5b5b2977aad3b4efb"
AVALON_AUSDC = "0xe2ec2f3933ad2f83de9a9dda844558354384e87d"   # "Avalon USDC" aToken; POOL() gives the pool
PROS_USD_FEED = "0x371ed36aee5bffbed1a2aa9b7ded7f4eb30f5466"  # Chainlink PROS/USD
REGISTRY = {  # Pharos Pacific mainnet token registry (docs.pharos.xyz/getting-started/token-registry)
    "0x52c48d4213107b20bc583832b0d951fb9ca8f0b0": "WPROS",
    "0xc879c018db60520f4355c26ed1a6d572cdac1815": "USDC",
    "0x51e2a24742db77604b881d6781ee16b5b8fcbe29": "LINK",
    "0x1f4b7011ee3d53969bb67f59428a9ec0477856e9": "WETH",
}
WPROS, USDC = "0x52c48d4213107b20bc583832b0d951fb9ca8f0b0", "0xc879c018db60520f4355c26ed1a6d572cdac1815"
WAD, RAY, ORACLE_SCALE = 10 ** 18, 10 ** 27, 10 ** 36
YEAR = 365 * 86400
DUST_USD = Decimal("0.01")


def sel(sig: str) -> str:
    k = keccak.new(digest_bits=256)
    k.update(sig.encode())
    return k.hexdigest()[:8]


F = {n: sel(n) for n in [
    "balanceOf(address)", "symbol()", "decimals()", "asset()", "convertToAssets(uint256)", "totalAssets()",
    "idToMarketParams(bytes32)", "market(bytes32)", "position(bytes32,address)", "price()", "latestRoundData()",
    "POOL()", "getReservesList()", "getReserveData(address)", "getUserAccountData(address)",
    "ADDRESSES_PROVIDER()", "getPriceOracle()", "getAssetPrice(address)", "name()"]}
F_RATE = sel("borrowRateView((address,address,address,address,uint256),(uint128,uint128,uint128,uint128,uint128,uint128))")
F_CREATE = "0x" + sel("createMarket((address,address,address,address,uint256))")
T_CREATE = "0x" + keccak.new(digest_bits=256, data=b"CreateMarket(bytes32,(address,address,address,address,uint256))").hexdigest()


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


def pad(a: str) -> str:
    return a[2:].lower().rjust(64, "0")


def amt(raw: int, dec: int) -> Decimal:
    return Decimal(raw) / (Decimal(10) ** dec)


def fnum(x: Decimal, places: int = 4) -> str:
    if x and abs(x) < Decimal(10) ** -places:  # dust: keep significant digits instead of printing 0.0000
        return f"{x:.3e}"
    return f"{x:,.{places}f}"


def fusd(x: Decimal | None) -> str:
    if x is None:
        return "no price"
    return f"{'−' if x < 0 else ''}${abs(x):,.2f}"


def short(a: str) -> str:
    return a[:6] + "…" + a[-4:]


class Snapshot:
    """Reads everything once, at one block. Each position dict carries 'src' = list of (contract, fn, block)."""

    def __init__(self, mcp: PharosMCP, ex: PharosExplorer, wallet: str, known_markets: list[str]):
        self.m, self.ex, self.w = mcp, ex, wallet
        self.block = mcp.block_number()
        self.ts = h2i(mcp.call_tool("get_block", {"block": self.block})["timestamp"])
        self._tok: dict[str, tuple[str, int] | None] = {}
        self.known_markets = known_markets
        self.positions: list[dict] = []     # assets and debts
        self.unreadable: list[str] = []     # what we could not read / value, in plain words
        self.protocols_checked: list[str] = []
        self.prices: dict[str, tuple[Decimal, str]] = {}  # token -> (usd, source)

    # ---- low-level -----------------------------------------------------
    def call(self, to: str, fn: str, args: str = "", block: int | None = None) -> str:
        data = "0x" + (F.get(fn) or fn) + args
        last = None
        for attempt in range(3):
            try:
                return self.m.eth_call(to, data, self.block if block is None else block)
            except MCPError as e:
                last = e
                if "revert" in str(e).lower() or "execution" in str(e).lower():
                    raise
                time.sleep(1 + attempt)
        raise last

    def try_call(self, to: str, fn: str, args: str = "", block: int | None = None) -> str | None:
        try:
            out = self.call(to, fn, args, block)
            return out if out and out != "0x" else None
        except MCPError:
            return None

    def token(self, a: str) -> tuple[str, int] | None:
        if a not in self._tok:
            dec = self.try_call(a, "decimals()")
            if dec is None:
                self._tok[a] = None
            else:
                sym = REGISTRY.get(a) or short(a)
                raw = self.try_call(a, "symbol()")
                if raw:
                    b = bytes.fromhex(raw[2:])
                    try:
                        if len(b) >= 96:
                            sym = b[64:64 + int.from_bytes(b[32:64], "big")].decode() or sym
                        elif len(b) == 32:
                            sym = b.rstrip(b"\0").decode() or sym
                    except UnicodeDecodeError:
                        pass
                self._tok[a] = (sym, h2i(dec))
        return self._tok[a]

    def src(self, contract: str, fn: str) -> tuple[str, str, int]:
        return (contract, fn, self.block)

    def add(self, **p) -> None:
        self.positions.append(p)

    # ---- prices ----------------------------------------------------------
    def price(self, token: str) -> tuple[Decimal, str] | None:
        if token in self.prices:
            return self.prices[token]
        return None

    def set_price(self, token: str, usd: Decimal, how: str) -> None:
        if token not in self.prices:
            self.prices[token] = (usd, how)

    def base_prices(self) -> None:
        self.set_price(USDC, Decimal(1), "USDC counted as $1.00 (assumption, no USDC/USD feed used)")
        w = words(self.call(PROS_USD_FEED, "latestRoundData()"))
        dec = h2i(self.call(PROS_USD_FEED, "decimals()"))
        p = amt(w[1], dec)
        self.set_price(WPROS, p, f"Chainlink PROS/USD {PROS_USD_FEED} latestRoundData() @ block {self.block:,}")
        self.set_price("native", p, f"Chainlink PROS/USD {PROS_USD_FEED} latestRoundData() @ block {self.block:,}")

    # ---- readers -----------------------------------------------------------
    def read_wallet(self) -> list[str]:
        bal = self.m.call_tool("get_balance", {"address": self.w, "block": self.block})
        wei = h2i(bal.get("balanceWei", 0))
        self.add(kind="wallet", protocol="Wallet", token="native", symbol="PROS", amount=amt(wei, 18),
                 usd_key="native", src=[(self.w, "get_balance", self.block)])
        tokens = dict.fromkeys(REGISTRY)
        try:
            _, rows = self.ex.token_transfers(self.w, 1000)
            for r in rows:
                ti = r.get("token_info") or {}
                if ti.get("type") in (None, "ERC20"):
                    tokens.setdefault(r["token_address"].lower())
        except Exception as e:  # explorer down -> registry tokens only, and say so
            self.unreadable.append(f"token list from the explorer failed ({e}); only registry tokens were checked")
        held = []
        for t in tokens:
            raw = self.try_call(t, "balanceOf(address)", pad(self.w))
            if raw is None or h2i(raw) == 0:
                continue
            meta = self.token(t)
            if meta is None:
                self.unreadable.append(f"token {t} has a balance but no decimals(); amount not shown")
                continue
            held.append(t)
            self.add(kind="wallet", protocol="Wallet", token=t, symbol=meta[0], amount=amt(h2i(raw), meta[1]),
                     usd_key=t, src=[self.src(t, "balanceOf(wallet)")])
        return held

    def read_morpho(self) -> None:
        self.protocols_checked.append(f"Morpho Blue {MORPHO}")
        ids = list(self.known_markets)
        try:
            _, txs = self.ex.transactions(MORPHO, 5000)
            for t in txs:
                if t.get("method_id") == F_CREATE and t.get("receipt_status") == 1:
                    for lg in self.m.call_tool("get_transaction_receipt", {"hash": t["hash"]})["logs"]:
                        if lg["address"].lower() == MORPHO and lg["topics"][0] == T_CREATE and lg["topics"][1] not in ids:
                            ids.append(lg["topics"][1])
        except Exception as e:
            self.unreadable.append(f"Morpho market discovery via explorer failed ({e}); used the known list only")
        for mid in ids:
            pos = words(self.call(MORPHO, "position(bytes32,address)", mid[2:] + pad(self.w)))
            if not any(pos):
                continue
            p = words(self.call(MORPHO, "idToMarketParams(bytes32)", mid[2:]))
            loan, col, oracle, irm, lltv = "0x%040x" % p[0], "0x%040x" % p[1], "0x%040x" % p[2], "0x%040x" % p[3], p[4]
            mk = words(self.call(MORPHO, "market(bytes32)", mid[2:]))  # tsa, tss, tba, tbs, lastUpdate, fee
            lt, ct = self.token(loan), self.token(col) if p[1] else ("-", 18)
            rate = 0
            if p[3]:
                data = "".join(format(x, "064x") for x in p[:5]) + "".join(format(x, "064x") for x in mk[:6])
                rate = h2i(self.call(irm, F_RATE, data))
            borrow_apy = Decimal(math.expm1(rate * YEAR / WAD))
            util = Decimal(mk[2]) / Decimal(mk[0]) if mk[0] else Decimal(0)
            supply_apy = borrow_apy * util * (1 - Decimal(mk[5]) / WAD)
            name = f"{ct[0]}/{lt[0]}"
            msrc = [self.src(MORPHO, f"position({short(mid)}, wallet)"), self.src(MORPHO, f"market({short(mid)})"),
                    self.src(irm, "borrowRateView()")]
            supplied = pos[0] * (mk[0] + 1) // (mk[1] + 10 ** 6) if pos[0] else 0       # SharesMathLib, round down
            debt = -(-pos[1] * (mk[2] + 1) // (mk[3] + 10 ** 6)) if pos[1] else 0      # round up
            px = None
            if pos[2] and oracle != "0x" + "0" * 40:
                praw = self.try_call(oracle, "price()")
                if praw:
                    px = Decimal(h2i(praw)) * Decimal(10) ** ct[1] / Decimal(ORACLE_SCALE) / Decimal(10) ** lt[1]
            if supplied:
                self.add(kind="supply", protocol="Morpho Blue", market=name, market_id=mid, token=loan, symbol=lt[0],
                         amount=amt(supplied, lt[1]), usd_key=loan, apy=supply_apy, src=msrc)
            if pos[2]:
                usd_key = col
                if px is not None and self.price(loan):
                    self.set_price(col, px * self.price(loan)[0],
                                   f"Morpho oracle {oracle} price() @ block {self.block:,} × {lt[0]} price")
                self.add(kind="collateral", protocol="Morpho Blue", market=name, market_id=mid, token=col,
                         symbol=ct[0], amount=amt(pos[2], ct[1]), usd_key=usd_key, apy=None,
                         src=msrc[:1] + ([self.src(oracle, "price()")] if px is not None else []))
            if debt:
                hf = None
                if px is not None and pos[2]:
                    hf = amt(pos[2], ct[1]) * px * Decimal(lltv) / WAD / amt(debt, lt[1])
                self.add(kind="debt", protocol="Morpho Blue", market=name, market_id=mid, token=loan, symbol=lt[0],
                         amount=amt(debt, lt[1]), usd_key=loan, apy=borrow_apy, health_factor=hf,
                         lltv=Decimal(lltv) / WAD, src=msrc + ([self.src(oracle, "price()")] if px is not None else []))

    def read_avalon(self) -> None:
        pool_raw = self.try_call(AVALON_AUSDC, "POOL()")
        if not pool_raw:
            self.unreadable.append(f"Avalon: POOL() of {AVALON_AUSDC} not readable; Avalon positions unknown")
            return
        pool = "0x" + pool_raw[-40:]
        self.protocols_checked.append(f"Avalon lending pool {pool}")
        oracle = None
        prov = self.try_call(pool, "ADDRESSES_PROVIDER()")
        if prov:
            o = self.try_call("0x" + prov[-40:], "getPriceOracle()")
            oracle = "0x" + o[-40:] if o else None
        lst = self.call(pool, "getReservesList()")
        w = words(lst)
        reserves = ["0x%040x" % x for x in w[2:2 + w[1]]]
        for asset in reserves:
            rd = words(self.call(pool, "getReserveData(address)", pad(asset)))
            # Aave V3 ReserveData: 0 config, 1 liquidityIndex, 2 currentLiquidityRate, 3 variableBorrowIndex,
            # 4 currentVariableBorrowRate, 5 stableRate, 6 lastUpdate, 7 id, 8 aToken, 9 stableDebt, 10 variableDebt
            a_tok, v_debt = "0x%040x" % rd[8], "0x%040x" % rd[10]
            # aTokens / debt tokens are receipts of the Avalon position, not separate wallet assets
            self.positions = [p for p in self.positions
                              if not (p["kind"] == "wallet" and p["token"] in (a_tok, v_debt, "0x%040x" % rd[9]))]
            meta = self.token(asset)
            if meta is None:
                continue
            if oracle:
                pr = self.try_call(oracle, "getAssetPrice(address)", pad(asset))
                if pr and h2i(pr):
                    self.set_price(asset, amt(h2i(pr), 8),
                                   f"Avalon oracle {oracle} getAssetPrice() @ block {self.block:,} (8 decimals USD)")
            sup_apr, bor_apr = Decimal(rd[2]) / RAY, Decimal(rd[4]) / RAY
            for tok, kind, apr in ((a_tok, "supply", sup_apr), (v_debt, "debt", bor_apr)):
                raw = self.try_call(tok, "balanceOf(address)", pad(self.w))
                if raw and h2i(raw):
                    self.add(kind=kind, protocol="Avalon", market=meta[0], token=asset, symbol=meta[0],
                             amount=amt(h2i(raw), meta[1]), usd_key=asset,
                             apy=Decimal(math.expm1(float(apr))),  # APR (ray) -> APY, continuous compounding approx.
                             src=[self.src(tok, "balanceOf(wallet)"), self.src(pool, f"getReserveData({meta[0]})")])
        acct = self.try_call(pool, "getUserAccountData(address)", pad(self.w))
        if acct:
            a = words(acct)
            if a[1]:
                self.avalon_hf = (amt(a[5], 18), pool)

    def read_vaults(self, held: list[str]) -> None:
        """Wallet tokens that are ERC-4626 vault shares: value them in the underlying, yield from 7-day share price."""
        week_block = None
        for t in held:
            asset_raw = self.try_call(t, "asset()")
            if not asset_raw:
                continue
            underlying = "0x" + asset_raw[-40:]
            meta, umeta = self.token(t), self.token(underlying)
            if not meta or not umeta:
                continue
            bal = h2i(self.call(t, "balanceOf(address)", pad(self.w)))
            conv = self.try_call(t, "convertToAssets(uint256)", format(bal, "064x"))
            if conv is None:
                self.unreadable.append(f"{meta[0]} ({t}) has asset() but convertToAssets() failed; value unknown")
                continue
            one = 10 ** meta[1]
            if week_block is None:
                week_block = self.block_at(self.ts - 7 * 86400)
            now_pps = self.try_call(t, "convertToAssets(uint256)", format(one, "064x"))
            old_pps = self.try_call(t, "convertToAssets(uint256)", format(one, "064x"), week_block)
            apy = None
            if now_pps and old_pps and h2i(old_pps):
                growth = Decimal(h2i(now_pps)) / Decimal(h2i(old_pps))
                dt = self.ts - h2i(self.m.call_tool("get_block", {"block": week_block})["timestamp"])
                apy = Decimal(math.exp(math.log(float(growth)) * YEAR / dt)) - 1 if growth > 0 else None
            name = self.try_call(t, "name()")
            label = meta[0]
            if name:
                b = bytes.fromhex(name[2:])
                if len(b) >= 96:
                    label = b[64:64 + int.from_bytes(b[32:64], "big")].decode("utf-8", "replace") or label
            self.protocols_checked.append(f"ERC-4626 vault {label} {t}")
            # the share token itself is replaced by its underlying value
            self.positions = [p for p in self.positions if not (p["kind"] == "wallet" and p["token"] == t)]
            self.add(kind="vault", protocol=f"Vault {label}", market=meta[0], token=underlying, symbol=umeta[0],
                     amount=amt(h2i(conv), umeta[1]), usd_key=underlying, apy=apy, shares=amt(bal, meta[1]),
                     share_token=t, src=[self.src(t, "balanceOf(wallet)"), self.src(t, "convertToAssets(shares)"),
                                         (t, "convertToAssets(1 share)", week_block)])

    def block_at(self, t: int) -> int:
        lo, hi = 1, self.block
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if h2i(self.m.call_tool("get_block", {"block": mid})["timestamp"]) <= t:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def load(self) -> "Snapshot":
        self.avalon_hf = None
        self.base_prices()
        held = self.read_wallet()
        self.read_avalon()        # before Morpho: the Avalon oracle prices more tokens
        self.read_morpho()
        self.read_vaults(held)
        for p in self.positions:
            pr = self.price(p["usd_key"])
            p["usd"] = p["amount"] * pr[0] if pr else None
            p["price_src"] = pr[1] if pr else None
        for p in self.positions:
            if p["usd"] is None and p["kind"] == "wallet":
                self.unreadable.append(f"{p['symbol']} ({p['token']}) in the wallet: no on-chain price source I trust, "
                                       "so it is left out of USD totals")
        return self


# ---------------------------------------------------------------- answering --
QUESTION_TYPES = [
    ("earning", r"\b(earn|earning|yield|apy|apr|interest|income|making|returns?)\b"),
    ("where_token", r"\bwhere\b.*\b(is|are)\b.*\bmy\b\s+([a-z0-9.\-]+)|\bwhere\b.*\bmy\b\s+([a-z0-9.\-]+)"),
    ("biggest", r"\b(biggest|largest|top|main)\b.*\b(position|holding|exposure|asset)s?\b"),
    ("health", r"\b(health|liquidat\w*|safe|risk)\b"),
    ("debt", r"\b(owe|debt|borrow\w*|loan|loans|liabilit\w*)\b"),
    ("networth", r"\b(net worth|worth|total|portfolio value|how much (do i|have i) (have|got))\b"),
    ("wallet", r"\b(wallet|balances?|tokens?|idle|holdings?)\b"),
    ("protocols", r"\b(protocols?|platforms?|where am i|which (apps|dapps)|vaults?)\b"),
    ("how_much_token", r"\bhow (much|many)\b\s+([a-z0-9.\-]+)"),
]


FORECAST_RX = r"\b(will|predict\w*|forecast\w*|next (year|month|week)|tomorrow|should i|going to)\b"


class Assistant:
    def __init__(self, snap: Snapshot):
        self.s = snap

    def sources(self, ps: list[dict]) -> list[str]:
        out = []
        for p in ps:
            for c, fn, b in p["src"]:
                line = f"{c} {fn} @ block {b:,}" if b else f"{c} {fn}"
                if line not in out:
                    out.append(line)
            if p.get("price_src") and p["price_src"] not in out:
                out.append(p["price_src"])
        return out

    def fmt_pos(self, p: dict) -> str:
        where = p["protocol"] + (f" · {p['market']}" if p.get("market") else "")
        kind = {"wallet": "held", "supply": "supplied (earning)", "collateral": "posted as collateral",
                "debt": "BORROWED (owed)", "vault": "deposited"}[p["kind"]]
        extra = ""
        if p.get("apy") is not None:
            extra = f", {'borrow' if p['kind'] == 'debt' else 'supply'} APY {p['apy'] * 100:.2f}%"
            if p["kind"] == "vault":
                extra = f", 7-day share-price APY {p['apy'] * 100:.2f}%"
        elif p["kind"] == "vault":
            extra = ", APY unknown (no share-price history)"
        where = "your wallet" if p["kind"] == "wallet" else where
        return f"{fnum(p['amount'])} {p['symbol']} ({fusd(p['usd'])}) — {kind} in {where}{extra}"

    def answer(self, q: str) -> dict:
        ql = q.lower().strip()
        if not ql or len(ql) > 300:
            return {"type": "invalid", "text": "Please ask a question of 1–300 characters.", "sources": []}
        if re.search(FORECAST_RX, ql):
            return {"type": "out_of_scope", "sources": [],
                    "text": "I only report what is on chain right now; I can't predict prices or future yields and I "
                            "don't give buy/sell advice, so I won't guess."}
        for qtype, rx in QUESTION_TYPES:
            m = re.search(rx, ql)
            if m:
                fn = getattr(self, "q_" + qtype)
                text, used = fn(ql, m)
                return {"type": qtype, "text": text, "sources": self.sources(used)}
        return {"type": "unknown", "sources": [],
                "text": "I don't know how to answer that from chain data, so I won't guess. I can answer: what am I "
                        "earning? · where is my <TOKEN>? · what's my biggest position? · what's my net worth? · "
                        "how much do I owe? · am I at risk of liquidation? · what's in my wallet? · which protocols "
                        "am I in? · how much <TOKEN> do I have?"}

    def caveat(self) -> str:
        if not self.s.unreadable:
            return ""
        return "\nWhat I could NOT read or value: " + "; ".join(self.s.unreadable)

    # each q_* returns (text, positions used)
    def q_earning(self, q, m):
        earn = [p for p in self.s.positions if p["kind"] in ("supply", "vault") and p["usd"]]
        debts = [p for p in self.s.positions if p["kind"] == "debt" and p["usd"]]
        if not earn and not debts:
            return "Nothing in this wallet is earning yield in the protocols I read (Morpho Blue, Avalon, ERC-4626 "\
                   "vaults)." + self.caveat(), []
        L, inc, cost = [], Decimal(0), Decimal(0)
        for p in sorted(earn, key=lambda x: x["usd"], reverse=True):
            if p.get("apy") is None:
                L.append(f"• {self.fmt_pos(p)} → yield unknown, not counted")
                continue
            y = p["usd"] * p["apy"]
            inc += y
            L.append(f"• {self.fmt_pos(p)} → ≈ {fusd(y)}/year")
        for p in debts:
            c = p["usd"] * p["apy"] if p.get("apy") is not None else Decimal(0)
            cost += c
            L.append(f"• cost: {self.fmt_pos(p)} → ≈ −{fusd(c)}/year")
        head = (f"You are earning about {fusd(inc)} per year (≈ {fusd(inc / 365)}/day) at current rates"
                + (f", and paying about {fusd(cost)} per year in borrow interest → net ≈ {fusd(inc - cost)}/year."
                   if debts else "."))
        return head + "\n" + "\n".join(L) + "\nRates are current on-chain rates and change every block; "\
            "vault APY is the last 7 days' share-price growth, annualised." + self.caveat(), earn + debts

    def _token_match(self, sym: str) -> list[dict]:
        sym = sym.strip("?.!,").lower()
        alias = {"pros": {"pros", "wpros"}, "wpros": {"pros", "wpros"}}
        want = alias.get(sym, {sym})
        return [p for p in self.s.positions if p["symbol"].lower() in want]

    def q_where_token(self, q, m):
        sym = next(g for g in m.groups()[1:] if g)
        ps = self._token_match(sym)
        if not ps:
            known = sorted({p["symbol"] for p in self.s.positions})
            return (f"I found no {sym.upper()} in this wallet or in the protocols I read. Tokens I did find: "
                    f"{', '.join(known) or 'none'}." + self.caveat()), []
        tot = sum((p["amount"] for p in ps if p["kind"] != "debt"), Decimal(0))
        owed = sum((p["amount"] for p in ps if p["kind"] == "debt"), Decimal(0))
        L = [f"• {self.fmt_pos(p)}" for p in sorted(ps, key=lambda x: x["amount"], reverse=True)]
        head = f"Your {sym.upper()}: {fnum(tot)} in total across {len([p for p in ps if p['kind'] != 'debt'])} place(s)"
        if owed:
            head += f", and you owe {fnum(owed)} {sym.upper()}"
        return head + ":\n" + "\n".join(L), ps

    q_how_much_token = q_where_token

    def q_biggest(self, q, m):
        assets = [p for p in self.s.positions if p["kind"] != "debt"]
        priced = [p for p in assets if p["usd"] is not None]
        if not priced:
            return "I can't rank positions: none of them has a price I can read." + self.caveat(), []
        top = sorted(priced, key=lambda x: x["usd"], reverse=True)
        total = sum((p["usd"] for p in priced), Decimal(0))
        L = [f"{i}. {self.fmt_pos(p)} — {p['usd'] / total * 100:.1f}% of priced assets"
             for i, p in enumerate(top[:3], 1)]
        return f"Your biggest position is {top[0]['symbol']} {top[0]['kind']} in {top[0]['protocol']} worth "\
               f"{fusd(top[0]['usd'])}.\nTop 3:\n" + "\n".join(L) + self.caveat(), top[:3]

    def q_networth(self, q, m):
        assets = [p for p in self.s.positions if p["kind"] != "debt"]
        debts = [p for p in self.s.positions if p["kind"] == "debt"]
        a = sum((p["usd"] for p in assets if p["usd"] is not None), Decimal(0))
        d = sum((p["usd"] for p in debts if p["usd"] is not None), Decimal(0))
        by = {}
        for p in assets:
            if p["usd"] is not None:
                by[p["protocol"]] = by.get(p["protocol"], Decimal(0)) + p["usd"]
        L = [f"• {k}: {fusd(v)}" for k, v in sorted(by.items(), key=lambda x: x[1], reverse=True)]
        return (f"Net worth ≈ {fusd(a - d)}: assets {fusd(a)} minus debts {fusd(d)} (priced positions only).\n"
                + "\n".join(L) + self.caveat()), assets + debts

    def q_debt(self, q, m):
        debts = [p for p in self.s.positions if p["kind"] == "debt"]
        if not debts:
            return "You have no debt in Morpho Blue or Avalon." + self.caveat(), []
        L = [f"• {self.fmt_pos(p)}" + (f", health factor {p['health_factor']:.3f}" if p.get("health_factor") else "")
             for p in debts]
        tot = sum((p["usd"] for p in debts if p["usd"] is not None), Decimal(0))
        return f"You owe {fusd(tot)} in total:\n" + "\n".join(L), debts

    def q_health(self, q, m):
        debts = [p for p in self.s.positions if p["kind"] == "debt" and p["protocol"] == "Morpho Blue"]
        L, used = [], []
        for p in debts:
            used.append(p)
            hf = p.get("health_factor")
            if hf is None:
                L.append(f"• Morpho {p['market']}: health factor unknown (oracle price not readable)")
                continue
            col = [c for c in self.s.positions if c["kind"] == "collateral" and c.get("market_id") == p["market_id"]]
            drop = (1 - 1 / hf) * 100 if hf > 0 else Decimal(0)
            state = "LIQUIDATABLE NOW" if hf < 1 else "at risk" if hf < Decimal("1.1") else \
                "watch it" if hf < Decimal("1.3") else "healthy"
            L.append(f"• Morpho {p['market']}: health factor {hf:.3f} ({state}); liquidation if the "
                     f"{col[0]['symbol'] if col else 'collateral'} price falls {drop:.1f}% (LLTV {p['lltv'] * 100:.1f}%)")
            used += col
        if getattr(self.s, "avalon_hf", None):
            hf, pool = self.s.avalon_hf
            L.append(f"• Avalon: health factor {hf:.3f} (getUserAccountData @ pool {pool})")
            used.append({"src": [(pool, "getUserAccountData(wallet)", self.s.block)], "price_src": None})
        if not L:
            return "No borrowing found in Morpho Blue or Avalon, so there is nothing to liquidate." + self.caveat(), []
        return "Liquidation risk (health factor < 1.0 = can be liquidated):\n" + "\n".join(L), used

    def q_wallet(self, q, m):
        ps = [p for p in self.s.positions if p["kind"] == "wallet"]
        if not ps:
            return "The wallet holds no tokens I can read." + self.caveat(), []
        tot = sum((p["usd"] for p in ps if p["usd"] is not None), Decimal(0))
        L = [f"• {fnum(p['amount'])} {p['symbol']} ({fusd(p['usd'])})"
             for p in sorted(ps, key=lambda x: x["usd"] or Decimal(-1), reverse=True)]
        return f"Tokens sitting in the wallet (not earning): {fusd(tot)} priced\n" + "\n".join(L) + self.caveat(), ps

    def q_protocols(self, q, m):
        used = sorted({p["protocol"] for p in self.s.positions if p["protocol"] != "Wallet"})
        return ("You have positions in: " + (", ".join(used) or "none of the protocols I read") + ".\nI checked: "
                + "; ".join(self.s.protocols_checked) + ". Other Pharos protocols (DEX LPs, other lenders) are NOT "
                "read by this assistant, so positions there would not show up." + self.caveat()), \
            [p for p in self.s.positions if p["protocol"] != "Wallet"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ask natural-language questions about a Pharos wallet (read-only).")
    ap.add_argument("wallet", help="0x wallet address")
    ap.add_argument("questions", nargs="*", help='questions, e.g. "what am I earning?" (none = interactive)')
    ap.add_argument("--markets", default="known_markets.json", help="known Morpho market ids (JSON)")
    ap.add_argument("--json", help="write questions, answers and sources to this JSON file")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Chain MCP endpoint (https only)")
    a = ap.parse_args(argv)
    try:
        wallet = check_address(a.wallet)
        known = []
        try:
            with open(a.markets, encoding="utf-8") as fh:
                known = list(json.load(fh).get("markets", {}))
        except FileNotFoundError:
            pass
        for mid in known:
            if not re.match(r"^0x[0-9a-f]{64}$", mid):
                raise ValueError(f"bad market id in {a.markets}: {mid}")
    except (ValueError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    t0 = time.time()
    snap = Snapshot(PharosMCP(a.endpoint), PharosExplorer(), wallet, known).load()
    print(f"=== Portfolio snapshot · {wallet} · Pharos mainnet block #{snap.block:,} "
          f"({time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(snap.ts))}) · read in {time.time() - t0:.0f} s")
    print(f"    {len(snap.positions)} positions; {EXPLORER_UI}/address/{wallet}\n")
    bot = Assistant(snap)
    qs = a.questions
    out = []

    def show(q):
        r = bot.answer(q)
        print(f"Q: {q}\n[{r['type']}] {r['text']}")
        if r["sources"]:
            print("Sources:\n" + "\n".join(f"  - {s}" for s in r["sources"]))
        print()
        out.append({"question": q, **r})

    if qs:
        for q in qs:
            show(q)
    else:
        print("Ask about this wallet (empty line to quit).")
        while True:
            try:
                q = input("> ").strip()
            except EOFError:
                break
            if not q:
                break
            show(q)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump({"wallet": wallet, "block": snap.block, "timestamp": snap.ts, "answers": out,
                       "positions": snap.positions, "unreadable": snap.unreadable}, fh, indent=1, default=str,
                      ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
