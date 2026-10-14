#!/usr/bin/env python3
"""Whale-Watching Agent for Pharos: track a list of addresses and report significant moves.

All chain data comes from the hosted Pharos Chain MCP server (https://mcp.pharos.xyz/mcp), read-only:

  get_logs      ERC-20 Transfer events of the configured tokens where a watched address is sender
                (topic1) or receiver (topic2); <= 1000 blocks per call (server limit)
  eth_call      symbol()/decimals() of each token, balanceOf(watched) before/after a significant move
                (-> "new position" / "exit" / "% of holding"), symbol() of unknown counterparties
  get_code      is a counterparty a contract or a plain wallet?
  get_balance   native PROS balance of each watched address at window start and end
  get_block     block timestamps

Config (JSON, see whales.json): watched addresses (+labels), per-token thresholds in token units,
an optional value-terms threshold (min_usd, using static user-supplied usd_price per token) and a
native PROS threshold. Output: one report per significant move + a summary across all watched
addresses; optional JSON and Markdown export, optional --follow mode for continuous watching.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict

from pharos_mcp import DEFAULT_ENDPOINT, PharosMCP, check_address

EXPLORER = "https://www.pharosscan.xyz"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO = "0x" + "0" * 40
MAX_RANGE = 1000  # get_logs window limit of the hosted server (inclusive)
WEI = 10 ** 18


def pad(addr: str) -> str:
    return "0x" + "0" * 24 + addr[2:].lower()


def h2i(v) -> int:
    if isinstance(v, int):
        return v
    s = str(v)
    return int(s, 16) if s.startswith("0x") else int(s)


def utc(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts))


def fmt(x: float) -> str:
    return f"{x:,.6f}".rstrip("0").rstrip(".") if abs(x) < 1e15 else f"{x:.3e}"


def pct(x: float) -> str:
    return "<1%" if 0 < x < 0.01 else f"{x:.0%}"


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    watch = [{"address": check_address(w["address"]), "label": w.get("label") or ""} for w in cfg.get("watch", [])]
    if len(watch) < 1:
        raise ValueError("config needs at least one address under 'watch'")
    if len({w["address"] for w in watch}) != len(watch):
        raise ValueError("duplicate address in 'watch'")
    tokens = []
    for t in cfg.get("tokens", []):
        thr = float(t["threshold"])
        if thr < 0:
            raise ValueError(f"negative threshold for {t}")
        price = t.get("usd_price")
        tokens.append({"address": check_address(t["address"]), "symbol": t.get("symbol"), "threshold": thr,
                       "usd_price": float(price) if price is not None else None})
    if not tokens:
        raise ValueError("config needs at least one token under 'tokens'")
    labels = {check_address(a): l for a, l in (cfg.get("labels") or {}).items()}
    for w in watch:
        labels.setdefault(w["address"], w["label"])
    return {"watch": watch, "tokens": tokens, "labels": labels,
            "min_usd": float(cfg["min_usd"]) if cfg.get("min_usd") is not None else None,
            "native_threshold": float(cfg.get("native_threshold_pros", 0) or 0)}


class WhaleWatcher:
    def __init__(self, mcp: PharosMCP, cfg: dict):
        self.m = mcp
        self.cfg = cfg
        self.watched = {w["address"]: w["label"] for w in cfg["watch"]}
        self.tokens = {t["address"]: t for t in cfg["tokens"]}
        self.labels = dict(cfg["labels"])
        self._label_cache: dict[str, str] = {}
        self._ts: dict[int, int] = {}
        self._bal: dict[tuple, int] = {}
        for t in self.tokens.values():  # symbol/decimals straight from the token contracts
            t["decimals"] = h2i(self.m.eth_call(t["address"], "0x313ce567"))
            sym = self._symbol(t["address"])
            t["symbol"] = sym or t.get("symbol") or t["address"][:10]

    # ---- small chain helpers ----------------------------------------------
    def _symbol(self, addr: str) -> str | None:
        try:
            out = self.m.eth_call(addr, "0x95d89b41")
        except Exception:
            return None
        try:
            raw = bytes.fromhex(out[2:])
            if len(raw) >= 96:
                ln = int.from_bytes(raw[32:64], "big")
                return raw[64:64 + ln].decode("utf-8", "replace") or None
            if len(raw) == 32:  # bytes32 symbol
                return raw.rstrip(b"\0").decode("utf-8", "replace") or None
        except (ValueError, UnicodeDecodeError):
            pass
        return None

    def block_ts(self, n: int) -> int:
        if n not in self._ts:
            self._ts[n] = h2i(self.m.call_tool("get_block", {"block": n})["timestamp"])
        return self._ts[n]

    def balance_of(self, token: str, holder: str, block: int) -> int:
        key = (token, holder, block)
        if key not in self._bal:
            self._bal[key] = h2i(self.m.eth_call(token, "0x70a08231" + pad(holder)[2:], block) or "0x0")
        return self._bal[key]

    def native(self, addr: str, block: int) -> int:
        return h2i(self.m.call_tool("get_balance", {"address": addr, "block": block})["balanceWei"])

    def label(self, addr: str) -> str:
        """Config label first; otherwise classify via get_code (+ symbol() for token contracts)."""
        if addr in self.labels and self.labels[addr]:
            return self.labels[addr]
        if addr not in self._label_cache:
            code = self.m.call_tool("get_code", {"address": addr})
            code = code.get("code", code) if isinstance(code, dict) else code
            if code in ("0x", "", None):
                self._label_cache[addr] = "unlabelled wallet (EOA)"
            else:
                sym = self._symbol(addr)
                self._label_cache[addr] = f"{sym} token contract" if sym else "unlabelled contract"
        return self._label_cache[addr]

    # ---- scanning ----------------------------------------------------------
    def fetch_moves(self, frm: int, to: int) -> list[dict]:
        """All Transfer events of the configured tokens touching a watched address in [frm, to]."""
        addrs = list(self.tokens)
        watched_topics = [pad(a) for a in self.watched]
        seen, moves = set(), []
        for start in range(frm, to + 1, MAX_RANGE):
            end = min(start + MAX_RANGE - 1, to)
            for topics in ([TRANSFER, watched_topics], [TRANSFER, None, watched_topics]):
                for lg in self.m.get_logs(addrs, topics, start, end):
                    key = (lg["transactionHash"], h2i(lg["logIndex"]))
                    if key in seen or lg.get("removed") or len(lg["topics"]) != 3:
                        continue  # dedupe (a watched->watched transfer matches both queries); skip ERC-721
                    seen.add(key)
                    token = lg["address"].lower()
                    if token not in self.tokens or lg["topics"][0].lower() != TRANSFER:
                        continue
                    t = self.tokens[token]
                    frm_a, to_a = "0x" + lg["topics"][1][26:].lower(), "0x" + lg["topics"][2][26:].lower()
                    raw = h2i(lg["data"]) if lg["data"] not in ("0x", "") else 0
                    moves.append({"tx": lg["transactionHash"].lower(), "log_index": key[1],
                                  "block": h2i(lg["blockNumber"]),
                                  "ts": h2i(lg["blockTimestamp"]) if lg.get("blockTimestamp") else None,
                                  "token": token, "symbol": t["symbol"], "raw": raw,
                                  "amount": raw / 10 ** t["decimals"], "from": frm_a, "to": to_a})
        moves.sort(key=lambda x: (x["block"], x["log_index"]))
        return moves

    def is_significant(self, mv: dict) -> tuple[bool, str]:
        t = self.tokens[mv["token"]]
        if mv["amount"] >= t["threshold"]:
            return True, f">= {fmt(t['threshold'])} {t['symbol']} token threshold"
        if self.cfg["min_usd"] is not None and t["usd_price"] is not None:
            usd = mv["amount"] * t["usd_price"]
            if usd >= self.cfg["min_usd"]:
                return True, f"~${usd:,.0f} >= ${self.cfg['min_usd']:,.0f} value threshold"
        return False, ""

    def context(self, mv: dict, who: str, direction: str) -> str:
        """Why it matters: new position / exit / share of the holding moved."""
        before = self.balance_of(mv["token"], who, mv["block"] - 1)
        after = self.balance_of(mv["token"], who, mv["block"])
        dec = self.tokens[mv["token"]]["decimals"]
        b, a = before / 10 ** dec, after / 10 ** dec
        if before == 0 and after == 0:
            return (f"PASS-THROUGH - held no {mv['symbol']} before or after this block "
                    f"(tokens {'received and forwarded' if direction == 'in' else 'forwarded right after receipt'} in the same block)")
        if direction == "out" and mv["raw"] > before:
            return f"forwards {mv['symbol']} received in the same block (held {fmt(b)} before, {fmt(a)} after)"
        if direction == "in":
            if before == 0:
                return f"NEW POSITION - held no {mv['symbol']} before this block (now {fmt(a)})"
            return f"adds {pct(mv['raw'] / before)} to the {fmt(b)} {mv['symbol']} held before (now {fmt(a)})"
        if after == 0 or (before and after / before < 0.01):
            return f"EXIT - {mv['symbol']} balance went from {fmt(b)} to {fmt(a)} in this block"
        share = mv["raw"] / before if before else 1
        return f"moves {pct(share)} of the {fmt(b)} {mv['symbol']} held before (now {fmt(a)})"

    def run_window(self, frm: int, to: int) -> dict:
        moves = self.fetch_moves(frm, to)
        by_tx = defaultdict(list)
        for mv in moves:
            by_tx[mv["tx"]].append(mv)
        reports = []
        stats = {a: {"label": l, "moves": 0, "significant": 0, "tokens": defaultdict(lambda: [0.0, 0.0]),
                     "largest": None, "last_block": None} for a, l in self.watched.items()}
        for mv in moves:
            for who, direction in ((mv["from"], "out"), (mv["to"], "in")):
                if who not in self.watched:
                    continue
                st = stats[who]
                st["moves"] += 1
                st["tokens"][mv["symbol"]][0 if direction == "in" else 1] += mv["amount"]
                st["last_block"] = mv["block"]
                sig, why = self.is_significant(mv)
                if not sig:
                    continue
                st["significant"] += 1
                other = mv["to"] if direction == "out" else mv["from"]
                # same tx: watched address also moved another token the other way -> swap
                legs = [x for x in by_tx[mv["tx"]] if x["token"] != mv["token"]
                        and ((direction == "out" and x["to"] == who) or (direction == "in" and x["from"] == who))]
                swap = None
                if legs:
                    leg = max(legs, key=lambda x: x["amount"])
                    swap = (f"part of a swap: {fmt(mv['amount'])} {mv['symbol']} -> {fmt(leg['amount'])} {leg['symbol']}"
                            if direction == "out" else
                            f"part of a swap: {fmt(leg['amount'])} {leg['symbol']} -> {fmt(mv['amount'])} {mv['symbol']}")
                kind = "mint to" if mv["from"] == ZERO else "burn by" if mv["to"] == ZERO else (
                    "sent" if direction == "out" else "received")
                rep = {"watched": who, "watched_label": self.watched[who] or self.label(who),
                       "direction": direction, "kind": kind, "token": mv["symbol"], "token_address": mv["token"],
                       "amount": mv["amount"], "raw": str(mv["raw"]), "counterparty": other,
                       "counterparty_label": self.label(other), "block": mv["block"],
                       "time": utc(mv["ts"] or self.block_ts(mv["block"])), "tx": mv["tx"],
                       "link": f"{EXPLORER}/tx/{mv['tx']}", "why_significant": why,
                       "context": self.context(mv, who, direction), "swap": swap}
                usd_p = self.tokens[mv["token"]]["usd_price"]
                rep["usd_estimate"] = round(mv["amount"] * usd_p, 2) if usd_p is not None else None
                reports.append(rep)
                if not st["largest"] or rep["amount"] * (usd_p or 0) > st["largest"][1]:
                    st["largest"] = (rep, rep["amount"] * (usd_p or 0))
        # native PROS change over the window
        native = {}
        for a in self.watched:
            b0, b1 = self.native(a, frm - 1), self.native(a, to)
            native[a] = (b1 - b0) / WEI
        return {"from_block": frm, "to_block": to, "from_time": utc(self.block_ts(frm)), "to_time": utc(self.block_ts(to)),
                "transfers_seen": len(moves), "reports": reports, "stats": stats, "native_change": native}


# ---- rendering --------------------------------------------------------------
def render_report(r: dict) -> str:
    usd = f" (~${r['usd_estimate']:,.0f})" if r.get("usd_estimate") is not None else ""
    lines = [f"[WHALE MOVE] {r['time']}  block #{r['block']:,}",
             f"  who     : {r['watched']}  [{r['watched_label']}]",
             f"  what    : {r['kind']} {fmt(r['amount'])} {r['token']}{usd}",
             f"  {'to   ->' if r['direction'] == 'out' else 'from <-'} : {r['counterparty']}  [{r['counterparty_label']}]",
             f"  why     : {r['why_significant']}; {r['context']}"]
    if r.get("swap"):
        lines.append(f"  note    : {r['swap']}")
    lines.append(f"  tx      : {r['link']}")
    return "\n".join(lines)


def render_summary(res: dict, cfg: dict, native_thr: float) -> str:
    out = [f"=== Summary: blocks #{res['from_block']:,}-#{res['to_block']:,} ({res['from_time']} -> {res['to_time']})",
           f"    {res['transfers_seen']} transfers touching watched addresses, {len(res['reports'])} significant",
           ""]
    for a, st in res["stats"].items():
        nat = res["native_change"][a]
        flag = "  <-- significant" if native_thr and abs(nat) >= native_thr else ""
        out.append(f"* {a}  [{st['label']}]")
        out.append(f"    transfers {st['moves']}, significant {st['significant']}, native PROS change {nat:+,.4f}{flag}")
        for sym, (i, o) in sorted(st["tokens"].items()):
            out.append(f"    {sym:6} in {fmt(i):>18}  out {fmt(o):>18}  net {i - o:+,.4f}")
        if st["largest"]:
            r = st["largest"][0]
            out.append(f"    largest: {r['kind']} {fmt(r['amount'])} {r['token']} {'to' if r['direction'] == 'out' else 'from'} "
                       f"{r['counterparty_label']}  {r['link']}")
        if not st["moves"]:
            out.append("    no token transfers in this window")
    return "\n".join(out)


def render_markdown(res: dict, cfg: dict) -> str:
    md = [f"# Whale watch — blocks {res['from_block']:,}–{res['to_block']:,}",
          f"{res['from_time']} → {res['to_time']} · Pharos mainnet · data via Pharos Chain MCP", "",
          "## Summary across watched addresses", "",
          "| Address | Label | Transfers | Significant | Net per token | Native PROS change |", "|---|---|---|---|---|---|"]
    for a, st in res["stats"].items():
        net = ", ".join(f"{s} {i - o:+,.2f}" for s, (i, o) in sorted(st["tokens"].items())) or "—"
        md.append(f"| [`{a[:8]}…{a[-4:]}`]({EXPLORER}/address/{a}) | {st['label']} | {st['moves']} | {st['significant']} "
                  f"| {net} | {res['native_change'][a]:+,.4f} |")
    md += ["", "## Significant moves", "", "| Time | Watched | Move | Counterparty | Why it matters | Tx |",
           "|---|---|---|---|---|---|"]
    for r in res["reports"]:
        md.append(f"| {r['time']} | {r['watched_label']} | {r['kind']} {fmt(r['amount'])} {r['token']} | "
                  f"{r['counterparty_label']} (`{r['counterparty'][:8]}…`) | {r['context']}"
                  f"{'; ' + r['swap'] if r.get('swap') else ''} | [{r['tx'][:10]}…]({r['link']}) |")
    if not res["reports"]:
        md.append("| — | — | no significant moves | — | — | — |")
    return "\n".join(md) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Track Pharos addresses and report significant token moves (read-only, Chain MCP).")
    ap.add_argument("--config", default="whales.json", help="watch-list config (JSON)")
    ap.add_argument("--blocks", type=int, default=2000, help="look-back window in blocks (default 2000, ~35 min)")
    ap.add_argument("--follow", action="store_true", help="after the look-back, keep polling for new blocks")
    ap.add_argument("--interval", type=float, default=30, help="poll interval in seconds for --follow (default 30)")
    ap.add_argument("--json", dest="json_out", help="write the window result to this JSON file")
    ap.add_argument("--markdown", help="write the summary + significant moves to this Markdown file")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Chain MCP endpoint (https)")
    a = ap.parse_args(argv)
    if not 1 <= a.blocks <= 20000:
        ap.error("--blocks must be between 1 and 20000 (get_logs is limited to 1000 blocks per call, 120 req/min)")
    if a.interval < 5:
        ap.error("--interval must be >= 5 seconds")
    cfg = load_config(a.config)

    mcp = PharosMCP(a.endpoint)
    ww = WhaleWatcher(mcp, cfg)
    head = mcp.block_number()
    print(f"Pharos Whale-Watching Agent · {mcp.server_info.get('name')} {mcp.server_info.get('version')} @ {a.endpoint}")
    print(f"  watching {len(ww.watched)} addresses, tokens: "
          + ", ".join(f"{t['symbol']} (>= {fmt(t['threshold'])})" for t in ww.tokens.values())
          + (f", or >= ${cfg['min_usd']:,.0f} where a usd_price is set" if cfg["min_usd"] is not None else "")
          + (f", native PROS change >= {fmt(cfg['native_threshold'])}" if cfg["native_threshold"] else ""))
    frm, to = head - a.blocks + 1, head
    while True:
        res = ww.run_window(frm, to)
        print()
        for r in res["reports"]:
            print(render_report(r) + "\n")
        print(render_summary(res, cfg, cfg["native_threshold"]))
        if a.json_out:
            ser = json.loads(json.dumps(res, default=lambda o: dict(o) if isinstance(o, defaultdict) else str(o)))
            for st in ser["stats"].values():
                st["largest"] = st["largest"][0]["tx"] if st["largest"] else None
            with open(a.json_out, "w", encoding="utf-8") as fh:
                json.dump(ser, fh, indent=2)
        if a.markdown:
            with open(a.markdown, "w", encoding="utf-8") as fh:
                fh.write(render_markdown(res, cfg))
        if not a.follow:
            return 0
        while True:
            time.sleep(a.interval)
            try:
                head = mcp.block_number()
            except Exception as e:
                print(f"poll error: {e}", file=sys.stderr)
                continue
            if head > to:
                frm, to = to + 1, head
                break


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
