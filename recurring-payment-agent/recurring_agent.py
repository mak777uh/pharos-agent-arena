#!/usr/bin/env python3
"""Recurring Payment Agent for Pharos mainnet (prepare-only).

Manages repeating on-chain payments (rent, subscription, payroll) on a weekly or monthly schedule. On every
`run` it works out which periods are due, checks the chain (Pharos Chain MCP) for a payment that already
happened, and otherwise prepares ONE unsigned EIP-1559 transaction per period for the payer to sign. Every
period is tracked as scheduled / pending / paid / failed / insufficient_funds, with each attempt kept in a
local JSON state file. Never signs, never sends, never holds a key.

Double-payment guards:
  * a period is settled by an on-chain Transfer(payer -> recipient, exact amount) found after its due time,
    whoever sent it; a transfer is attributed to at most one period;
  * while an attempt is pending no second transaction is prepared for that period;
  * a pending attempt only becomes `failed` when it can no longer be mined: its receipt reverted, or the signer's
    nonce was used by another transaction and no matching transfer exists;
  * insufficient balance / allowance records `insufficient_funds` and prepares nothing (retried next run).

Commands: add, list, run, confirm, history  (see --help of each).
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pay_core as pc

__version__ = "1.0.0"
STATE = "recurring_state.json"


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def parse_dt(s: str) -> datetime:
    s = s.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s if "T" in s or " " in s else s + "T00:00:00+00:00")
    if dt.tzinfo is None:
        raise ValueError(f"time {s!r} needs a timezone (e.g. 2026-10-15T09:00Z)")
    return dt.astimezone(timezone.utc)


def due_at(s: dict, k: int) -> datetime:
    a = parse_dt(s["start"])
    if s["every"] == "weekly":
        return a + timedelta(weeks=k)
    m = a.month - 1 + k
    y, m = a.year + m // 12, m % 12 + 1
    d = min(a.day, calendar.monthrange(y, m)[1])  # 31st -> last day of shorter months
    return a.replace(year=y, month=m, day=d)


def period_key(s: dict, k: int) -> str:
    return due_at(s, k).strftime("%Y-%m-%d")


# ------------------------------------------------------------------ state
def load(path: str) -> dict:
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"version": 1, "schedules": {}, "used_txs": []}


def save(st: dict, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, path)  # atomic: a crash never leaves a half-written state


def log(msg: str) -> None:
    print(f"[{iso(now_utc())}] {msg}")


# ------------------------------------------------------------------ chain-facing logic
class Agent:
    def __init__(self, st: dict, chain: pc.Chain, out_dir: str, scan_blocks: int):
        self.st, self.c, self.out_dir, self.scan_blocks = st, chain, out_dir, scan_blocks

    def find_payment(self, s: dict, after_ts: int, from_block: int | None = None) -> dict | None:
        """First unattributed Transfer(payer -> to) of exactly `amount` at/after after_ts."""
        tok = self.c.token(s["token"])
        latest = self.c.block_number()
        start = from_block if from_block is not None else self.c.block_at(after_ts, latest)
        start = max(start, latest - self.scan_blocks)
        for t in self.c.transfers(tok, s["payer"], s["to"], start, latest):
            if t["amount"] == int(s["amount_units"]) and t["tx"] not in self.st["used_txs"]:
                return t
        return None

    def settle_paid(self, s: dict, per: dict, t: dict, how: str) -> None:
        per["status"], per["tx"], per["paid_block"], per["settled_by"] = "paid", t["tx"], t["block"], how
        self.st["used_txs"].append(t["tx"])
        for a in per["attempts"]:
            if a["status"] != "pending":
                continue
            tx = self.c.m.call_tool("get_transaction", {"hash": t["tx"]})
            if isinstance(tx, dict) and tx.get("from", "").lower() == s["signer"] and pc.h2i(tx.get("nonce", -1)) == a["nonce"]:
                a["status"] = "mined"
            else:  # paid by some other tx: this unsigned attempt must never be signed now
                a["status"] = "void"
                a["reason"] = "period already paid by another tx - DO NOT sign this attempt"
                log(f"{s['id']} {per['period']}: attempt #{a['n']} is VOID - do not sign {a['file']}")
        log(f"{s['id']} {per['period']}: PAID by {pc.EXPLORER}/tx/{t['tx']} (block {t['block']:,}, {how})")

    def check_pending(self, s: dict, per: dict) -> None:
        a = per["attempts"][-1]
        t = self.find_payment(s, 0, from_block=a["prepared_block"])
        if t:
            return self.settle_paid(s, per, t, "matching transfer found after the attempt was prepared")
        cur = self.c.nonce(s["signer"], "latest")
        if cur > a["nonce"]:
            a["status"] = "failed"
            a["reason"] = (f"dropped: signer nonce is now {cur} > {a['nonce']} and no matching transfer exists, "
                           f"so this unsigned tx can never be mined")
            per["status"] = "failed"
            log(f"{s['id']} {per['period']}: attempt #{a['n']} FAILED ({a['reason']})")
        else:
            log(f"{s['id']} {per['period']}: attempt #{a['n']} still PENDING (nonce {a['nonce']} unused) - "
                f"not preparing another tx; sign {a['file']}")

    def prepare(self, s: dict, per: dict) -> None:
        n = len(per["attempts"]) + 1
        try:
            p = self.c.prepare(s["signer"], s["to"], s["token"], Decimal(s["amount"]),
                               reference=f"{s['id']}:{per['period']}",
                               pull_owner=s["payer"] if s["signer"] != s["payer"] else None)
        except pc.InsufficientBalance as e:
            per["attempts"].append({"n": n, "status": "insufficient_funds", "reason": str(e), "at": iso(now_utc())})
            per["status"] = "insufficient_funds"
            log(f"{s['id']} {per['period']}: INSUFFICIENT FUNDS - {e}; nothing prepared, will retry next run")
            return
        except pc.PaymentError as e:
            per["attempts"].append({"n": n, "status": "failed", "reason": f"validation: {e}", "at": iso(now_utc())})
            per["status"] = "failed"
            log(f"{s['id']} {per['period']}: cannot prepare - {e}")
            return
        os.makedirs(self.out_dir, exist_ok=True)
        f = os.path.join(self.out_dir, f"{s['id']}_{per['period']}_attempt{n}.json")
        with open(f, "w", encoding="utf-8") as fh:
            json.dump({"schedule": s["id"], "period": per["period"], "attempt": n, **p.to_json()}, fh, indent=2)
        per["attempts"].append({"n": n, "status": "pending", "nonce": p.tx["nonce"], "signing_hash": p.signing_hash,
                                "prepared_block": p.block, "file": f, "at": iso(now_utc())})
        per["status"] = "pending"
        log(f"{s['id']} {per['period']}: PREPARED attempt #{n} - {p.amount_str} {p.token.symbol} -> "
            f"{pc.to_checksum(s['to'])}, nonce {p.tx['nonce']}, gas {p.tx['gas']:,}, signing hash "
            f"{p.signing_hash[:18]}... -> {f} (unsigned; sign it in your wallet)")

    def run(self, now: datetime, lead: timedelta) -> None:
        for s in self.st["schedules"].values():
            if not s.get("active", True):
                continue
            k = 0
            while due_at(s, k) <= now + lead:
                key = period_key(s, k)
                per = s["periods"].setdefault(key, {"period": key, "due": iso(due_at(s, k)), "status": "scheduled",
                                                    "attempts": []})
                k += 1
                if per["status"] == "paid":
                    continue
                if per["status"] == "pending":
                    self.check_pending(s, per)
                if per["status"] in ("scheduled", "failed", "insufficient_funds"):
                    due_ts = int(due_at(s, k - 1).timestamp())
                    t = self.find_payment(s, due_ts - int(lead.total_seconds()))
                    if t:
                        self.settle_paid(s, per, t, "already paid on chain before preparing")
                        continue
                    late = "" if due_at(s, k - 1) > now else f" (due {per['due']})"
                    log(f"{s['id']} {per['period']}: due{late}, no payment on chain yet -> preparing")
                    self.prepare(s, per)


# ------------------------------------------------------------------ CLI
def show_history(st: dict, only: str | None, now: datetime) -> str:
    out = []
    for s in st["schedules"].values():
        if only and s["id"] != only:
            continue
        mode = "push (payer signs transfer)" if s["signer"] == s["payer"] else f"pull (operator {s['signer']} signs transferFrom)"
        out.append(f"== {s['id']}: {s['amount']} {s['token']} {s['every']} from {s['payer']} to {s['to']} - {mode}")
        out.append(f"   {'period':<11} {'due':<21} {'status':<19} detail")
        for key in sorted(s["periods"]):
            per = s["periods"][key]
            detail = ""
            if per["status"] == "paid":
                detail = f"{pc.EXPLORER}/tx/{per['tx']}"
            elif per["attempts"]:
                a = per["attempts"][-1]
                detail = f"attempt #{a['n']}: " + (f"nonce {a['nonce']}, {a['file']}" if a["status"] == "pending" else a.get("reason", ""))
            out.append(f"   {key:<11} {per['due']:<21} {per['status']:<19} {detail}")
            for a in per["attempts"][:-1] if per["status"] != "paid" else per["attempts"]:
                out.append(f"   {'':<11} {'':<21} {'  #' + str(a['n']) + ' ' + a['status']:<19} {a.get('reason', 'nonce ' + str(a.get('nonce')))}")
        k = 0
        while due_at(s, k) <= now:
            k += 1
        nxt = s["periods"].get(period_key(s, k))
        out.append(f"   next scheduled payment: {iso(due_at(s, k))} ({s['amount']} {s['token']})"
                   + (f" - already {nxt['status']}" if nxt else ""))
        open_ = [p["period"] for p in s["periods"].values() if p["status"] in ("pending", "failed", "insufficient_funds")]
        if open_:
            out.append(f"   open (not yet paid): {', '.join(sorted(open_))}")
    return "\n".join(out) or "(no schedules)"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--state", default=STATE, help=f"state file (default {STATE})")
    ap.add_argument("--endpoint", default=None, help="Chain MCP endpoint (https only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a_add = sub.add_parser("add", help="create a schedule")
    a_add.add_argument("--id", required=True)
    a_add.add_argument("--payer", required=True, help="wallet the tokens come from")
    a_add.add_argument("--to", required=True, help="recipient")
    a_add.add_argument("--token", default="USDC")
    a_add.add_argument("--amount", required=True)
    a_add.add_argument("--every", choices=["weekly", "monthly"], required=True)
    a_add.add_argument("--start", required=True, help="first due time, ISO with zone, e.g. 2026-10-15T09:00Z")
    a_add.add_argument("--signer", help="operator that signs transferFrom (pull mode); default = payer (push)")
    sub.add_parser("list", help="list schedules and the next due payment")
    a_run = sub.add_parser("run", help="process due periods (cron this, e.g. hourly)")
    a_run.add_argument("--lead-hours", type=float, default=1.0, help="prepare this long before the due time (default 1)")
    a_run.add_argument("--out-dir", default="prepared", help="where unsigned tx JSON files go")
    a_run.add_argument("--scan-blocks", type=int, default=20000, help="max blocks scanned back for payments")
    a_cf = sub.add_parser("confirm", help="settle a period with the hash of the tx you signed and sent")
    a_cf.add_argument("--id", required=True)
    a_cf.add_argument("--period", required=True, help="YYYY-MM-DD as shown by history")
    a_cf.add_argument("--tx", required=True)
    a_h = sub.add_parser("history", help="payment history + next scheduled payment")
    a_h.add_argument("--id")
    a = ap.parse_args(argv)
    st = load(a.state)
    now = now_utc()

    if a.cmd == "add":
        if a.id in st["schedules"]:
            print(f"schedule {a.id!r} exists", file=sys.stderr)
            return 2
        if not a.id.replace("-", "").replace("_", "").isalnum() or len(a.id) > 32:
            print("id: letters, digits, - and _ only (max 32)", file=sys.stderr)
            return 2
        try:
            payer, to = pc.check_address(a.payer, "--payer"), pc.check_address(a.to, "--to")
            signer = pc.check_address(a.signer, "--signer") if a.signer else payer
            amount = Decimal(a.amount)
            start = parse_dt(a.start)
            if amount <= 0:
                raise pc.PaymentError("amount must be positive")
            if payer == to:
                raise pc.PaymentError("payer and recipient are the same")
            chain = pc.Chain(a.endpoint)
            tok = chain.token(a.token)
            units = pc.to_units(amount, tok.decimals)
            if signer != payer and tok.address is None:
                raise pc.PaymentError("pull mode (--signer) needs an ERC-20 token")
            if tok.address is None:
                raise pc.PaymentError("native PROS schedules are not supported yet (no Transfer event to reconcile); use WPROS")
        except (ValueError, pc.PaymentError) as e:
            print(f"invalid schedule: {e}", file=sys.stderr)
            return 2
        st["schedules"][a.id] = {"id": a.id, "payer": payer, "to": to, "signer": signer, "token": tok.symbol.upper(),
                                 "amount": str(amount), "amount_units": str(units), "every": a.every,
                                 "start": start.isoformat(), "created": now.isoformat(), "active": True, "periods": {}}
        save(st, a.state)
        print(f"added {a.id}: {amount} {tok.symbol} {a.every} from {payer} to {to}, first due {iso(start)}"
              + (f", pull via operator {signer}" if signer != payer else ""))
        return 0
    if a.cmd == "list":
        for s in st["schedules"].values():
            k = 0
            while period_key(s, k) in s["periods"] and s["periods"][period_key(s, k)]["status"] == "paid":
                k += 1
            print(f"{s['id']:<14} {s['amount']:>10} {s['token']:<5} {s['every']:<8} to {s['to']}  next unpaid due {iso(due_at(s, k))}")
        return 0
    if a.cmd == "history":
        print(show_history(st, a.id, now))
        return 0

    chain = pc.Chain(a.endpoint)
    if a.cmd == "run":
        if not 0 <= a.lead_hours <= 24 * 7:
            print("--lead-hours must be 0..168", file=sys.stderr)
            return 2
        ag = Agent(st, chain, a.out_dir, a.scan_blocks)
        log(f"run: {len(st['schedules'])} schedule(s), block {chain.block_number():,}, lead {a.lead_hours} h")
        try:
            ag.run(now, timedelta(hours=a.lead_hours))
        finally:
            save(st, a.state)
        return 0
    if a.cmd == "confirm":
        s = st["schedules"].get(a.id)
        per = s and s["periods"].get(a.period)
        if not per:
            print("unknown schedule/period", file=sys.stderr)
            return 2
        if per["status"] == "paid":
            print(f"{a.id} {a.period} is already PAID by {per['tx']} - nothing to do (no double payment)")
            return 0
        h = a.tx.lower()
        if len(h) != 66 or not all(c in "0123456789abcdef" for c in h[2:]) or not h.startswith("0x"):
            print("--tx must be a 0x-prefixed 32-byte hash", file=sys.stderr)
            return 2
        if h in st["used_txs"]:
            print(f"REJECTED: {h} already settles another period")
            return 1
        r = chain.m.call_tool("get_transaction_receipt", {"hash": h})
        if not r or not isinstance(r, dict) or not r.get("status"):
            print("REJECTED: no receipt (not mined yet?) - try again later")
            return 1
        tok = chain.token(s["token"])
        ok_log = any(l["address"].lower() == tok.address and l["topics"][0] == pc.TRANSFER_TOPIC
                     and l["topics"][1][-40:] == s["payer"][2:] and l["topics"][2][-40:] == s["to"][2:]
                     and pc.h2i(l["data"]) == int(s["amount_units"]) for l in r.get("logs", []))
        if pc.h2i(r["status"]) == 0:
            if r["from"].lower() == s["signer"]:
                per["attempts"].append({"n": len(per["attempts"]) + 1, "status": "failed", "reason": f"tx {h} reverted"})
                per["status"] = "failed"
                save(st, a.state)
                print(f"{a.id} {a.period}: FAILED - {h} reverted on chain; next run prepares a new attempt")
                return 0
            print("REJECTED: reverted tx was not sent by this schedule's signer")
            return 1
        if not ok_log:
            print(f"REJECTED: {pc.EXPLORER}/tx/{h} does not transfer {s['amount']} {s['token']} from {s['payer']} to {s['to']}")
            return 1
        Agent(st, chain, "prepared", 0).settle_paid(s, per, {"tx": h, "block": pc.h2i(r["blockNumber"])}, "confirmed by user")
        save(st, a.state)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
