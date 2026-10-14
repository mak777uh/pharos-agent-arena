#!/usr/bin/env python3
"""Liquidation Watchdog for Morpho Blue positions on Pharos mainnet (read-only, via the Pharos Chain MCP).

For every configured (market id, borrower) it reads from chain, pinned to one block:
  Morpho.idToMarketParams(id)  -> loan token, collateral token, oracle, IRM, LLTV
  Morpho.market(id)            -> total borrow assets/shares, lastUpdate, fee
  Morpho.position(id, user)    -> borrowShares, collateral
  IRM.borrowRateView(params, market) -> per-second rate, to add the interest accrued since lastUpdate
  Oracle.price()               -> collateral price in loan-token units, scaled 1e36 (Morpho oracle convention)
and computes
  debt   = borrowShares * totalBorrowAssets' / totalBorrowShares   (rounded up, like Morpho)
  HF     = collateral * price / 1e36 * LLTV / debt                  (Morpho liquidates when HF < 1)
  liquidation price, the collateral price drop that would trigger liquidation, and the concrete
  repay / add-collateral amounts that bring HF back to --target-hf.
Alerts (WARNING below --warn, CRITICAL below --critical, LIQUIDATABLE below 1.0) are printed to the console with
UTC timestamps and appended as JSON lines to --alert-log. Nothing is signed or sent.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, getcontext

from Crypto.Hash import keccak

from pharos_mcp import DEFAULT_ENDPOINT, MCPError, PharosMCP, check_address

getcontext().prec = 60
__version__ = "1.0.0"
MORPHO = "0x18573fa18fd17ddfd790b4a5b5b2977aad3b4efb"  # Morpho Blue on Pharos mainnet (verified "Morpho" on pharosscan)
EXPLORER = "https://www.pharosscan.xyz"
WAD = 10 ** 18
ORACLE_SCALE = 10 ** 36
ID_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def sel(sig: str) -> str:
    k = keccak.new(digest_bits=256)
    k.update(sig.encode())
    return k.hexdigest()[:8]


S_PARAMS, S_MARKET, S_POSITION = sel("idToMarketParams(bytes32)"), sel("market(bytes32)"), sel("position(bytes32,address)")
S_PRICE, S_SYMBOL, S_DECIMALS = sel("price()"), sel("symbol()"), sel("decimals()")
S_RATE = sel("borrowRateView((address,address,address,address,uint256),(uint128,uint128,uint128,uint128,uint128,uint128))")


def words(h: str) -> list[int]:
    h = h[2:] if h.startswith("0x") else h
    return [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]


def w_taylor(rate: int, dt: int) -> int:
    """Morpho MathLib.wTaylorCompounded: e^(rate*dt) - 1 (3 terms), WAD-scaled."""
    first = rate * dt
    second = first * first // (2 * WAD)
    third = second * first // (3 * WAD)
    return first + second + third


def fmt(raw: int | Decimal, dec: int, places: int = 4) -> str:
    v = Decimal(raw) / (Decimal(10) ** dec)
    return f"{v:,.{places}f}"


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Watchdog:
    def __init__(self, mcp: PharosMCP, warn: float, critical: float, target: float, shock_pct: float = 0.0):
        if not (1.0 < critical < warn < target):
            raise ValueError("thresholds must satisfy 1.0 < --critical < --warn < --target-hf")
        if not 0 <= shock_pct < 100:
            raise ValueError("--price-shock must be in [0, 100)")
        self.m, self.warn, self.critical, self.target, self.shock = mcp, warn, critical, target, shock_pct
        self._tok: dict[str, tuple[str, int]] = {}
        self._params: dict[str, dict] = {}

    def call(self, to: str, data: str, block: int) -> str:
        return self.m.eth_call(to, "0x" + data, block)

    def token(self, addr: str, block: int) -> tuple[str, int]:
        if addr not in self._tok:
            sym = addr[:10]
            try:
                b = bytes.fromhex(self.call(addr, S_SYMBOL, block)[2:])
                sym = b[64:64 + int.from_bytes(b[32:64], "big")].decode() or sym
            except (MCPError, ValueError):
                pass
            self._tok[addr] = (sym, int(self.call(addr, S_DECIMALS, block), 16))
        return self._tok[addr]

    def params(self, mid: str, block: int) -> dict:
        if mid not in self._params:
            p = words(self.call(MORPHO, S_PARAMS + mid[2:], block))
            if p[0] == 0:
                raise ValueError(f"market {mid} does not exist on Morpho {MORPHO}")
            loan, col = "0x%040x" % p[0], "0x%040x" % p[1]
            self._params[mid] = {"loan": loan, "collateral": col, "oracle": "0x%040x" % p[2], "irm": "0x%040x" % p[3],
                                 "lltv": p[4], "raw": p, "loan_tok": self.token(loan, block), "col_tok": self.token(col, block)}
        return self._params[mid]

    def read(self, mid: str, user: str, block: int, block_ts: int) -> dict:
        P = self.params(mid, block)
        mkt = words(self.call(MORPHO, S_MARKET + mid[2:], block))  # tsa, tss, tba, tbs, lastUpdate, fee
        pos = words(self.call(MORPHO, S_POSITION + mid[2:] + user[2:].rjust(64, "0"), block))  # supplyShares, borrowShares, collateral
        tba, tbs, last = mkt[2], mkt[3], mkt[4]
        # accrue interest since lastUpdate, exactly like MorphoBalancesLib.expectedMarketBalances
        rate = 0
        elapsed = max(0, block_ts - last)
        if elapsed and tba and P["irm"] != "0x" + "0" * 40:
            data = S_RATE + "".join(format(x, "064x") for x in P["raw"][:5]) + "".join(format(x, "064x") for x in mkt[:6])
            rate = int(self.call(P["irm"], data, block), 16)
            tba += tba * w_taylor(rate, elapsed) // WAD
        borrow_shares, collateral = pos[1], pos[2]
        debt = -(-borrow_shares * (tba + 1) // (tbs + 10 ** 6)) if borrow_shares else 0  # toAssetsUp w/ virtual shares
        price = int(self.call(P["oracle"], S_PRICE, block), 16)
        price_used = price * (Decimal(100) - Decimal(str(self.shock))) / 100
        (ls, ld), (cs, cd) = P["loan_tok"], P["col_tok"]
        col_value = Decimal(collateral) * price_used / ORACLE_SCALE          # in loan-token raw units
        max_borrow = col_value * P["lltv"] / WAD
        hf = (max_borrow / debt) if debt else None
        r = {"market": mid, "borrower": user, "block": block, "loan": ls, "collateral_symbol": cs,
             "loan_decimals": ld, "collateral_decimals": cd, "lltv": P["lltv"] / WAD, "oracle": P["oracle"],
             "collateral_raw": collateral, "debt_raw": debt, "collateral_value_raw": col_value,
             "oracle_price_raw": price, "price_shock_pct": self.shock, "borrow_rate_apr": rate * 365 * 86400 / WAD,
             "health_factor": hf}
        # price of 1 collateral token in loan tokens (human units)
        unit_price = price_used / ORACLE_SCALE * (Decimal(10) ** cd) / (Decimal(10) ** ld)
        r["collateral_price"] = unit_price
        if debt and collateral:
            liq_unit_price = unit_price / hf
            r["liquidation_price"] = liq_unit_price
            r["drop_to_liquidation_pct"] = float((1 - 1 / hf) * 100) if hf > 0 else 0.0
            # concrete actions to restore --target-hf
            repay = Decimal(debt) - max_borrow / Decimal(str(self.target))
            add_col_value = Decimal(str(self.target)) * Decimal(debt) * WAD / P["lltv"] - col_value
            add_col_raw = add_col_value * ORACLE_SCALE / price_used if price_used else Decimal(0)
            r["repay_raw"] = max(Decimal(0), repay)
            r["add_collateral_raw"] = max(Decimal(0), add_col_raw)
        return r

    def level(self, hf: Decimal | None) -> str:
        if hf is None:
            return "NO_DEBT"
        if hf < 1:
            return "LIQUIDATABLE"
        if hf < Decimal(str(self.critical)):
            return "CRITICAL"
        if hf < Decimal(str(self.warn)):
            return "WARNING"
        return "OK"


def describe(r: dict, label: str, lvl: str, wd: Watchdog) -> str:
    ld, cd, ls, cs = r["loan_decimals"], r["collateral_decimals"], r["loan"], r["collateral_symbol"]
    hf = r["health_factor"]
    head = f"[{now_utc()}] {lvl:<12} {label}  {cs}/{ls} market {r['market'][:10]}…  borrower {r['borrower']}"
    if hf is None:
        return head + f"\n    no debt (collateral {fmt(r['collateral_raw'], cd)} {cs}) — nothing to liquidate"
    lines = [head,
             f"    HF {hf:.4f}  (warn < {wd.warn}, critical < {wd.critical}, liquidation < 1.0; LLTV {r['lltv']:.1%})"
             + (f"  [what-if: oracle price −{r['price_shock_pct']:g}%]" if r["price_shock_pct"] else ""),
             f"    collateral {fmt(r['collateral_raw'], cd)} {cs} ≈ {fmt(r['collateral_value_raw'], ld, 2)} {ls}"
             f"   debt {fmt(r['debt_raw'], ld)} {ls}   borrow APR {r['borrow_rate_apr']:.2%}",
             f"    {cs} price {r['collateral_price']:,.6f} {ls}; liquidation at {r['liquidation_price']:,.6f} {ls}"
             + (f" ({r['drop_to_liquidation_pct']:.1f}% lower)" if hf >= 1 else " (price is already BELOW the liquidation price)")]
    if lvl in ("WARNING", "CRITICAL", "LIQUIDATABLE"):
        lines.append(f"    ACTION: repay {fmt(r['repay_raw'], ld, 2)} {ls} OR add {fmt(r['add_collateral_raw'], cd, 4)} {cs} collateral"
                     f" to bring HF back to {wd.target}"
                     + ("  — position can be liquidated NOW" if lvl == "LIQUIDATABLE" else ""))
        lines.append(f"    borrower {EXPLORER}/address/{r['borrower']}  ·  Morpho {EXPLORER}/address/{MORPHO}")
    else:
        lines.append(f"    (to reach HF {wd.target} if needed: repay {fmt(r['repay_raw'], ld, 2)} {ls} or add "
                     f"{fmt(r['add_collateral_raw'], cd, 4)} {cs}; already above)" if r["repay_raw"] > 0 else
                     f"    healthy: no action needed (HF already ≥ target {wd.target})")
    return "\n".join(lines)


def load_positions(path: str | None, market: str | None, borrower: str | None) -> list[dict]:
    if market or borrower:
        if not (market and borrower):
            raise ValueError("--market and --borrower go together")
        items = [{"market": market, "borrower": borrower, "label": "cli"}]
    else:
        items = json.load(open(path))["positions"]
    out = []
    for it in items:
        if not ID_RE.match(it.get("market", "")):
            raise ValueError(f"bad market id {it.get('market')!r} (expected 0x + 64 hex)")
        out.append({"market": it["market"].lower(), "borrower": check_address(it["borrower"]),
                    "label": str(it.get("label", ""))[:60]})
    if not out:
        raise ValueError("no positions configured")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Morpho Blue liquidation watchdog for Pharos mainnet (read-only, Chain MCP).")
    ap.add_argument("--config", default="positions.json", help="JSON with {\"positions\": [{market, borrower, label}]}")
    ap.add_argument("--market", help="single market id (bytes32) instead of --config")
    ap.add_argument("--borrower", help="borrower address for --market")
    ap.add_argument("--warn", type=float, default=1.3, help="WARNING below this health factor (default 1.3)")
    ap.add_argument("--critical", type=float, default=1.1, help="CRITICAL below this health factor (default 1.1)")
    ap.add_argument("--target-hf", type=float, default=1.5, help="HF the suggested repay/add-collateral restores (default 1.5)")
    ap.add_argument("--price-shock", type=float, default=0.0, help="what-if: lower every oracle price by this %% (default 0 = real prices)")
    ap.add_argument("--follow", action="store_true", help="keep polling")
    ap.add_argument("--interval", type=int, default=60, help="seconds between polls in --follow (>= 10, default 60)")
    ap.add_argument("--polls", type=int, default=0, help="stop after N polls in --follow (0 = forever)")
    ap.add_argument("--alert-log", default="alerts.jsonl", help="append alerts as JSON lines here ('' to disable)")
    ap.add_argument("--json", help="write the last poll's readings to this JSON file")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    a = ap.parse_args(argv)
    try:
        positions = load_positions(a.config, a.market, a.borrower)
        wd = Watchdog(PharosMCP(a.endpoint), a.warn, a.critical, a.target_hf, a.price_shock)
        if a.interval < 10:
            raise ValueError("--interval must be >= 10 s")
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as e:
        ap.error(str(e))
    last_level: dict[tuple, str] = {}
    poll = 0
    while True:
        poll += 1
        block = wd.m.block_number()
        ts = int(wd.m.call_tool("get_block", {"block": block})["timestamp"], 16)
        print(f"=== poll {poll} · Pharos mainnet block #{block:,} "
              f"({datetime.fromtimestamp(ts, timezone.utc):%Y-%m-%d %H:%M:%S} UTC) · Morpho {MORPHO}"
              + (f" · WHAT-IF oracle prices −{a.price_shock:g}%" if a.price_shock else ""))
        readings, counts = [], {}
        for p in positions:
            try:
                r = wd.read(p["market"], p["borrower"], block, ts)
            except (MCPError, ValueError) as e:
                print(f"[{now_utc()}] ERROR {p['label']} {p['market'][:10]}… {p['borrower']}: {e}")
                continue
            lvl = wd.level(r["health_factor"])
            counts[lvl] = counts.get(lvl, 0) + 1
            key = (p["market"], p["borrower"])
            changed = last_level.get(key) != lvl
            last_level[key] = lvl
            if a.follow and poll > 1 and not changed and lvl in ("OK", "NO_DEBT"):
                continue  # in follow mode, only print healthy positions when their level changes
            print(describe(r, p["label"], lvl, wd))
            r.update(level=lvl, label=p["label"], time_utc=now_utc())
            readings.append(r)
            if a.alert_log and lvl in ("WARNING", "CRITICAL", "LIQUIDATABLE"):
                with open(a.alert_log, "a") as f:
                    f.write(json.dumps({k: (str(v) if isinstance(v, Decimal) else v) for k, v in r.items()}) + "\n")
        print("--- summary: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + "\n")
        if a.json:
            with open(a.json, "w") as f:
                json.dump(readings, f, indent=2, default=str)
        if not a.follow or (a.polls and poll >= a.polls):
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
