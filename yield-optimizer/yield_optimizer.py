#!/usr/bin/env python3
"""Yield Optimizer for Pharos mainnet: compare live yields, net out costs, recommend a move or "stay put".

    python yield_optimizer.py --token USDC --wallet 0xWALLET                   # check every USDC position the wallet has
    python yield_optimizer.py --token USDC --amount 25000 --wallet 0xWALLET     # idle USDC in the wallet: where to put it?
    python yield_optimizer.py --token WPROS --wallet 0xWALLET --hold-days 14 --prepare prepared/

1. Compare: every venue for the token — Avalon reserve, each Morpho Blue market lending it, each ERC-4626 vault
   holding it — with live on-chain APY, size, exit liquidity and deposit headroom (one pinned block, Chain MCP).
2. Cost: gas for withdraw + approve + deposit (estimate_gas from the wallet where possible, else the median gas of
   recent real txs on that contract, else a labelled default) priced with eth_gasPrice and Chainlink PROS/USD;
   entry/exit fees measured with previewDeposit/previewRedeem; minimum holding period (--hold-days).
3. Decide: MOVE only if the extra yield over the holding period, using the destination's post-deposit APY, beats
   all costs by --min-gain-usd and the APY gap is at least --min-apy-diff; otherwise STAY PUT with the reason.
4. Prepare (optional, --prepare DIR): the unsigned transactions (withdraw, approve, deposit) as JSON for the user
   to sign in their own wallet. The agent never holds keys, never signs and never broadcasts.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal

from pharos_explorer import PharosExplorer
from pharos_mcp import DEFAULT_ENDPOINT, MCPError, PharosMCP, check_address
from venues import Chain, Erc4626Vault, amt, discover, resolve_token, short, simulate, to_raw

__version__ = "1.0.0"


def pct(x) -> str:
    return "  n/a " if x is None else f"{x * 100:6.2f}%"


def usd(x) -> str:
    if x is None:
        return "n/a"
    if x and abs(x) < Decimal("0.01"):
        return f"{'−' if x < 0 else ''}${abs(x):.4f}"
    return f"{'−' if x < 0 else ''}${abs(x):,.2f}"


def days(d) -> str:
    if d is None:
        return "never"
    if d < Decimal(1) / 24:
        return f"{max(d * 1440, Decimal('0.1')):.1f} min"
    return f"{d * 24:.1f} h" if d < 1 else f"{d:.1f} d"


def table(ch, venues, price) -> list[str]:
    L = [f"{'#':>2}  {'venue':<52} {'APY':>7}  {'size':>16}  {'exit liquidity':>16}  {'deposit room':>14}"]
    for i, v in enumerate(sorted(venues, key=lambda v: -(v.apy or Decimal(-1))), 1):
        liq = f"{v.liquidity:,.0f}" if v.liquidity is not None else "per holder*"
        room = "no cap" if v.headroom is None else f"{v.headroom:,.0f}"
        L.append(f"{i:>2}  {(v.protocol + ' · ' + v.label)[:52]:<52} {pct(v.apy)}  {v.tvl:>16,.2f}  {liq:>16}  {room:>14}")
        L.append(f"      basis: {v.apy_basis}")
        if v.notes:
            L.append(f"      {'; '.join(v.notes)}")
    L.append(f"  sizes in {venues[0].sym if venues else ''}; * vault exit is checked by simulating redeem() from the holder")
    return L


class Optimizer:
    def __init__(self, ch: Chain, venues: list, a):
        self.ch, self.venues, self.a = ch, venues, a
        self.price = None

    def gas_usd(self, units: int) -> Decimal:
        return Decimal(units * self.ch.gas_price) / Decimal(10 ** 18) * self.ch.pros_usd

    def last_deposit(self, src, wallet: str):
        """Newest transfer of the token from the wallet into the venue's contract (explorer index) -> (days, block, tx)."""
        dest = {getattr(src, "a_token", None), src.address}
        try:
            _, rows = self.ch.ex.token_transfers(wallet, 1000)
        except Exception as e:
            return None, f"explorer unavailable ({str(e)[:80]})"
        for r in rows:   # newest first
            if (r.get("token_address", "").lower() == src.asset and r.get("from_address", "").lower() == wallet
                    and r.get("to_address", "").lower() in dest):
                b = int(r["block_number"])
                ts = int(str(self.ch.m.call_tool("get_block", {"block": b})["timestamp"]), 0)
                return (Decimal(self.ch.ts - ts) / 86400, b, r["transaction_hash"]), None
        return None, "no deposit transfer from this wallet found in the explorer index"

    def eligible(self, v) -> tuple[bool, str]:
        if v.apy is None:
            return False, "no reliable APY"
        tvl_usd = self.ch.usd(v.asset, v.tvl)
        if tvl_usd is not None and tvl_usd < self.a.min_tvl_usd:
            return False, f"too small (size {usd(tvl_usd)} < --min-tvl-usd {self.a.min_tvl_usd:,.0f})"
        if v.headroom is not None and v.headroom <= 0:
            return False, "no deposit room (cap reached / frozen)"
        return True, ""

    def evaluate(self, src, holding: Decimal, wallet: str | None) -> dict:
        """src = a Venue the wallet is in, or None for idle tokens in the wallet."""
        a, ch = self.a, self.ch
        token = self.venues[0].asset if self.venues else None
        dec = self.venues[0].dec
        src_apy = src.apy if src is not None else Decimal(0)
        src_name = f"{src.protocol} · {src.label}" if src else "idle in wallet"
        out = {"from": src_name, "from_apy": None if src_apy is None else float(src_apy), "amount": str(holding),
               "candidates": [], "skipped": []}
        if src is not None and src_apy is None:
            out.update(decision="STAY PUT", reason="the current venue's APY can't be measured, so a gain can't be computed")
            return out
        # exit check + exit cost from the current venue (same for every destination)
        exit_gas, exit_how, exit_ok, exit_msg, exit_fee_bps = 0, "none (tokens already in the wallet)", True, "", Decimal(0)
        wtx = None
        movable = holding
        out["partial_exit"] = None
        if src is None and wallet:
            bal = amt(int(ch.call(token, "balanceOf(address)", "0" * 24 + wallet[2:]), 16), dec)
            if bal < holding:
                out["wallet_note"] = (f"the wallet holds only {bal:,.4f} {self.venues[0].sym} now — "
                                      "prepared deposits would revert until it is funded")
        if src is not None:
            everything = True
            if src.liquidity is not None and src.liquidity < holding:
                # pool venue short of cash: only part of the position can leave now
                movable = (src.liquidity * Decimal("0.99")).quantize(Decimal(1) / (Decimal(10) ** min(dec, 6)))
                everything = False
                out["partial_exit"] = (f"only {src.liquidity:,.2f} {src.sym} is withdrawable in {src_name} right now "
                                       f"(the rest is lent out); evaluating a partial move of {movable:,.2f} (99% of it)")
            wtx = src.build_withdraw(wallet, to_raw(movable, dec), everything)[0] if wallet and movable > 0 else None
            if wtx:
                exit_ok, exit_msg = simulate(ch, wtx)
            elif movable <= 0:
                exit_ok, exit_msg = False, f"nothing withdrawable in {src_name} right now"
            exit_gas, exit_how = src.gas("withdraw", wtx if exit_ok else None)
            if isinstance(src, Erc4626Vault):
                _, exit_fee_bps, _ = src.fees(holding)
        for v in self.venues:
            if v is src:
                continue
            ok, why = self.eligible(v)
            if not ok:
                out["skipped"].append(f"{v.protocol} · {v.label}: {why}")
                continue
            if any(x.lower() in f"{v.protocol} {v.label} {v.address} {' '.join(v.notes)}".lower() for x in a.exclude):
                out["skipped"].append(f"{v.protocol} · {v.label}: excluded by --exclude")
                continue
            move = movable if v.headroom is None else min(movable, v.headroom)
            if move <= 0:
                continue
            new_apy = v.apy_after(move)
            entry_bps, _, fee_how = v.fees(move)
            dep = v.build_deposit(wallet or "0x" + "0" * 40, to_raw(move, dec))
            appr_gas, appr_how = (v.gas("approve", dep[0]) if wallet else (60_000, "assumed default"))
            if appr_how.startswith("median") or appr_how.startswith("assumed"):
                appr_gas, appr_how = 60_000, "assumed default (approve)"
            dep_gas, dep_how = v.gas("deposit", None)   # can't simulate before the approve is mined
            gas_units = exit_gas + appr_gas + dep_gas
            mv_usd = ch.usd(token, move) or Decimal(0)
            fee_usd = mv_usd * (entry_bps + exit_fee_bps) / 10_000
            gas_cost = self.gas_usd(gas_units)
            cost = gas_cost + fee_usd
            gain_year = mv_usd * (new_apy - src_apy)
            gain_hold = gain_year * a.hold_days / 365
            net = gain_hold - cost
            be = (cost / (gain_year / 365)) if gain_year > 0 else None
            out["candidates"].append({
                "venue": v, "to": f"{v.protocol} · {v.label}", "move": move, "apy_now": v.apy, "apy_after": new_apy,
                "gain_year": gain_year, "gain_hold": gain_hold, "gas_units": gas_units, "gas_usd": gas_cost,
                "gas_detail": f"withdraw {exit_gas:,} ({exit_how}); approve {appr_gas:,} ({appr_how}); deposit {dep_gas:,} ({dep_how})",
                "fee_bps": entry_bps + exit_fee_bps, "fee_usd": fee_usd, "fee_detail": fee_how, "cost": cost,
                "net": net, "breakeven_days": be, "deposit_txs": dep})
        out["candidates"].sort(key=lambda c: -c["net"])
        age = None
        if src is not None and wallet and a.min_age_days > 0:
            age, why = self.last_deposit(src, wallet)
            out["held_since"] = (f"last deposit {days(age[0])} ago (block {age[1]:,}, https://www.pharosscan.xyz/tx/{age[2]})"
                                 if age else f"unknown — {why}")
        out["exit_ok"], out["exit_msg"], out["withdraw_tx"] = exit_ok, exit_msg, wtx
        if not out["candidates"]:
            out.update(decision="STAY PUT", reason="no other eligible venue for this token")
            return out
        best = out["candidates"][0]
        reasons = []
        diff = best["apy_after"] - src_apy
        if diff * 100 < Decimal(str(a.min_apy_diff)):
            reasons.append(f"best alternative ({best['to']}) would pay {pct(best['apy_after']).strip()} after your deposit vs "
                           f"{pct(src_apy).strip()} now — a gap of {diff * 100:.2f} pts, below --min-apy-diff {a.min_apy_diff}")
        if best["net"] < Decimal(str(a.min_gain_usd)):
            reasons.append(f"expected net gain over the {a.hold_days}-day holding period is {usd(best['net'])} "
                           f"(gain {usd(best['gain_hold'])} − costs {usd(best['cost'])}), below --min-gain-usd {a.min_gain_usd}")
        if best["breakeven_days"] is not None and best["breakeven_days"] > a.hold_days:
            reasons.append(f"costs take {best['breakeven_days']:.0f} days to earn back, longer than the {a.hold_days}-day holding period")
        if age and age[0] < a.min_age_days:
            reasons.append(f"minimum holding period not reached: the current position was last topped up {days(age[0])} ago, "
                           f"less than --min-age-days {a.min_age_days} (avoids churning)")
        if not exit_ok:
            reasons.append(f"withdrawal from the current venue would fail right now ({exit_msg})")
        out["best"] = best
        if reasons:
            out.update(decision="STAY PUT", reason="; ".join(reasons))
        else:
            part = "" if best["move"] == holding else (f" (partial: {best['move']:,.2f} of {holding:,.2f} — "
                + ("exit liquidity limit)" if out["partial_exit"] else "destination cap)"))
            out.update(decision="MOVE" if src is not None else "DEPOSIT", reason=f"{'move' if src is not None else 'deposit'} {best['move']:,.2f} {self.venues[0].sym}{part} to {best['to']}: "
                       f"{pct(best['apy_after']).strip()} expected after your deposit vs {pct(src_apy).strip()} now → "
                       f"+{usd(best['gain_hold'])} over {a.hold_days} days, costs {usd(best['cost'])}, "
                       f"net {usd(best['net'])}, break-even in {days(best['breakeven_days'])}")
        return out


