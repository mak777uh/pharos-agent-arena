#!/usr/bin/env python3
"""Audit-Log Agent for Pharos: a plain-language activity report for one wallet over a date range.

All chain data comes from the hosted Pharos Chain MCP server (https://mcp.pharos.xyz/mcp), read-only:

  get_block               block timestamps -> binary search maps the UTC date range to a block range
  get_logs                (<= 1000 blocks per call) token Transfer in/out, Approval / ApprovalForAll by the
                          wallet, WPROS-style Deposit/Withdrawal (wrap/unwrap), Uniswap V2/V3 Swap events
                          whose recipient is the wallet
  get_transaction_count   nonce at range start/end -> how many txs the wallet sent; a binary search on the
                          nonce finds the block of every sent tx (no indexer needed)
  get_block(+txs), get_transaction_receipt
                          the sent txs themselves: target, PROS value, status, fee, emitted events
  get_code, eth_call      contract vs wallet; token symbol()/decimals()
  get_balance             native PROS balance at range start and end

Every transaction is put in exactly one category: swap > transfer_out > transfer_in > approval >
contract_interaction. Output: console summary, Markdown report and CSV line items.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from pharos_mcp import DEFAULT_ENDPOINT, PharosMCP, check_address

EXPLORER = "https://www.pharosscan.xyz"
MAX_RANGE = 1000            # hosted get_logs window limit (inclusive)
WEI = 10 ** 18
UINT256_MAX = 2 ** 256 - 1

T_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"  # Transfer(address,address,uint256)
T_APPROVAL = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"  # Approval(address,address,uint256)
T_APPROVAL_ALL = "0x17307eab39ab6107e8899845ad3d59bd9653f200f220920489ca2b5937696c31"  # ApprovalForAll(address,address,bool)
T_DEPOSIT = "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c"  # Deposit(address,uint256)  (WPROS wrap)
T_WITHDRAWAL = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"  # Withdrawal(address,uint256) (unwrap)
SWAP_TOPICS = {  # known DEX swap events (topic0) seen on Pharos mainnet
    "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822": "Uniswap V2 Swap",
    "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67": "Uniswap V3 Swap",
    "0xc2c0245e056d5fb095f04cd6373bc770802ebd1e6c918eb78fdef843cdb37b0f": "DODO DODOSwap",
}
CATEGORIES = ["swap", "transfer_out", "transfer_in", "approval", "contract_interaction"]
CAT_TITLE = {"swap": "Swaps", "transfer_out": "Transfers out", "transfer_in": "Transfers in",
             "approval": "Approvals", "contract_interaction": "Other contract interactions"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?$")


def h2i(v) -> int:
    if v is None:
        return 0
    if isinstance(v, int):
        return v
    s = str(v)
    return int(s, 16) if s.startswith("0x") else int(s)


def pad(addr: str) -> str:
    return "0x" + "0" * 24 + addr[2:].lower()


def utc(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts))


def fmt(x: float) -> str:
    return f"{x:,.6f}".rstrip("0").rstrip(".") if abs(x) < 1e15 else f"{x:.3e}"


def parse_when(s: str, end: bool) -> int:
    """'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM[:SS]', always UTC. A bare end date means the end of that day."""
    if not DATE_RE.match(s or ""):
        raise ValueError(f"bad date {s!r}; use YYYY-MM-DD or YYYY-MM-DDTHH:MM (UTC)")
    s = s.replace(" ", "T")
    fmt_ = "%Y-%m-%dT%H:%M:%S" if s.count(":") == 2 else "%Y-%m-%dT%H:%M" if "T" in s else "%Y-%m-%d"
    dt = datetime.strptime(s, fmt_).replace(tzinfo=timezone.utc)
    if end and fmt_ == "%Y-%m-%d":
        dt += timedelta(days=1) - timedelta(seconds=1)
    return int(dt.timestamp())


class AuditAgent:
    def __init__(self, mcp: PharosMCP, wallet: str):
        self.m = mcp
        self.w = wallet
        self._ts: dict[int, int] = {}
        self._nonce: dict[int, int] = {}
        self._tok: dict[str, tuple[str, int | None]] = {}
        self._code: dict[str, bool] = {}
        self.calls_note: list[str] = []

    # ---- chain helpers -------------------------------------------------
    def ts(self, n: int) -> int:
        if n not in self._ts:
            self._ts[n] = h2i(self.m.call_tool("get_block", {"block": n})["timestamp"])
        return self._ts[n]

    def first_block_at_or_after(self, t: int, lo: int, hi: int) -> int:
        while lo < hi:  # smallest block with timestamp >= t (binary search on block timestamps)
            mid = (lo + hi) // 2
            if self.ts(mid) >= t:
                hi = mid
            else:
                lo = mid + 1
        return lo

    def date_range_to_blocks(self, t0: int, t1: int) -> tuple[int, int]:
        head = self.m.block_number()
        if t0 > self.ts(head):
            raise ValueError("start date is in the future")
        b0 = self.first_block_at_or_after(t0, 1, head)
        b1 = head if t1 >= self.ts(head) else self.first_block_at_or_after(t1 + 1, b0, head) - 1
        if b1 < b0:
            raise ValueError("no blocks in that date range")
        return b0, b1

    def nonce(self, b: int) -> int:
        if b not in self._nonce:
            self._nonce[b] = h2i(self.m.call_tool("get_transaction_count", {"address": self.w, "block": b})["nonce"])
        return self._nonce[b]

    def token(self, addr: str) -> tuple[str, int | None]:
        if addr not in self._tok:
            sym, dec = None, None
            try:
                out = self.m.eth_call(addr, "0x95d89b41")
                raw = bytes.fromhex(out[2:])
                if len(raw) >= 96:
                    sym = raw[64:64 + int.from_bytes(raw[32:64], "big")].decode("utf-8", "replace")
                elif len(raw) == 32:
                    sym = raw.rstrip(b"\0").decode("utf-8", "replace")
            except Exception:
                pass
            try:
                dec = h2i(self.m.eth_call(addr, "0x313ce567"))
            except Exception:
                pass
            self._tok[addr] = (sym or f"token {addr[:8]}…", dec)
        return self._tok[addr]

    def is_contract(self, addr: str) -> bool:
        if addr not in self._code:
            code = self.m.call_tool("get_code", {"address": addr})
            code = code.get("code", code) if isinstance(code, dict) else code
            self._code[addr] = code not in ("0x", "", None)
        return self._code[addr]

    def logs(self, topics: list, b0: int, b1: int) -> list:
        out = []
        for s in range(b0, b1 + 1, MAX_RANGE):
            out += self.m.call_tool("get_logs", {"topics": topics, "fromBlock": s, "toBlock": min(s + MAX_RANGE - 1, b1)})
        return [lg for lg in out if not lg.get("removed")]

    # ---- sent transactions via nonce binary search ----------------------
    def sent_txs(self, b0: int, b1: int, limit: int) -> tuple[list[dict], int]:
        n0, n1 = self.nonce(b0 - 1), self.nonce(b1)
        total = n1 - n0
        found, blocks = [], {}
        for k in range(n0, min(n1, n0 + limit)):
            lo, hi = b0, b1  # smallest block whose post-state nonce > k  -> the block that contains nonce k
            while lo < hi:
                mid = (lo + hi) // 2
                if self.nonce(mid) > k:
                    hi = mid
                else:
                    lo = mid + 1
            if lo not in blocks:
                blocks[lo] = self.m.call_tool("get_block", {"block": lo, "includeTransactions": True})
            for tx in blocks[lo].get("transactions", []):
                if isinstance(tx, dict) and tx.get("from", "").lower() == self.w and h2i(tx.get("nonce")) == k:
                    found.append(tx)
                    break
        return found, total

    # ---- report ------------------------------------------------------------
    def build(self, t0: int, t1: int, b0: int, b1: int, max_sent: int) -> dict:
        W = pad(self.w)
        legs = []  # every wallet-related event, one line item each
        out_and_approvals = self.logs([[T_TRANSFER, T_APPROVAL, T_APPROVAL_ALL, T_DEPOSIT, T_WITHDRAWAL], W], b0, b1)
        incoming = self.logs([T_TRANSFER, None, W], b0, b1)
        swaps_to_wallet = self.logs([list(SWAP_TOPICS), None, W], b0, b1)
        seen = set()
        for lg in out_and_approvals + incoming + swaps_to_wallet:
            key = (lg["transactionHash"].lower(), h2i(lg["logIndex"]))
            if key in seen:
                continue
            seen.add(key)
            legs.append(lg)

        print(f"  logs: {len(legs)} wallet events; locating sent txs by nonce search ...", file=sys.stderr, flush=True)
        sent, sent_total = self.sent_txs(b0, b1, max_sent)
        print(f"  sent txs: {len(sent)} of {sent_total} located; reading receipts ...", file=sys.stderr, flush=True)
        txs: dict[str, dict] = {}

        def txrec(h: str, block: int, ts: int | None) -> dict:
            if h not in txs:
                txs[h] = {"tx": h, "block": block, "ts": ts, "items": [], "sent": None, "swap_events": set()}
            return txs[h]

        items = []
        for lg in legs:
            h, block = lg["transactionHash"].lower(), h2i(lg["blockNumber"])
            ts = h2i(lg["blockTimestamp"]) if lg.get("blockTimestamp") else self.ts(block)
            rec = txrec(h, block, ts)
            t0_, tok = lg["topics"][0].lower(), lg["address"].lower()
            if t0_ in SWAP_TOPICS:
                rec["swap_events"].add(SWAP_TOPICS[t0_])
                continue
            sym, dec = self.token(tok)
            raw = h2i(lg["data"][:66]) if lg.get("data") not in (None, "0x", "") else 0
            it = {"tx": h, "block": block, "ts": ts, "token": sym, "token_address": tok, "raw": raw,
                  "amount": raw / 10 ** dec if dec is not None else float(raw), "decimals": dec}
            if t0_ == T_TRANSFER:
                if len(lg["topics"]) == 4:  # ERC-721: tokenId in topic3
                    it.update(amount=1.0, raw=1, note=f"NFT #{h2i(lg['topics'][3])}")
                frm, to = "0x" + lg["topics"][1][26:], "0x" + lg["topics"][2][26:]
                it.update(kind="out" if frm == self.w else "in", counterparty=to if frm == self.w else frm)
                if frm == self.w and to == self.w:
                    it.update(kind="self")
                elif it["counterparty"] == "0x" + "0" * 40 and not it.get("note"):
                    it["note"] = "minted to wallet" if it["kind"] == "in" else "burned"
            elif t0_ in (T_APPROVAL, T_APPROVAL_ALL):
                spender = "0x" + lg["topics"][2][26:]
                it.update(kind="approval", counterparty=spender)
                if t0_ == T_APPROVAL_ALL:
                    it.update(amount=float(h2i(lg["data"])), note="approve ALL NFTs" if h2i(lg["data"]) else "revoke ALL NFTs")
                elif raw == UINT256_MAX or raw >= 2 ** 255:
                    it["note"] = "UNLIMITED approval"
                elif raw == 0:
                    it["note"] = "approval revoked (0)"
            elif t0_ == T_DEPOSIT:
                it.update(kind="in", counterparty=tok, note=f"wrap PROS -> {sym}")
            elif t0_ == T_WITHDRAWAL:
                it.update(kind="out", counterparty=tok, note=f"unwrap {sym} -> PROS")
            rec["items"].append(it)
            items.append(it)

        fees_wei, failed = 0, 0
        for tx in sent:
            h = tx["hash"].lower()
            rc = self.m.call_tool("get_transaction_receipt", {"hash": h})
            block = h2i(rc["blockNumber"])
            rec = txrec(h, block, h2i(tx.get("blockTimestamp")) if tx.get("blockTimestamp") else self.ts(block))
            fee = h2i(rc.get("gasUsed")) * h2i(rc.get("effectiveGasPrice") or tx.get("gasPrice"))
            fees_wei += fee
            ok = h2i(rc.get("status")) == 1
            failed += 0 if ok else 1
            to = (tx.get("to") or "").lower() or None
            rec["sent"] = {"to": to, "value_wei": h2i(tx.get("value")), "status": "success" if ok else "FAILED",
                           "fee_wei": fee, "selector": (tx.get("input") or "0x")[:10],
                           "is_contract": self.is_contract(to) if to else True,
                           "created": rc.get("contractAddress")}
            for lg in rc.get("logs", []):
                if lg["topics"] and lg["topics"][0].lower() in SWAP_TOPICS:
                    rec["swap_events"].add(SWAP_TOPICS[lg["topics"][0].lower()])
            if h2i(tx.get("value")) and ok:  # native PROS sent with the tx
                it = {"tx": h, "block": block, "ts": rec["ts"], "token": "PROS", "token_address": None,
                      "raw": h2i(tx["value"]), "amount": h2i(tx["value"]) / WEI, "decimals": 18,
                      "kind": "out", "counterparty": to or rc.get("contractAddress"), "note": "native PROS"}
                rec["items"].append(it)
                items.append(it)

        # one category per transaction
        for rec in txs.values():
            kinds = {it["kind"] for it in rec["items"]}
            toks_in = {it["token"] for it in rec["items"] if it["kind"] == "in"}
            toks_out = {it["token"] for it in rec["items"] if it["kind"] == "out"}
            wrap = any("wrap" in (it.get("note") or "") for it in rec["items"])
            if (toks_in and toks_out and toks_in != toks_out) or rec["swap_events"] or wrap:
                cat = "swap"
            elif "out" in kinds:
                cat = "transfer_out"
            elif "in" in kinds:
                cat = "transfer_in"
            elif "approval" in kinds:
                cat = "approval"
            else:
                cat = "contract_interaction"
            if cat == "swap" and not (toks_in or toks_out):
                cat = "contract_interaction" if rec["sent"] else "swap"
            rec["category"] = cat
            for it in rec["items"]:
                it["category"] = cat

        # totals per category
        totals = {c: {"txs": 0, "in": defaultdict(float), "out": defaultdict(float), "approvals": 0} for c in CATEGORIES}
        for rec in txs.values():
            t = totals[rec["category"]]
            t["txs"] += 1
            for it in rec["items"]:
                if it["kind"] in ("in", "out"):
                    t[it["kind"]][it["token"]] += it["amount"]
                elif it["kind"] == "approval":
                    t["approvals"] += 1
        bal0 = h2i(self.m.call_tool("get_balance", {"address": self.w, "block": b0 - 1})["balanceWei"])
        bal1 = h2i(self.m.call_tool("get_balance", {"address": self.w, "block": b1})["balanceWei"])
        return {"wallet": self.w, "is_contract": self.is_contract(self.w), "t0": t0, "t1": t1, "b0": b0, "b1": b1,
                "txs": sorted(txs.values(), key=lambda r: (r["block"], r["tx"])), "items": items,
                "totals": totals, "sent_total": sent_total, "sent_found": len(sent), "failed": failed,
                "fees_pros": fees_wei / WEI, "pros_start": bal0 / WEI, "pros_end": bal1 / WEI}


# ---- notable transactions + rendering ------------------------------------------
def notable(rep: dict, top: int) -> list[tuple[str, dict]]:
    out = []
    for rec in rep["txs"]:
        for it in rec["items"]:
            if (it.get("note") or "").startswith(("UNLIMITED", "approve ALL")):
                out.append((f"{it['note']} of {it['token']} to {it['counterparty']}", rec))
        if rec["sent"] and rec["sent"]["status"] == "FAILED":
            out.append(("failed transaction (fee paid, nothing executed)", rec))
    targets = defaultdict(list)  # contract interactions: one line per called contract (latest call linked)
    for rec in rep["txs"]:
        if rec["category"] == "contract_interaction" and rec["sent"]:
            targets[(rec["sent"]["to"] or "contract creation", rec["sent"]["selector"])].append(rec)
    for (to, sel), recs in sorted(targets.items(), key=lambda kv: -len(kv[1])):
        out.append((f"{len(recs)} call(s) to contract {to} (function {sel})", recs[-1]))
    by_token = defaultdict(list)  # largest moves per token
    for it in rep["items"]:
        if it["kind"] in ("in", "out"):
            by_token[it["token"]].append(it)
    recs = {r["tx"]: r for r in rep["txs"]}
    for tok, its in by_token.items():
        for it in sorted(its, key=lambda x: -x["amount"])[:top]:
            out.append((f"largest {tok} moves: {it['kind']} {fmt(it['amount'])} {tok} ({it['category']})", recs[it["tx"]]))
    seen, res = set(), []
    for why, rec in out:
        if (why, rec["tx"]) not in seen:
            seen.add((why, rec["tx"]))
            res.append((why, rec))
    return res


def describe(rec: dict) -> str:
    parts = []
    for it in rec["items"]:
        if it["kind"] == "approval":
            note = it.get("note") or ""
            what = (f"an UNLIMITED amount of {it['token']}" if note.startswith("UNLIMITED")
                    else f"{note} ({it['token']})" if note else f"{fmt(it['amount'])} {it['token']}")
            parts.append(f"approved {it['counterparty'][:10]}… to spend {what}")
        elif it["kind"] in ("in", "out"):
            arrow = "received" if it["kind"] == "in" else "sent"
            prep = "from" if it["kind"] == "in" else "to"
            parts.append(f"{arrow} {fmt(it['amount'])} {it['token']} {prep} {(it.get('counterparty') or '?')[:10]}…"
                         + (f" ({it['note']})" if it.get("note") else ""))
    s = rec["sent"]
    if s:
        parts.append(f"called {s['to'] or 'contract creation'} ({s['selector']}), {s['status']}")
    if rec["swap_events"]:
        parts.append("DEX event: " + ", ".join(sorted(rec["swap_events"])))
    return "; ".join(parts) or "—"


def plain_summary(rep: dict) -> str:
    t = rep["totals"]
    def toks(d):
        return ", ".join(f"{fmt(v)} {k}" for k, v in sorted(d.items(), key=lambda kv: -kv[1])) or "nothing"
    who = "contract" if rep["is_contract"] else "wallet"
    s = [f"Between {utc(rep['t0'])} and {utc(rep['t1'])} UTC (blocks {rep['b0']:,}–{rep['b1']:,}) this {who} "
         f"was involved in {len(rep['txs'])} transactions and itself sent {rep['sent_total']}"
         + (f" ({rep['failed']} of them failed)" if rep["failed"] else "") + f", paying {fmt(rep['fees_pros'])} PROS in fees."]
    s.append(f"It made {t['swap']['txs']} swaps (gave {toks(t['swap']['out'])}; got {toks(t['swap']['in'])}), "
             f"{t['transfer_out']['txs']} outgoing transfers ({toks(t['transfer_out']['out'])}), "
             f"{t['transfer_in']['txs']} incoming transfers ({toks(t['transfer_in']['in'])}), "
             f"{sum(x['approvals'] for x in t.values())} token approvals and "
             f"{t['contract_interaction']['txs']} other contract interactions.")
    s.append(f"Native PROS balance went from {fmt(rep['pros_start'])} to {fmt(rep['pros_end'])}.")
    return " ".join(s)


def render_markdown(rep: dict, top: int) -> str:
    w = rep["wallet"]
    md = [f"# Wallet activity report — `{w}`", "",
          f"* Network: Pharos Pacific mainnet (chainId 1672) · data via Pharos Chain MCP (`get_logs`, nonce search, receipts)",
          f"* Period: {utc(rep['t0'])} → {utc(rep['t1'])} UTC = blocks {rep['b0']:,}–{rep['b1']:,}",
          f"* Explorer: {EXPLORER}/address/{w}", "", "## Summary", "", plain_summary(rep), "",
          "## Totals per category", "", "| Category | Transactions | Tokens in | Tokens out | Approvals |", "|---|---|---|---|---|"]
    for c in CATEGORIES:
        t = rep["totals"][c]
        tin = ", ".join(f"{fmt(v)} {k}" for k, v in t["in"].items()) or "—"
        tout = ", ".join(f"{fmt(v)} {k}" for k, v in t["out"].items()) or "—"
        md.append(f"| {CAT_TITLE[c]} | {t['txs']} | {tin} | {tout} | {t['approvals'] or '—'} |")
    md += ["", f"Fees paid: {fmt(rep['fees_pros'])} PROS for {rep['sent_found']} sent transactions"
               f" ({rep['failed']} failed). Native PROS: {fmt(rep['pros_start'])} → {fmt(rep['pros_end'])}.", "",
           "## Notable transactions", ""]
    nts = notable(rep, top)
    md += [f"* **{why}** — {utc(rec['ts'])} — [{rec['tx'][:12]}…]({EXPLORER}/tx/{rec['tx']})" for why, rec in nts] or ["* none"]
    for c in CATEGORIES:
        recs = [r for r in rep["txs"] if r["category"] == c]
        md += ["", f"## {CAT_TITLE[c]} ({len(recs)})", ""]
        if not recs:
            md.append("None in this period.")
            continue
        md += ["| Time (UTC) | Block | What happened | Tx |", "|---|---|---|---|"]
        md += [f"| {utc(r['ts'])} | {r['block']:,} | {describe(r)} | [{r['tx'][:10]}…]({EXPLORER}/tx/{r['tx']}) |" for r in recs]
    md += ["", "## Coverage notes", "",
           f"* Sent transactions: {rep['sent_found']} of {rep['sent_total']} located via nonce binary search."
           + (" Raise `--max-sent` to locate the rest." if rep["sent_found"] < rep["sent_total"] else ""),
           "* Token movements come from ERC-20/721 `Transfer`, `Approval`, `ApprovalForAll` and WPROS `Deposit`/`Withdrawal` "
           "events that name the wallet; swaps are detected from opposite token legs in one tx or Uniswap V2/V3 / DODO "
           "swap events.",
           "* Native PROS received from others (and internal transfers) do not emit events, so they are not itemised; the "
           "start/end PROS balance is shown instead."]
    return "\n".join(md) + "\n"


def write_csv(rep: dict, path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(["time_utc", "block", "tx_hash", "category", "direction", "token", "token_address", "amount",
                     "counterparty", "note", "tx_status", "fee_pros", "explorer_link"])
        for r in rep["txs"]:
            s = r["sent"] or {}
            rows = r["items"] or [None]
            for it in rows:
                wr.writerow([utc(r["ts"]), r["block"], r["tx"], r["category"],
                             it["kind"] if it else "call", it["token"] if it else "", (it or {}).get("token_address") or "",
                             fmt(it["amount"]) if it else "", (it or {}).get("counterparty") or s.get("to") or "",
                             (it or {}).get("note") or "", s.get("status", "not sent by wallet"),
                             fmt(s["fee_wei"] / WEI) if s else "", f"{EXPLORER}/tx/{r['tx']}"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Plain-language activity report for a Pharos wallet (read-only, Chain MCP).")
    ap.add_argument("--wallet", required=True, help="wallet (or contract) address, 0x + 40 hex")
    ap.add_argument("--start", required=True, help="UTC start, YYYY-MM-DD or YYYY-MM-DDTHH:MM")
    ap.add_argument("--end", required=True, help="UTC end (inclusive), YYYY-MM-DD (= end of day) or YYYY-MM-DDTHH:MM")
    ap.add_argument("--max-blocks", type=int, default=20000,
                    help="refuse ranges longer than this many blocks (default 20000 ~ 5.5 h; 3 get_logs calls per 1000 blocks)")
    ap.add_argument("--max-sent", type=int, default=100, help="locate at most this many sent txs (default 100)")
    ap.add_argument("--top", type=int, default=3, help="largest moves per token listed as notable (default 3)")
    ap.add_argument("--markdown", default="report.md", help="Markdown output path (default report.md)")
    ap.add_argument("--csv", default="report.csv", help="CSV output path (default report.csv)")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Chain MCP endpoint (https)")
    a = ap.parse_args(argv)
    try:
        wallet = check_address(a.wallet)
        t0, t1 = parse_when(a.start, False), parse_when(a.end, True)
    except ValueError as e:
        ap.error(str(e))
    if t1 < t0:
        ap.error("--end is before --start")
    if a.max_blocks < 1 or a.max_sent < 0 or a.top < 0:
        ap.error("--max-blocks must be >= 1, --max-sent and --top >= 0")

    mcp = PharosMCP(a.endpoint)
    ag = AuditAgent(mcp, wallet)
    print(f"Pharos Audit-Log Agent · {mcp.server_info.get('name')} {mcp.server_info.get('version')} @ {a.endpoint}")
    b0, b1 = ag.date_range_to_blocks(t0, t1)
    print(f"  wallet {wallet}\n  period {utc(t0)} -> {utc(t1)} UTC  =  blocks #{b0:,} - #{b1:,} ({b1 - b0 + 1:,} blocks)")
    if b1 - b0 + 1 > a.max_blocks:
        print(f"  range is {b1 - b0 + 1:,} blocks > --max-blocks {a.max_blocks:,}; narrow the dates or raise --max-blocks "
              f"(get_logs: <=1000 blocks per call, 120 req/min)", file=sys.stderr)
        return 2
    rep = ag.build(t0, t1, b0, b1, a.max_sent)
    print("\n" + plain_summary(rep) + "\n")
    for c in CATEGORIES:
        t = rep["totals"][c]
        print(f"  {CAT_TITLE[c]:28} {t['txs']:4} txs   in: {', '.join(f'{fmt(v)} {k}' for k, v in t['in'].items()) or '-'}"
              f"   out: {', '.join(f'{fmt(v)} {k}' for k, v in t['out'].items()) or '-'}"
              + (f"   approvals: {t['approvals']}" if t["approvals"] else ""))
    print("\nNotable:")
    for why, rec in notable(rep, a.top):
        print(f"  - {why}  {utc(rec['ts'])}  {EXPLORER}/tx/{rec['tx']}")
    with open(a.markdown, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(rep, a.top))
    write_csv(rep, a.csv)
    print(f"\nWrote {a.markdown} and {a.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
