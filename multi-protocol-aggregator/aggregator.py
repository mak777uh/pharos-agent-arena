#!/usr/bin/env python3
"""Multi-Protocol Aggregator for Pharos mainnet: one interface over Avalon, Morpho Blue and ERC-4626 vaults.

    python aggregator.py rates --token USDC
    python aggregator.py positions --wallet 0xWALLET --token USDC --token WPROS
    python aggregator.py deposit  --wallet 0xWALLET --token USDC --amount 5000        # routed to the best venue
    python aggregator.py withdraw --wallet 0xWALLET --token USDC --amount 20000       # routed to the right venue(s)
    python aggregator.py ask 0xWALLET "deposit 5000 USDC"                             # same, in plain English
    python aggregator.py rates --token USDC --simulate-outage avalon                  # outage drill

Queries read live state through the hosted Pharos Chain MCP (https://mcp.pharos.xyz/mcp), pinned to one block.
Actions (deposit / withdraw) are routed to a venue with the reason printed, then PREPARED as unsigned transactions
(JSON) and simulated with eth_call from the wallet where possible. Nothing is signed or broadcast; the user signs
in their own wallet. Each protocol adapter is isolated: if one protocol fails, the others still answer and the
failure is reported.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from decimal import Decimal

from pharos_explorer import PharosExplorer
from pharos_mcp import DEFAULT_ENDPOINT, MCPError, PharosMCP, check_address
from venues import TOKENS, Chain, amt, discover, h2i, resolve_token, short, simulate, to_raw, w

__version__ = "1.0.0"
PROTOCOLS = ("avalon", "morpho", "vault")


def collapse(down: list[dict]) -> list[str]:
    """Group unavailable venues by protocol: 'Morpho Blue: 12 venues UNAVAILABLE — <first error>'."""
    groups: dict[str, list[str]] = {}
    for d in down:
        name = d["venue"]
        proto = "Morpho Blue" if name.startswith("Morpho") else "ERC-4626 vault" if name.startswith("ERC-4626") else name.split()[0]
        groups.setdefault(proto, []).append(d["error"])
    return [f"{p}: UNAVAILABLE ({len(e)} venue{'s' if len(e) > 1 else ''}) — {e[0]}" for p, e in groups.items()]


def pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:.2f}%"


class Aggregator:
    def __init__(self, ch: Chain, min_tvl_usd: Decimal, exclude: list[str]):
        self.ch, self.min_tvl_usd, self.exclude = ch, min_tvl_usd, [e.lower() for e in exclude]
        self._cache: dict = {}

    def venues(self, token: str):
        if token not in self._cache:
            self._cache[token] = discover(self.ch, token)
        return self._cache[token]

    def status(self, token: str) -> list[str]:
        vs, down = self.venues(token)
        by = {}
        for v in vs:
            by.setdefault(v.protocol, 0)
            by[v.protocol] += 1
        lines = [f"{p}: up ({n} venue{'s' if n > 1 else ''})" for p, n in by.items()]
        names = {"Avalon": "avalon", "Morpho Blue": "morpho", "ERC-4626 vault": "vault"}
        for p, k in names.items():
            if p not in by and not any(d["venue"].lower().startswith(p.split()[0].lower()) for d in down):
                lines.append(f"{p}: up (no venue for this token)")
        lines += collapse(down)
        return lines

    # ---------------------------------------------------------------- queries --
    def rates(self, token: str) -> dict:
        vs, down = self.venues(token)
        rows = sorted(vs, key=lambda v: -(v.apy if v.apy is not None else Decimal(-1)))
        return {"token": self.ch.token(token)[0], "venues": [v.summary() for v in rows], "unavailable": down,
                "status": self.status(token)}

    def positions(self, wallet: str, token: str) -> dict:
        vs, down = self.venues(token)
        sym = self.ch.token(token)[0]
        out = []
        idle = amt(h2i(self.ch.call(token, "balanceOf(address)", w(wallet))), self.ch.token(token)[1])
        for v in vs:
            try:
                bal, raw = v.position(wallet)
            except (MCPError, ValueError) as e:
                down.append({"venue": v.key, "error": f"position read failed: {str(e)[:120]}"})
                continue
            if raw:
                out.append({"venue": v.key, "address": v.address, "amount": bal, "apy": v.apy,
                            "usd": self.ch.usd(token, bal), "src": v.src[-2:]})
        return {"token": sym, "wallet_idle": idle, "positions": out, "unavailable": down, "status": self.status(token)}

    # ---------------------------------------------------------------- routing --
    def eligible(self, v, amount: Decimal) -> tuple[bool, str]:
        if any(e in f"{v.protocol} {v.label} {v.address} {' '.join(v.notes)}".lower() for e in self.exclude):
            return False, "excluded by --exclude"
        if v.apy is None:
            return False, "no reliable APY (no share-price history)"
        tvl = self.ch.usd(v.asset, v.tvl)
        if tvl is not None and tvl < self.min_tvl_usd:
            return False, f"too small (size ${tvl:,.2f} < ${self.min_tvl_usd:,.0f})"
        if v.headroom is not None and v.headroom < amount:
            return False, f"deposit cap room {v.headroom:,.2f} < {amount:,.2f}"
        return True, ""

    def route_deposit(self, wallet: str, token: str, amount: Decimal, venue: str | None) -> dict:
        vs, down = self.venues(token)
        sym, dec = self.ch.token(token)
        cands, rejected = [], []
        for v in vs:
            if venue and venue.lower() not in v.key.lower() and venue.lower() != v.address:
                continue
            ok, why = self.eligible(v, amount)
            if ok:
                cands.append((v.apy_after(amount), v))
            else:
                rejected.append(f"{v.key}: {why}")
        res = {"action": "deposit", "token": sym, "amount": str(amount), "wallet": wallet, "unavailable": down,
               "rejected": rejected}
        if not cands:
            res["result"] = "no eligible venue" + (f" matching --venue {venue}" if venue else "")
            return res
        cands.sort(key=lambda c: -c[0])
        best_apy, best = cands[0]
        why = [f"highest expected APY after your deposit: {pct(best_apy)} (now {pct(best.apy)}; {best.apy_basis})"]
        if len(cands) > 1:
            nxt_apy, nxt = cands[1]
            why.append(f"next best {nxt.key} would give {pct(nxt_apy)} after deposit "
                       f"(≈ ${(best_apy - nxt_apy) * (self.ch.usd(token, amount) or 0):,.2f}/year less)")
        why.append("capacity ok: " + ("no supply cap" if best.headroom is None else f"cap room {best.headroom:,.2f} {sym}"))
        if best.notes:
            why.append("venue facts: " + "; ".join(best.notes[:3]))
        if venue:
            why.insert(0, f"restricted to venues matching --venue {venue!r}")
        raw = to_raw(amount, dec)
        txs = best.build_deposit(wallet, raw)
        bal = h2i(self.ch.call(token, "balanceOf(address)", w(wallet)))
        allowance = h2i(self.ch.call(token, "allowance(address,address)", w(wallet) + w(best.spender)))
        checks = [f"wallet balance {amt(bal, dec):,.4f} {sym}" + (" — INSUFFICIENT, the deposit would revert" if bal < raw else " — enough")]
        if allowance >= raw:
            txs = txs[1:]
            ok, msg = simulate(self.ch, txs[0])
            checks.append(f"existing allowance {amt(allowance, dec):,.4f} covers it: approve skipped; deposit simulated → "
                          f"{'ok' if ok else 'REVERTS: ' + msg}")
        else:
            ok, msg = simulate(self.ch, txs[0])
            checks.append(f"approve simulated → {'ok' if ok else 'REVERTS: ' + msg}; deposit not simulated (needs the approve mined first)")
        res.update(result="routed", venue=best.key, venue_address=best.address, why=why, checks=checks, transactions=txs,
                   alternatives=[{"venue": v.key, "apy_after": str(a)} for a, v in cands[1:4]])
        return res

    def route_withdraw(self, wallet: str, token: str, amount: Decimal | None, venue: str | None) -> dict:
        vs, down = self.venues(token)
        sym, dec = self.ch.token(token)
        holds = []
        for v in vs:
            if venue and venue.lower() not in v.key.lower() and venue.lower() != v.address:
                continue
            try:
                bal, raw = v.position(wallet)
            except (MCPError, ValueError):
                continue
            if raw:
                cap = bal if v.liquidity is None else min(bal, v.liquidity)
                holds.append((v, bal, cap))
        res = {"action": "withdraw", "token": sym, "amount": "all" if amount is None else str(amount), "wallet": wallet,
               "unavailable": down}
        if not holds:
            res["result"] = f"the wallet has no {sym} in the reachable venues" + (f" matching --venue {venue}" if venue else "")
            return res
        # keep the best earners: take from the lowest-APY venue first; skip venues whose withdrawal would revert
        holds.sort(key=lambda h: (h[0].apy if h[0].apy is not None else Decimal(-1)))
        need = amount if amount is not None else sum(h[1] for h in holds)
        legs, why, notes = [], [], []
        for v, bal, cap in holds:
            if need <= 0:
                break
            take = min(need, cap)
            if take <= 0:
                notes.append(f"{v.key}: holds {bal:,.2f} but only {cap:,.2f} withdrawable now (the rest is lent out)")
                continue
            everything = take == bal and v.liquidity is None
            tx = v.build_withdraw(wallet, to_raw(take, dec), everything)[0]
            ok, msg = simulate(self.ch, tx)
            if not ok:
                notes.append(f"{v.key}: withdrawal simulation REVERTS ({msg}) — skipped")
                continue
            legs.append({"venue": v.key, "amount": str(take), "apy": None if v.apy is None else str(v.apy), "tx": tx,
                         "check": f"simulated at block {self.ch.block:,} → ok"})
            rank = "lowest" if not legs[:-1] else "next-lowest"
            why.append(f"{v.key}: take {take:,.4f} {sym} — {rank} APY among your withdrawable positions ({pct(v.apy)}), "
                       f"so higher-earning ones stay invested" + ("" if take == bal else f"; {bal - take:,.2f} stays here"))
            need -= take
        res.update(result="routed" if legs and need <= 0 else ("partial" if legs else "cannot withdraw now"),
                   why=why, notes=notes, transactions=[l["tx"] for l in legs], legs=legs,
                   shortfall=str(max(need, Decimal(0))))
        return res


# ------------------------------------------------------------- plain English --
ASK = [
    ("deposit", r"\b(deposit|supply|put|invest|lend|park)\b\s+([\d.,]+)\s*([a-z]+)"),
    ("withdraw", r"\b(withdraw|take out|pull|redeem|remove)\b\s+(all|everything|[\d.,]+)\s*(?:of\s+my\s+|my\s+)?([a-z]+)"),
    ("positions", r"\b(positions?|holdings?|what do i have|where is my|balances?)\b"),
    ("rates", r"\b(rates?|apy|apr|yields?|best (venue|place)|where (should|can) i)\b"),
]


def parse_ask(text: str) -> tuple[str, dict]:
    t = " ".join(text.lower().split())
    if not t or len(t) > 200:
        raise ValueError("ask: 1–200 characters please")
    for kind, rx in ASK:
        m = re.search(rx, t)
        if not m:
            continue
        if kind in ("deposit", "withdraw"):
            qty, tok = m.group(2), m.group(3).upper()
            if tok not in TOKENS:
                raise ValueError(f"unknown token {tok!r}; I know {', '.join(TOKENS)}")
            amount = None if qty in ("all", "everything") else Decimal(qty.replace(",", ""))
            if amount is not None and amount <= 0:
                raise ValueError("amount must be positive")
            return kind, {"token": tok, "amount": amount}
        tok = next((k for k in TOKENS if re.search(rf"\b{k.lower()}\b", t)), None)
        if tok is None and kind == "rates":
            tok = "USDC"
        return kind, {"token": tok}
    raise ValueError("I understand: rates/APY for <TOKEN> · my positions · deposit <N> <TOKEN> · "
                     "withdraw <N|all> <TOKEN>. I won't guess anything else.")


# ------------------------------------------------------------------- output --
def print_rates(r: dict):
    print(f"\n{r['token']} venues (best first):")
    for i, v in enumerate(r["venues"], 1):
        apy = "n/a" if v["apy"] is None else f"{v['apy'] * 100:.2f}%"
        liq = "per holder" if v["liquidity"] is None else f"{Decimal(v['liquidity']):,.0f}"
        room = "no cap" if v["headroom"] is None else f"{Decimal(v['headroom']):,.0f}"
        print(f"  {i:>2}. {v['key'][:58]:<58} APY {apy:>7}  size {Decimal(v['tvl']):>14,.2f}  exit liq {liq:>12}  room {room}")
    print_status(r["status"])


def print_status(st):
    print("  protocol status: " + " | ".join(st))


def print_positions(r: dict, ch: Chain):
    print(f"\n{r['token']} positions: idle in wallet {r['wallet_idle']:,.4f}")
    tot = r["wallet_idle"]
    for p in r["positions"]:
        tot += p["amount"]
        print(f"  - {p['venue'][:58]:<58} {p['amount']:>16,.4f}  APY {pct(p['apy']):>7}   [{'; '.join(p['src'])}]")
    print(f"  total {tot:,.4f} {r['token']}" + (f" (≈ ${ch.usd(TOKENS.get(r['token'], ''), tot):,.2f})" if ch.usd(TOKENS.get(r['token'], ''), tot) is not None else ""))
    print_status(r["status"])


def print_action(r: dict, prepare: str | None, block: int):
    print(f"\n{r['action'].upper()} {r['amount']} {r['token']} for {r['wallet']} → {r['result']}")
    if r.get("venue"):
        print(f"  routed to: {r['venue']} ({r['venue_address']})")
    for line in r.get("why", []):
        print(f"  why: {line}")
    for line in r.get("notes", []):
        print(f"  note: {line}")
    for line in r.get("checks", []):
        print(f"  check: {line}")
    if r.get("alternatives"):
        print("  alternatives: " + "; ".join(f"{a['venue']} {Decimal(a['apy_after']) * 100:.2f}%" for a in r["alternatives"]))
    for line in r.get("rejected", []):
        print(f"  not eligible: {line}")
    if r.get("shortfall") and Decimal(r["shortfall"]) > 0:
        print(f"  shortfall: {Decimal(r['shortfall']):,.4f} {r['token']} can't be withdrawn right now")
    for line in collapse(r.get("unavailable", [])):
        print(f"  unavailable: {line} → routed over the remaining protocols")
    txs = r.get("transactions") or []
    for i, t in enumerate(txs, 1):
        print(f"  tx {i} (unsigned): to {t['to']} · {t['description']} · data {t['data'][:74]}…")
    if txs and prepare:
        os.makedirs(prepare, exist_ok=True)
        name = os.path.join(prepare, f"{r['action']}_{r['token']}_{block}.json")
        json.dump({"chainId": 1672, "block": block, "from": r["wallet"], "route": r.get("venue") or [l["venue"] for l in r.get("legs", [])],
                   "why": r.get("why"), "signing": "UNSIGNED — sign and send in order with your own wallet (it sets nonce, "
                   "gas limit and fees). This tool never signs or broadcasts.", "transactions": txs}, open(name, "w"), indent=2)
        print(f"  prepared → {name}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="One interface over Avalon, Morpho Blue and ERC-4626 vaults on Pharos (read + prepare-only).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--simulate-outage", action="append", default=[], choices=PROTOCOLS,
                        help="drill: treat this protocol as unreachable (repeatable)")
    common.add_argument("--min-tvl-usd", type=Decimal, default=Decimal("10000"))
    common.add_argument("--exclude", action="append", default=[], help="skip venues containing this text (e.g. a collateral symbol)")
    common.add_argument("--prepare", metavar="DIR", help="write the unsigned transactions as JSON into DIR")
    common.add_argument("--json", metavar="FILE", help="write the result as JSON")
    common.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    p = sub.add_parser("rates", parents=[common]); p.add_argument("--token", default="USDC")
    p = sub.add_parser("positions", parents=[common]); p.add_argument("--wallet", required=True); p.add_argument("--token", action="append")
    for name in ("deposit", "withdraw"):
        p = sub.add_parser(name, parents=[common])
        p.add_argument("--wallet", required=True); p.add_argument("--token", required=True)
        p.add_argument("--amount", help="amount in token units (withdraw: omit or 'all' for everything)")
        p.add_argument("--venue", help="force a venue: text matching its name/key, or its address")
    p = sub.add_parser("ask", parents=[common]); p.add_argument("wallet"); p.add_argument("text", nargs="+")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "ask":
            kind, params = parse_ask(" ".join(a.text))
            print(f"understood: {kind} {json.dumps({k: str(v) if v is not None else None for k, v in params.items()})}")
            a.cmd, a.token, a.amount, a.venue = kind, params["token"], params.get("amount"), None
            if kind == "positions":
                a.token = [params["token"]] if params["token"] else None
        wallet = check_address(a.wallet) if getattr(a, "wallet", None) else None
        if a.cmd == "positions":
            tokens = [resolve_token(t) for t in (a.token or ["USDC", "WPROS"])]
        else:
            tokens = [resolve_token(a.token)]
        amount = None
        if a.cmd in ("deposit", "withdraw"):
            raw = a.amount
            if raw is not None and str(raw).lower() != "all":
                amount = Decimal(str(raw).replace(",", ""))
                if amount <= 0:
                    raise ValueError("amount must be positive")
            elif a.cmd == "deposit":
                raise ValueError("deposit needs --amount")
        ch = Chain(PharosMCP(a.endpoint), PharosExplorer(), set(a.simulate_outage))
    except (ValueError, ArithmeticError, MCPError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    agg = Aggregator(ch, a.min_tvl_usd, a.exclude)
    print(f"=== Pharos multi-protocol aggregator · mainnet block #{ch.block:,} · PROS ${ch.pros_usd:.4f} (Chainlink)"
          + (f" · OUTAGE DRILL: {', '.join(a.simulate_outage)}" if a.simulate_outage else ""))
    out = []
    if a.cmd == "rates":
        r = agg.rates(tokens[0]); print_rates(r); out.append(r)
    elif a.cmd == "positions":
        for t in tokens:
            r = agg.positions(wallet, t); print_positions(r, ch); out.append(r)
    elif a.cmd == "deposit":
        r = agg.route_deposit(wallet, tokens[0], amount, a.venue); print_action(r, a.prepare, ch.block); out.append(r)
    else:
        r = agg.route_withdraw(wallet, tokens[0], amount, a.venue); print_action(r, a.prepare, ch.block); out.append(r)
    print("\nNothing was signed or sent. Rates are live on-chain values at this block; not financial advice.")
    if a.json:
        json.dump(out, open(a.json, "w"), indent=2, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