def render(r: dict, a, sym: str) -> list[str]:
    L = [f"── {r['from']} · {Decimal(r['amount']):,.4f} {sym} · current APY {pct(None if r['from_apy'] is None else Decimal(str(r['from_apy']))).strip()}"]
    if r.get("partial_exit"):
        L.append(f"   exit liquidity: {r['partial_exit']}")
    if r.get("wallet_note"):
        L.append(f"   wallet: {r['wallet_note']}")
    if r.get("held_since"):
        L.append(f"   held since: {r['held_since']}")
    if r.get("exit_msg") and not r.get("exit_ok", True):
        L.append(f"   exit check: FAILS — {r['exit_msg']}")
    elif r.get("withdraw_tx"):
        L.append("   exit check: withdraw simulated with eth_call from the wallet → ok")
    for c in r["candidates"][:4]:
        be = days(c["breakeven_days"])
        L.append(f"   → {c['to'][:56]:<56} APY {pct(c['apy_now']).strip()} now / {pct(c['apy_after']).strip()} after deposit · "
                 f"gain {usd(c['gain_hold'])} in {a.hold_days} d · gas {usd(c['gas_usd'])} ({c['gas_units']:,} gas) · "
                 f"fees {c['fee_bps']:.1f} bps {usd(c['fee_usd'])} · net {usd(c['net'])} · break-even {be}")
    if r.get("best"):
        L.append(f"   gas detail (best): {r['best']['gas_detail']}")
        L.append(f"   fee detail (best): {r['best']['fee_detail']}")
    if r["skipped"]:
        L.append(f"   not considered: {'; '.join(r['skipped'])}")
    L.append(f"   ⇒ {r['decision']}: {r['reason']}")
    return L


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Compare live Pharos yields and recommend (or prepare) a rebalance.")
    ap.add_argument("--token", default="USDC", help="USDC, WPROS, WETH or a token address (default USDC)")
    ap.add_argument("--wallet", help="wallet to analyse (positions are read on chain)")
    ap.add_argument("--amount", type=Decimal, help="idle amount in the wallet to place (instead of / besides positions)")
    ap.add_argument("--hold-days", type=int, default=30, help="minimum holding period after a move, days (default 30)")
    ap.add_argument("--min-age-days", type=Decimal, default=Decimal("7"), help="don't move a position topped up less than this many days ago (default 7; 0 = off)")
    ap.add_argument("--min-gain-usd", type=Decimal, default=Decimal("5"), help="minimum net gain over the holding period (default $5)")
    ap.add_argument("--min-apy-diff", type=Decimal, default=Decimal("0.25"), help="minimum APY gap in percentage points (default 0.25)")
    ap.add_argument("--min-tvl-usd", type=Decimal, default=Decimal("10000"), help="ignore venues smaller than this (default $10,000)")
    ap.add_argument("--vault", action="append", default=[], help="extra ERC-4626 vault address to consider (repeatable)")
    ap.add_argument("--exclude", action="append", default=[], help="skip venues whose name/address/notes contain this text (e.g. a collateral symbol); repeatable")
    ap.add_argument("--prepare", metavar="DIR", help="write unsigned transactions for every MOVE to DIR (never signed/sent)")
    ap.add_argument("--json", metavar="FILE", help="write the full result as JSON")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    a = ap.parse_args(argv)
    try:
        token = resolve_token(a.token)
        wallet = check_address(a.wallet) if a.wallet else None
        if a.amount is not None and a.amount <= 0:
            raise ValueError("--amount must be positive")
        if not 1 <= a.hold_days <= 3650:
            raise ValueError("--hold-days must be 1..3650")
        if a.min_age_days < 0 or a.min_gain_usd < 0 or a.min_apy_diff < 0 or a.min_tvl_usd < 0:
            raise ValueError("thresholds must be >= 0")
        if not wallet and a.amount is None:
            raise ValueError("give --wallet and/or --amount")
        if a.amount is not None and not wallet:
            print("note: no --wallet: costs use default gas figures and no transactions can be prepared", file=sys.stderr)
        ch = Chain(PharosMCP(a.endpoint), PharosExplorer())
    except (ValueError, MCPError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    venues, down = discover(ch, token, a.vault)
    sym = ch.token(token)[0]
    print(f"=== Yield Optimizer · {sym} on Pharos mainnet · block #{ch.block:,} · gas price {ch.gas_price / 1e9:g} gwei · "
          f"PROS ${ch.pros_usd:.4f} (Chainlink) · holding period {a.hold_days} d")
    if len(venues) < 1:
        print("no venues found for this token")
        return 2
    print(f"\n{len(venues)} yield sources compared (live, same block):")
    print("\n".join(table(ch, venues, None)))
    if down:
        print("unavailable: " + "; ".join(f"{d['venue']}: {d['error']}" for d in down))
    opt = Optimizer(ch, venues, a)
    holdings = []
    if wallet:
        for v in venues:
            bal, raw = v.position(wallet)
            if raw and (ch.usd(token, bal) or Decimal(1)) >= Decimal("1"):
                holdings.append((v, bal))
    if a.amount is not None:
        holdings.append((None, a.amount))
    if not holdings:
        print(f"\n{wallet} holds no {sym} in these venues; pass --amount to place idle {sym}.")
    results = []
    print()
    for src, bal in holdings:
        r = opt.evaluate(src, bal, wallet)
        results.append(r)
        print("\n".join(render(r, a, sym)))
        if r["decision"] in ("MOVE", "DEPOSIT") and a.prepare and wallet:
            os.makedirs(a.prepare, exist_ok=True)
            best = r["best"]
            txs = ([r["withdraw_tx"]] if r.get("withdraw_tx") else []) + best["deposit_txs"]
            sims = []
            for i, t in enumerate(txs):
                if i == 0 or t is best["deposit_txs"][0]:      # withdraw and approve can be simulated now
                    ok, msg = simulate(ch, t)
                    sims.append(f"simulated at block {ch.block:,}: {'ok' if ok else 'REVERTS: ' + msg}")
                else:
                    sims.append("not simulated: needs the approve above to be mined first")
            name = os.path.join(a.prepare, f"move_{len(results)}_{sym}_{short(best['venue'].address).replace('…', '_')}.json")
            plan = {"chainId": 1672, "from": wallet, "block": ch.block, "decision": r["reason"],
                    "signing": "UNSIGNED. Review, then sign and send each transaction in order with your own wallet; "
                               "set nonce, gas limit and gas price there. This tool never signs or broadcasts.",
                    "transactions": [{**t, "step": i + 1, "check": s} for i, (t, s) in enumerate(zip(txs, sims))]}
            json.dump(plan, open(name, "w"), indent=2)
            print(f"   prepared {len(txs)} unsigned txs → {name}")
            for t, s in zip(plan["transactions"], sims):
                print(f"     {t['step']}. to {t['to']} · {t['description']} · {s}")
    print("\nCaveats: rates are current on-chain values and move every block; the post-deposit APY is an estimate "
          "(pool venues: utilisation falls with your deposit, borrow rate held; vaults: yield diluted pro rata). "
          "Past vault share-price growth does not guarantee future yield. The ranking is by yield net of costs only: "
          "collateral, LLTV and curator risk differ per venue (shown in the notes) — use --exclude for venues you "
          "don't accept. Not financial advice.")
    if a.json:
        def clean(o):
            if isinstance(o, Decimal):
                return str(o)
            if hasattr(o, "summary"):
                return o.key
            raise TypeError(type(o))
        json.dump({"token": sym, "block": ch.block, "gas_price_wei": ch.gas_price, "pros_usd": str(ch.pros_usd),
                   "venues": [v.summary() for v in venues], "unavailable": down, "results": results},
                  open(a.json, "w"), indent=2, default=clean)
    return 0


if __name__ == "__main__":
    sys.exit(main())
