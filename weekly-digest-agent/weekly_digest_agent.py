#!/usr/bin/env python3
"""Weekly Digest Agent: a periodic summary of the Morpho Blue lending protocol on Pharos mainnet.

Every period (weekly by default) it compares the period that just ended with the one before it:
  * key metrics      - total supplied / borrowed / utilization across all Morpho markets at each period end
                       (Morpho.market(id) via eth_call pinned to the period's last block), PROS/USD price
                       (Chainlink feed), number of Morpho events by type, gross flows in USD, unique accounts
  * notable events   - the top 3 events of the period (liquidations and new markets first, then by USD size)
  * delivery         - console + a Markdown and an HTML digest file per period in --out (a page you can publish);
                       a state file prevents the same period from being delivered twice.

Chain data: hosted Pharos Chain MCP (https://mcp.pharos.xyz/mcp), read-only tools get_block (period start/end ->
block numbers by binary search on timestamps), get_transaction_receipt (the Morpho events themselves) and eth_call
(market state, oracle prices, token metadata, Chainlink). Which transactions touched Morpho is looked up in the
public Pharosscan explorer index (token transfers to/from Morpho + direct calls), because a node cannot list them
without scanning every block. Nothing is signed or sent.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext

from Crypto.Hash import keccak

from pharos_explorer import EXPLORER_UI, PharosExplorer
from pharos_mcp import DEFAULT_ENDPOINT, MCPError, PharosMCP

getcontext().prec = 60
__version__ = "1.0.0"
MORPHO = "0x18573fa18fd17ddfd790b4a5b5b2977aad3b4efb"   # Morpho Blue on Pharos mainnet (verified "Morpho")
PROS_USD_FEED = "0x371ed36aee5bffbed1a2aa9b7ded7f4eb30f5466"  # Chainlink PROS/USD (decimals() read on chain)
WPROS = "0x52c48d4213107b20bc583832b0d951fb9ca8f0b0"
USDC = "0xc879c018db60520f4355c26ed1a6d572cdac1815"
ORACLE_SCALE = 10 ** 36
PERIOD_RE = re.compile(r"^(weekly|daily|\d+[hd])$")


def kk(sig: str) -> str:
    k = keccak.new(digest_bits=256)
    k.update(sig.encode())
    return "0x" + k.hexdigest()


def sel(sig: str) -> str:
    return kk(sig)[:10]


EV = {  # Morpho Blue EventsLib
    kk("Supply(bytes32,address,address,uint256,uint256)"): "Supply",
    kk("Withdraw(bytes32,address,address,address,uint256,uint256)"): "Withdraw",
    kk("Borrow(bytes32,address,address,address,uint256,uint256)"): "Borrow",
    kk("Repay(bytes32,address,address,uint256,uint256)"): "Repay",
    kk("SupplyCollateral(bytes32,address,address,uint256)"): "SupplyCollateral",
    kk("WithdrawCollateral(bytes32,address,address,address,uint256)"): "WithdrawCollateral",
    kk("Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)"): "Liquidate",
    kk("CreateMarket(bytes32,(address,address,address,address,uint256))"): "CreateMarket",
    kk("FlashLoan(address,address,uint256)"): "FlashLoan",
}
EV_ORDER = ["Supply", "Withdraw", "Borrow", "Repay", "SupplyCollateral", "WithdrawCollateral", "Liquidate",
            "CreateMarket", "FlashLoan"]
S_PARAMS, S_MARKET = sel("idToMarketParams(bytes32)"), sel("market(bytes32)")
S_PRICE, S_SYMBOL, S_DECIMALS = sel("price()"), sel("symbol()"), sel("decimals()")
S_LATEST = sel("latestRoundData()")


def h2i(v) -> int:
    if v is None:
        return 0
    if isinstance(v, int):
        return v
    s = str(v)
    return int(s, 16) if s.startswith("0x") else int(s)


def words(h: str) -> list[int]:
    h = h[2:] if h.startswith("0x") else h
    return [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]


def utc(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def usd(x) -> str:
    return f"${Decimal(x):,.2f}"


def pct_change(new, old) -> str:
    new, old = Decimal(new), Decimal(old)
    if old == 0:
        return "new" if new else "±0"
    d = (new - old) / old * 100
    return f"{d:+.1f}%"


def period_seconds(p: str) -> int:
    if not PERIOD_RE.match(p or ""):
        raise ValueError(f"bad --period {p!r}; use weekly, daily, <N>d or <N>h")
    if p == "weekly":
        return 7 * 86400
    if p == "daily":
        return 86400
    n = int(p[:-1])
    sec = n * (86400 if p.endswith("d") else 3600)
    if not 3600 <= sec <= 31 * 86400:
        raise ValueError("--period must be between 1h and 31d")
    return sec


class Digest:
    def __init__(self, mcp: PharosMCP, ex: PharosExplorer, known: list[str] | None = None):
        self.m, self.ex = mcp, ex
        self.known = list(known or [])
        self.new_markets: list[str] = []
        self._ts: dict[int, int] = {}
        self._tok: dict[str, tuple[str, int]] = {}
        self._params: dict[str, dict] = {}
        self._price: dict[tuple, Decimal] = {}

    # ---- chain helpers -------------------------------------------------
    def ts(self, n: int) -> int:
        if n not in self._ts:
            self._ts[n] = h2i(self.m.call_tool("get_block", {"block": n})["timestamp"])
        return self._ts[n]

    def block_at(self, t: int, head: int) -> int:
        """Last block with timestamp <= t (binary search on block timestamps)."""
        lo, hi = 1, head
        if self.ts(head) <= t:
            return head
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.ts(mid) <= t:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def call(self, to: str, data: str, block) -> str:
        return self.m.eth_call(to, data, block)

    def token(self, a: str) -> tuple[str, int]:
        if a not in self._tok:
            sym = a[:10]
            try:
                b = bytes.fromhex(self.call(a, S_SYMBOL, "latest")[2:])
                sym = b[64:64 + int.from_bytes(b[32:64], "big")].decode() or sym
            except (MCPError, ValueError):
                pass
            self._tok[a] = (sym, h2i(self.call(a, S_DECIMALS, "latest")))
        return self._tok[a]

    def params(self, mid: str) -> dict:
        if mid not in self._params:
            p = words(self.call(MORPHO, S_PARAMS + mid[2:], "latest"))
            loan, col = "0x%040x" % p[0], "0x%040x" % p[1]
            self._params[mid] = {"loan": loan, "collateral": col, "oracle": "0x%040x" % p[2], "lltv": p[4],
                                 "loan_tok": self.token(loan),
                                 "col_tok": self.token(col) if p[1] else ("-", 18)}
        return self._params[mid]

    def pros_usd(self, block: int) -> Decimal:
        key = ("pros", block)
        if key not in self._price:
            if not hasattr(self, "_feed_dec"):  # read decimals() from the feed instead of assuming 8
                self._feed_dec = h2i(self.call(PROS_USD_FEED, S_DECIMALS, "latest"))
            w = words(self.call(PROS_USD_FEED, S_LATEST, block))
            self._price[key] = Decimal(w[1]) / Decimal(10) ** self._feed_dec
        return self._price[key]

    def loan_usd(self, token: str, block: int) -> Decimal:
        """USD price of a loan token: USDC = $1.00 (assumption), WPROS = Chainlink PROS/USD."""
        if token == USDC:
            return Decimal(1)
        if token == WPROS:
            return self.pros_usd(block)
        raise KeyError(token)

    def col_in_loan(self, mid: str, block: int) -> Decimal:
        """Collateral price in loan-token units from the market's oracle (1e36 scale, Morpho convention)."""
        key = (mid, block)
        if key not in self._price:
            P = self.params(mid)
            raw = h2i(self.call(P["oracle"], S_PRICE, block))
            self._price[key] = Decimal(raw) * Decimal(10) ** P["col_tok"][1] / Decimal(ORACLE_SCALE) / \
                Decimal(10) ** P["loan_tok"][1]
        return self._price[key]

    # ---- data gathering -----------------------------------------------
    def markets(self) -> list[str]:
        """All Morpho markets: CreateMarket events in receipts of the direct createMarket() calls."""
        cm = sel("createMarket((address,address,address,address,uint256))")
        _, txs = self.ex.transactions(MORPHO, 5000)
        ids = []
        for t in txs:
            if t.get("method_id") == cm and t.get("receipt_status") == 1:
                for lg in self.m.call_tool("get_transaction_receipt", {"hash": t["hash"]})["logs"]:
                    if lg["address"].lower() == MORPHO and EV.get(lg["topics"][0]) == "CreateMarket":
                        ids.append(lg["topics"][1])
        return ids

    def tx_hashes(self, b0: int, b1: int) -> list[str]:
        """Txs in [b0, b1] that moved tokens in/out of Morpho or called it directly (explorer index)."""
        hs = {}
        page = 1
        while True:
            d = self.ex._get(f"address/{MORPHO}/token_transfers", {"page": page, "size": 100})
            rows = d.get("data") or []
            for r in rows:
                if b0 <= r["block_number"] <= b1:
                    hs[r["transaction_hash"]] = r["block_number"]
            if len(rows) < 100 or rows[-1]["block_number"] < b0:
                break
            page += 1
        _, txs = self.ex.transactions(MORPHO, 5000)
        for t in txs:
            if b0 <= t["block_number"] <= b1 and t.get("receipt_status") == 1:
                hs[t["hash"]] = t["block_number"]
        return sorted(hs, key=lambda h: hs[h])

    def events(self, b0: int, b1: int) -> list[dict]:
        out = []
        for h in self.tx_hashes(b0, b1):
            rc = self.m.call_tool("get_transaction_receipt", {"hash": h})
            if h2i(rc.get("status")) != 1:
                continue
            blk = h2i(rc["blockNumber"])
            for lg in rc["logs"]:
                if lg["address"].lower() != MORPHO or lg["topics"][0] not in EV:
                    continue
                out.append(self.decode(EV[lg["topics"][0]], lg, h, blk))
        return out

    def decode(self, kind: str, lg: dict, tx: str, blk: int) -> dict:
        t, d = lg["topics"], words(lg["data"])
        e = {"kind": kind, "tx": tx, "block": blk, "usd": Decimal(0), "detail": "", "account": None}
        if kind == "FlashLoan":
            tok = "0x" + t[2][-40:]
            sym, dec = self.token(tok)
            amt = Decimal(d[0]) / Decimal(10) ** dec
            e["detail"] = f"flash loan of {amt:,.4f} {sym}"
            try:
                e["usd"] = amt * self.loan_usd(tok, blk)
            except KeyError:
                pass
            return e
        mid = t[1]
        P = self.params(mid)
        lsym, ldec = P["loan_tok"]
        csym, cdec = P["col_tok"]
        e["market"] = f"{csym}/{lsym}"
        e["market_id"] = mid
        lp = self.loan_usd(P["loan"], blk)
        if kind == "CreateMarket":
            e["detail"] = f"new market {csym} collateral → {lsym} loans, LLTV {Decimal(P['lltv']) / 10 ** 16:.1f}%"
            return e
        if kind in ("Supply", "Repay"):
            e["account"] = "0x" + t[3][-40:]
            amt = Decimal(d[0]) / Decimal(10) ** ldec
        elif kind in ("Withdraw", "Borrow"):
            e["account"] = "0x" + t[2][-40:]
            amt = Decimal(d[1]) / Decimal(10) ** ldec
        elif kind == "SupplyCollateral":
            e["account"] = "0x" + t[3][-40:]
            amt = Decimal(d[0]) / Decimal(10) ** cdec
        elif kind == "WithdrawCollateral":
            e["account"] = "0x" + t[2][-40:]
            amt = Decimal(d[1]) / Decimal(10) ** cdec
        else:  # Liquidate: repaidAssets, repaidShares, seizedAssets, badDebtAssets, badDebtShares
            e["account"] = "0x" + t[3][-40:]
            rep = Decimal(d[0]) / Decimal(10) ** ldec
            seized = Decimal(d[2]) / Decimal(10) ** cdec
            bad = Decimal(d[3]) / Decimal(10) ** ldec
            e["usd"] = rep * lp
            e["detail"] = (f"borrower {e['account']} liquidated in {e['market']}: {rep:,.4f} {lsym} repaid, "
                           f"{seized:,.4f} {csym} seized" + (f", bad debt {bad:,.4f} {lsym}" if bad else ""))
            return e
        if kind in ("SupplyCollateral", "WithdrawCollateral"):
            e["usd"] = amt * self.col_in_loan(mid, blk) * lp
            e["detail"] = f"{kind} {amt:,.4f} {csym} in {e['market']}"
        else:
            e["usd"] = amt * lp
            e["detail"] = f"{kind} {amt:,.4f} {lsym} in {e['market']}"
        return e

    def snapshot(self, ids: list[str], block: int) -> dict:
        """Per-market totals at `block`. A market whose read fails 3 times is listed in 'failed', never guessed."""
        per, failed = {}, []
        for mid in ids:
            w = None
            for attempt in range(3):
                try:
                    w = words(self.call(MORPHO, S_MARKET + mid[2:], block))
                    break
                except MCPError:
                    time.sleep(2 * (attempt + 1))
            if w is None:
                failed.append(mid)
                continue
            P = self.params(mid)
            if w[4] == 0:  # lastUpdate 0 -> market did not exist yet at this block
                continue
            dec = P["loan_tok"][1]
            lp = self.loan_usd(P["loan"], block)
            per[mid] = {"market": f"{P['col_tok'][0]}/{P['loan_tok'][0]}",
                        "supply_usd": Decimal(w[0]) / Decimal(10) ** dec * lp,
                        "borrow_usd": Decimal(w[2]) / Decimal(10) ** dec * lp}
        return {"block": block, "markets": per, "failed": failed, "pros_usd": self.pros_usd(block)}

    @staticmethod
    def totals(snap: dict, skip: set) -> None:
        ms = [v for k, v in snap["markets"].items() if k not in skip]
        snap["supply_usd"] = sum((m["supply_usd"] for m in ms), Decimal(0))
        snap["borrow_usd"] = sum((m["borrow_usd"] for m in ms), Decimal(0))
        snap["utilization"] = snap["borrow_usd"] / snap["supply_usd"] * 100 if snap["supply_usd"] else Decimal(0)

    # ---- the digest ---------------------------------------------------
    def build(self, end_ts: int, period: int) -> dict:
        head = self.m.block_number()
        if end_ts > self.ts(head):
            end_ts = self.ts(head)
        start_ts, prev_ts = end_ts - period, end_ts - 2 * period
        b_end = self.block_at(end_ts, head)
        b_start = self.block_at(start_ts, head)
        b_prev = self.block_at(prev_ts, head)
        cur_ev = self.events(b_start + 1, b_end)
        prev_ev = self.events(b_prev + 1, b_start)
        ids = self.markets()  # direct createMarket() calls
        ids += [m for m in self.known if m not in ids]  # + known_markets.json (markets created via other contracts)
        ids += [m for m in dict.fromkeys(e.get("market_id") for e in cur_ev + prev_ev) if m and m not in ids]
        self.new_markets = [m for m in ids if m not in self.known]
        snap_end, snap_start = self.snapshot(ids, b_end), self.snapshot(ids, b_start)
        unreadable = set(snap_end["failed"]) | set(snap_start["failed"])
        self.totals(snap_end, unreadable)   # compare like with like: a market unreadable at either end
        self.totals(snap_start, unreadable)  # is left out of both totals and named in the digest

        def stats(evs):
            c = Counter(e["kind"] for e in evs)
            flows = defaultdict(Decimal)
            for e in evs:
                flows[e["kind"]] += e["usd"]
            return {"count": c, "flows": flows, "accounts": len({e["account"] for e in evs if e["account"]}),
                    "txs": len({e["tx"] for e in evs})}

        cur, prev = stats(cur_ev), stats(prev_ev)

        def rank(e):
            return (2 if e["kind"] == "Liquidate" else 1 if e["kind"] == "CreateMarket" else 0, e["usd"])

        top = sorted(cur_ev, key=rank, reverse=True)[:3]
        movers = []
        for mid, m in snap_end["markets"].items():
            if mid in unreadable:
                continue
            o = snap_start["markets"].get(mid, {"supply_usd": Decimal(0), "borrow_usd": Decimal(0)})
            movers.append((abs(m["supply_usd"] - o["supply_usd"]) + abs(m["borrow_usd"] - o["borrow_usd"]), m, o))
        movers.sort(key=lambda x: x[0], reverse=True)
        return {"period_s": period, "start": start_ts, "end": end_ts, "prev_start": prev_ts,
                "blocks": {"prev_start": b_prev, "start": b_start, "end": b_end}, "markets": len(ids),
                "snap_end": snap_end, "snap_start": snap_start, "cur": cur, "prev": prev, "top": top,
                "movers": movers[:3], "unreadable": sorted(unreadable), "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}


def render_md(r: dict) -> str:
    se, ss, cur, prev = r["snap_end"], r["snap_start"], r["cur"], r["prev"]
    days = r["period_s"] / 86400
    B = r["blocks"]
    label = {7: "weekly", 1: "daily"}.get(days) or (f"{days:g}-day" if days >= 1 else f"{r['period_s'] // 3600}-hour")
    L = [f"# Morpho Blue on Pharos · {label} digest",
         f"**Period:** {utc(r['start'])} → {utc(r['end'])} (blocks {B['start'] + 1:,}–{B['end']:,}) · compared with "
         f"{utc(r['prev_start'])} → {utc(r['start'])} (blocks {B['prev_start'] + 1:,}–{B['start']:,})",
         f"Morpho contract [{MORPHO}]({EXPLORER_UI}/address/{MORPHO}) · {r['markets']} markets · generated "
         f"{r['generated']}", "",
         "## Key metrics", "",
         "| Metric | This period | Previous period | Change |", "|---|---:|---:|---:|",
         f"| Total supplied (end of period) | {usd(se['supply_usd'])} | {usd(ss['supply_usd'])} | "
         f"{pct_change(se['supply_usd'], ss['supply_usd'])} |",
         f"| Total borrowed (end of period) | {usd(se['borrow_usd'])} | {usd(ss['borrow_usd'])} | "
         f"{pct_change(se['borrow_usd'], ss['borrow_usd'])} |",
         f"| Utilization | {se['utilization']:.2f}% | {ss['utilization']:.2f}% | "
         f"{se['utilization'] - ss['utilization']:+.2f} pp |",
         f"| PROS/USD (Chainlink) | ${se['pros_usd']:.6f} | ${ss['pros_usd']:.6f} | "
         f"{pct_change(se['pros_usd'], ss['pros_usd'])} |",
         f"| Transactions touching Morpho | {cur['txs']} | {prev['txs']} | {pct_change(cur['txs'], prev['txs'])} |",
         f"| Unique accounts | {cur['accounts']} | {prev['accounts']} | {pct_change(cur['accounts'], prev['accounts'])} |"]
    for k in EV_ORDER:
        a, b = cur["count"].get(k, 0), prev["count"].get(k, 0)
        if a or b:
            fa, fb = cur["flows"].get(k, Decimal(0)), prev["flows"].get(k, Decimal(0))
            extra = f" ({usd(fa)} vs {usd(fb)})" if k not in ("CreateMarket",) else ""
            L.append(f"| {k} events{extra} | {a} | {b} | {pct_change(a, b)} |")
    if r["unreadable"]:
        L.append("")
        L.append(f"_Could not read {len(r['unreadable'])} market(s) after 3 tries, left out of both totals: "
                 + ", ".join(m[:10] + "…" for m in r["unreadable"]) + "._")
    L += ["", "## Top 3 notable events", ""]
    if not r["top"]:
        L.append("_No Morpho events in this period._")
    for i, e in enumerate(r["top"], 1):
        L.append(f"{i}. **{e['kind']}** — {e['detail']}" + (f" (≈ {usd(e['usd'])})" if e["usd"] else "")
                 + f" · block {e['block']:,} · [tx]({EXPLORER_UI}/tx/{e['tx']})")
    L += ["", "## Biggest market moves (end of period vs start)", "",
          "| Market | Supplied | Borrowed |", "|---|---:|---:|"]
    for _, m, o in r["movers"]:
        L.append(f"| {m['market']} | {usd(m['supply_usd'])} ({pct_change(m['supply_usd'], o['supply_usd'])}) | "
                 f"{usd(m['borrow_usd'])} ({pct_change(m['borrow_usd'], o['borrow_usd'])}) |")
    L += ["", "---", "Sources: Pharos Chain MCP `https://mcp.pharos.xyz/mcp` — `eth_call Morpho.market(id)` pinned to "
          f"blocks {B['end']:,} and {B['start']:,}, Morpho events from `get_transaction_receipt`, Chainlink PROS/USD "
          f"[{PROS_USD_FEED}]({EXPLORER_UI}/address/{PROS_USD_FEED}) `latestRoundData()`; list of transactions from the "
          "Pharosscan explorer index. USD values: USDC counted as $1.00, WPROS at the Chainlink PROS/USD price, "
          "collateral via each market's Morpho oracle. Read-only; not financial advice."]
    return "\n".join(L) + "\n"


def md_to_html(md: str, title: str) -> str:
    """Small Markdown→HTML for our own digest (headings, tables, lists, links, bold); output is escaped."""
    out, in_table, in_list = [], False, False
    link = re.compile(r"\[([^\]]+)\]\((https://[^)]+)\)")

    def inline(s: str) -> str:
        s = html.escape(s)
        s = link.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
        return re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<i>\1</i>", s)

    for line in md.splitlines():
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(set(c) <= set("-:") for c in cells):
                continue
            if not in_table:
                out.append("<table>")
                in_table = True
                out.append("<tr>" + "".join(f"<th>{inline(c)}</th>" for c in cells) + "</tr>")
                continue
            out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if re.match(r"^\d+\. ", line):
            if not in_list:
                out.append("<ol>")
                in_list = True
            out.append(f"<li>{inline(line.split('. ', 1)[1])}</li>")
            continue
        if in_list:
            out.append("</ol>")
            in_list = False
        if line.startswith("## "):
            out.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{inline(line[2:])}</h1>")
        elif line == "---":
            out.append("<hr>")
        elif line.strip():
            out.append(f"<p>{inline(line)}</p>")
    if in_table:
        out.append("</table>")
    if in_list:
        out.append("</ol>")
    css = ("body{font-family:system-ui,sans-serif;max-width:900px;margin:2em auto;padding:0 1em;color:#111}"
           "table{border-collapse:collapse;margin:1em 0}td,th{border:1px solid #ccc;padding:4px 8px;text-align:right}"
           "td:first-child,th:first-child{text-align:left}code{background:#f3f3f3;padding:0 3px}")
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<style>{css}</style></head><body>\n" + "\n".join(out) + "\n</body></html>\n")


def load_state(state_path: str) -> dict:
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def already_delivered(state_path: str, start: int, end: int) -> bool:
    if f"{start}-{end}" in load_state(state_path).get("delivered", []):
        print(f"[{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}] period {utc(start)} → {utc(end)} "
              "already delivered; skipping")
        return True
    return False


def deliver(r: dict, out_dir: str, state_path: str) -> str:
    """Console + Markdown/HTML files; records the period in the state file. Returns the Markdown path."""
    key = f"{r['start']}-{r['end']}"
    state = load_state(state_path)
    md = render_md(r)
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.join(out_dir, "digest_" + time.strftime("%Y-%m-%dT%H%MZ", time.gmtime(r["end"])))
    with open(stem + ".md", "w", encoding="utf-8") as fh:
        fh.write(md)
    with open(stem + ".html", "w", encoding="utf-8") as fh:
        fh.write(md_to_html(md, "Morpho Blue on Pharos digest"))
    print(md)
    print(f"[{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}] delivered: {stem}.md and {stem}.html")
    state.setdefault("delivered", []).append(key)
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
    return stem + ".md"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Periodic digest of Morpho Blue on Pharos mainnet (read-only).")
    ap.add_argument("--period", default="weekly", help="weekly (default), daily, <N>d or <N>h (1h..31d)")
    ap.add_argument("--end", help="period end, UTC 'YYYY-MM-DDTHH:MM' (default: now)")
    ap.add_argument("--schedule", action="store_true",
                    help="keep running: deliver a digest every --period, aligned to --anchor")
    ap.add_argument("--anchor", default="monday 00:00",
                    help="schedule anchor for weekly digests, '<weekday> HH:MM' UTC (default 'monday 00:00'); "
                         "other periods align to multiples of the period since the Unix epoch")
    ap.add_argument("--runs", type=int, default=0, help="with --schedule: stop after N digests (0 = forever)")
    ap.add_argument("--out", default="digests", help="output directory for .md/.html digests")
    ap.add_argument("--state", default="state_digest.json", help="delivered-periods state file")
    ap.add_argument("--force", action="store_true", help="deliver even if this period was already delivered")
    ap.add_argument("--json", help="also write the raw digest data to this JSON file")
    ap.add_argument("--markets", default="known_markets.json",
                    help="known Morpho market ids (unioned with on-chain discovery; new ids are appended)")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Chain MCP endpoint (https only)")
    a = ap.parse_args(argv)
    try:
        period = period_seconds(a.period)
        end_ts = None
        if a.end:
            if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$", a.end):
                raise ValueError("--end must be YYYY-MM-DDTHH:MM (UTC)")
            end_ts = int(datetime.strptime(a.end, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc).timestamp())
        days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        m = re.match(r"^(\w+) (\d{2}):(\d{2})$", a.anchor.lower())
        if not m or m.group(1) not in days or int(m.group(2)) > 23 or int(m.group(3)) > 59:
            raise ValueError("--anchor must look like 'monday 00:00'")
        if a.runs < 0:
            raise ValueError("--runs must be >= 0")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    known = {}
    if a.markets and os.path.exists(a.markets):
        with open(a.markets, encoding="utf-8") as fh:
            known = json.load(fh)
        for mid in known.get("markets", {}):
            if not re.match(r"^0x[0-9a-f]{64}$", mid):
                print(f"error: bad market id in {a.markets}: {mid}", file=sys.stderr)
                return 2
    dg = Digest(PharosMCP(a.endpoint), PharosExplorer(), list(known.get("markets", {})))

    def remember_markets() -> None:  # append newly seen market ids to the markets file
        if a.markets and dg.new_markets:
            known.setdefault("markets", {})
            for mid in dg.new_markets:
                P = dg.params(mid)
                known["markets"][mid] = f"{P['col_tok'][0]}/{P['loan_tok'][0]}"
            with open(a.markets, "w", encoding="utf-8") as fh:
                json.dump(known, fh, indent=1)
            dg.known += dg.new_markets

    def last_boundary(now: int) -> int:
        if period == 7 * 86400:
            wd, hh, mm = days.index(m.group(1)), int(m.group(2)), int(m.group(3))
            d = datetime.fromtimestamp(now, timezone.utc)
            b = d.replace(hour=hh, minute=mm, second=0, microsecond=0) - timedelta(days=(d.weekday() - wd) % 7)
            if b > d:
                b -= timedelta(days=7)
            return int(b.timestamp())
        return now - now % period

    if not a.schedule:
        end = min(end_ts or int(time.time()), dg.ts(dg.m.block_number()))
        if not a.force and already_delivered(a.state, end - period, end):
            return 0
        r = dg.build(end, period)
        deliver(r, a.out, a.state)
        remember_markets()
        if a.json:
            with open(a.json, "w", encoding="utf-8") as fh:
                json.dump(r, fh, indent=1, default=str)
        return 0
    done = 0
    print(f"[{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}] scheduler started: period {a.period}"
          + (f", anchor {a.anchor} UTC" if period == 7 * 86400 else ""))
    while True:
        boundary = last_boundary(int(time.time()))
        if not already_delivered(a.state, boundary - period, boundary):
            deliver(dg.build(boundary, period), a.out, a.state)
            remember_markets()
            done += 1
        nxt = boundary + period
        if a.runs and done >= a.runs:
            print(f"[{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}] --runs {a.runs} reached; next digest would be at "
                  f"{utc(nxt)}; exiting")
            return 0
        wait = max(nxt - int(time.time()) + 30, 30)  # +30 s so the boundary block exists
        print(f"[{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}] next digest at {utc(nxt)} (sleeping {wait} s)")
        time.sleep(wait)


if __name__ == "__main__":
    sys.exit(main())
