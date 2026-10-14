#!/usr/bin/env python3
"""Trend Watcher — a Skill with memory: act only when a trend is confirmed over several observations.

    python3 scripts/trend.py observe  --config assets/wpros_down.json          # one live observation (cron it)
    python3 scripts/trend.py watch    --config assets/wpros_down.json --interval 60 --count 10
    python3 scripts/trend.py backfill --config assets/wpros_down.json --from-block N --to-block M --every 600
    python3 scripts/trend.py history  --config assets/wpros_down.json [--last 30]
    python3 scripts/trend.py events   --config assets/wpros_down.json

Each observation is a value read through the hosted Pharos Chain MCP (https://mcp.pharos.xyz/mcp) at a specific
block (WPROS/USDC pool price, Chainlink PROS/USD, Morpho market utilization/liquidity, an ERC-20 balance) and is
stored in SQLite (`--db`, default trend_state.db) keyed by (series, block): a restart, a re-run or an overlapping
backfill can neither lose nor duplicate history. After every insert the rule is re-evaluated over the stored,
block-ordered history:
  * consecutive: `moves` (>= 2, i.e. >= 3 observations) consecutive moves in `direction`, each at least
    `min_step_pct` and at most `max_step_pct` (bigger jumps are outliers and reset the streak), together at least
    `min_total_pct`, no gap larger than `max_gap_blocks`;
  * sustained:   value `op` `threshold` for `observations` (>= 3) consecutive observations.
A trigger is recorded once (unique per series/rule/trigger block) at the observation that completes the trend;
a live trigger then runs the action (alert to alerts.jsonl, optionally an UNSIGNED ERC-20 transfer — never signed
or sent). Triggers found while backfilling history are recorded as historical and do not act.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from pay_core import Chain, PaymentError, check_address, h2i, render as render_payment  # noqa: E402
from skill_chain import Reader, WPROS_USDC_POOL, CHAINLINK_PROS_USD, addr_link, cast_send_command, utc  # noqa: E402

OPS = {"lt": lambda a, b: a < b, "lte": lambda a, b: a <= b, "gt": lambda a, b: a > b, "gte": lambda a, b: a >= b}
SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
  series TEXT NOT NULL, block INTEGER NOT NULL, block_ts INTEGER NOT NULL, value TEXT NOT NULL,
  source TEXT NOT NULL, mode TEXT NOT NULL, observed_at INTEGER NOT NULL, PRIMARY KEY (series, block));
CREATE TABLE IF NOT EXISTS triggers (
  series TEXT NOT NULL, rule TEXT NOT NULL, trigger_block INTEGER NOT NULL, block_ts INTEGER NOT NULL,
  first_block INTEGER NOT NULL, observations INTEGER NOT NULL, detail TEXT NOT NULL, historical INTEGER NOT NULL,
  action TEXT, recorded_at INTEGER NOT NULL, PRIMARY KEY (series, rule, trigger_block));
"""


# ---------------------------------------------------------------- config
def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        c = json.load(f)
    for k in ("series", "source", "rule"):
        if k not in c:
            raise ValueError(f"config is missing {k!r}")
    s = c["source"]
    if s.get("type") not in SOURCES:
        raise ValueError(f"unknown source type {s.get('type')!r}; known: {', '.join(SOURCES)}")
    r = c["rule"]
    r.setdefault("id", r.get("type", "rule"))
    if r.get("type") == "consecutive":
        if r.get("direction") not in ("down", "up"):
            raise ValueError("consecutive rule needs direction 'down' or 'up'")
        if int(r.get("moves", 0)) < 2:
            raise ValueError("consecutive rule needs moves >= 2 (that is >= 3 observations)")
        for k in ("min_step_pct", "min_total_pct", "max_step_pct"):
            Decimal(str(r.get(k, "0")))
    elif r.get("type") == "sustained":
        if r.get("op") not in OPS:
            raise ValueError(f"sustained rule needs op in {', '.join(OPS)}")
        Decimal(str(r["threshold"]))
        if int(r.get("observations", 0)) < 3:
            raise ValueError("sustained rule needs observations >= 3")
    else:
        raise ValueError("rule.type must be 'consecutive' or 'sustained'")
    if int(r.get("max_gap_blocks", 10**9)) < 1:
        raise ValueError("max_gap_blocks must be >= 1")
    return c


# ---------------------------------------------------------------- sources (read via Chain MCP at a block)
def s_pool_price(r: Reader, p: dict, block: int):
    pool = check_address(p.get("pool", WPROS_USDC_POOL), "pool")
    v, m = r.pool_price(pool, block)
    return v, f"{pool} slot0() {m['token0']}/{m['token1']}"


def s_oracle_price(r: Reader, p: dict, block: int):
    feed = check_address(p.get("feed", CHAINLINK_PROS_USD), "feed")
    v, m = r.oracle(feed, block)
    return v, f"{feed} latestRoundData() updatedAt {m['updatedAt']}"


def s_morpho_utilization_pct(r: Reader, p: dict, block: int):
    m, meta = r.morpho_market(p["market_id"], block)
    return m["utilization"] * 100, meta["source"]


def s_morpho_liquidity(r: Reader, p: dict, block: int):
    m, meta = r.morpho_market(p["market_id"], block)
    return m["liquidity"], meta["source"]


def s_erc20_balance(r: Reader, p: dict, block: int):
    v, m = r.erc20_balance(check_address(p["token"], "token"), check_address(p["owner"], "owner"), block)
    return v, m["source"]


SOURCES = {f.__name__[2:]: f for f in (s_pool_price, s_oracle_price, s_morpho_utilization_pct, s_morpho_liquidity,
                                        s_erc20_balance)}


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def add(self, series: str, block: int, ts: int, value: Decimal, source: str, mode: str) -> bool:
        with self.db:  # one transaction; PK (series, block) makes re-inserts no-ops
            cur = self.db.execute("INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?)",
                                  (series, block, ts, str(value), source, mode, int(time.time())))
        return cur.rowcount == 1

    def history(self, series: str) -> list[dict]:
        rows = self.db.execute("SELECT block, block_ts, value, mode, source FROM observations WHERE series=? ORDER BY block",
                               (series,)).fetchall()
        return [{"block": b, "ts": t, "value": Decimal(v), "mode": m, "src": src} for b, t, v, m, src in rows]

    def has_block(self, series: str, block: int) -> bool:
        return self.db.execute("SELECT 1 FROM observations WHERE series=? AND block=?", (series, block)).fetchone() is not None

    def record_trigger(self, series: str, rule: str, t: dict, historical: bool) -> bool:
        with self.db:
            cur = self.db.execute("INSERT OR IGNORE INTO triggers VALUES (?,?,?,?,?,?,?,?,?,?)",
                                  (series, rule, t["block"], t["ts"], t["first_block"], t["observations"], t["detail"],
                                   int(historical), None, int(time.time())))
        return cur.rowcount == 1

    def set_action(self, series: str, rule: str, block: int, action: dict) -> None:
        with self.db:
            self.db.execute("UPDATE triggers SET action=? WHERE series=? AND rule=? AND trigger_block=?",
                            (json.dumps(action, default=str), series, rule, block))

    def triggers(self, series: str) -> list[tuple]:
        return self.db.execute("SELECT trigger_block, block_ts, first_block, observations, detail, historical, action "
                               "FROM triggers WHERE series=? ORDER BY trigger_block", (series,)).fetchall()


# ---------------------------------------------------------------- the rule (pure function of the stored history)
def evaluate(hist: list[dict], rule: dict) -> tuple[list[dict], list[dict]]:
    """Walk the block-ordered history. Returns (annotated rows, triggers). A trigger is the observation at which
    the streak first reaches the required length; longer streaks do not re-trigger until the streak breaks."""
    rows, trigs = [], []
    gap = int(rule.get("max_gap_blocks", 10**9))
    streak, start, cum = 0, None, Decimal(0)
    for i, h in enumerate(hist):
        row = {**h, "move_pct": None, "streak": 0, "trigger": False,
               "note": ("[" + h["src"].split(" | ", 1)[1] + "] ") if " | " in h.get("src", "") else ""}
        prev = hist[i - 1] if i else None
        if prev and h["block"] - prev["block"] > gap:
            streak, start, cum = 0, None, Decimal(0)
            row["note"] += f"gap {h['block'] - prev['block']} blocks > {gap}: streak reset"
            prev = None
        if rule["type"] == "consecutive":
            need = int(rule["moves"])
            if prev is not None and prev["value"] != 0:
                mv = (h["value"] - prev["value"]) / prev["value"] * 100
                row["move_pct"] = mv
                ok = (mv < 0 if rule["direction"] == "down" else mv > 0) and abs(mv) >= Decimal(str(rule.get("min_step_pct", 0)))
                cap = rule.get("max_step_pct")
                if ok and cap is not None and abs(mv) > Decimal(str(cap)):
                    ok = False
                    row["note"] += f"|move| {abs(mv):.4f}% > max_step_pct {cap}: outlier, streak reset"
                if ok:
                    if streak == 0:
                        start, cum = prev, Decimal(0)
                    streak += 1
                    cum = (h["value"] - start["value"]) / start["value"] * 100
                else:
                    streak, start, cum = 0, None, Decimal(0)
            row["streak"] = streak
            if streak == need:
                if abs(cum) >= Decimal(str(rule.get("min_total_pct", 0))):
                    row["trigger"] = True
                    trigs.append({"block": h["block"], "ts": h["ts"], "first_block": start["block"],
                                  "observations": need + 1,
                                  "detail": f"{need} consecutive {rule['direction']} moves {start['value']:.6f} -> "
                                            f"{h['value']:.6f} ({cum:+.4f}%) from block {start['block']} to {h['block']}"})
                else:
                    row["note"] += f"{need} moves but total {cum:+.4f}% < min_total_pct {rule.get('min_total_pct')}"
        else:
            need = int(rule["observations"])
            ok = OPS[rule["op"]](h["value"], Decimal(str(rule["threshold"])))
            if ok:
                if streak == 0:
                    start = h
                streak += 1
            else:
                streak, start = 0, None
            row["streak"] = streak
            if streak == need:
                row["trigger"] = True
                trigs.append({"block": h["block"], "ts": h["ts"], "first_block": start["block"], "observations": need,
                              "detail": f"value {rule['op']} {rule['threshold']} for {need} consecutive observations "
                                        f"(blocks {start['block']}..{h['block']})"})
        rows.append(row)
    return rows, trigs


# ---------------------------------------------------------------- run helpers
def need_obs(rule: dict) -> int:
    return int(rule["moves"]) + 1 if rule["type"] == "consecutive" else int(rule["observations"])


def observe_at(cfg: dict, store: Store, r: Reader, block: int, mode: str) -> tuple[bool, Decimal, int]:
    if store.has_block(cfg["series"], block):
        h = next(x for x in store.history(cfg["series"]) if x["block"] == block)
        return False, h["value"], h["ts"]
    ts = h2i(r.c.block(block)["timestamp"])
    read = SOURCES[cfg["source"]["type"]]
    params = cfg["source"].get("params", {})
    v, src = read(r, params, block)
    # Source sanity check: a reading that jumps more than recheck_pct from the neighbouring stored observation is
    # read again (twice if needed) and the value two reads agree on is kept. The hosted endpoint has been seen to
    # return an inconsistent archive answer now and then; a real move survives the re-read, a glitch does not.
    lim = Decimal(str(cfg["source"].get("recheck_pct", "2")))
    hist = store.history(cfg["series"])
    prev = max((h for h in hist if h["block"] < block), key=lambda h: h["block"], default=None)
    if prev and prev["value"] and abs(v - prev["value"]) / prev["value"] * 100 > lim:
        v2, _ = read(r, params, block)
        if v2 != v:
            v3, _ = read(r, params, block)
            agreed = v2 if v2 == v3 else (v if v == v3 else None)
            if agreed is None:
                raise RuntimeError(f"block {block}: three reads disagree ({v}, {v2}, {v3}) - not stored")
            src += f" | re-read: first answer {v:.6f} rejected, {agreed:.6f} confirmed"
            v = agreed
        else:
            src += f" | re-read confirmed the {abs(v - prev['value']) / prev['value'] * 100:.2f}% jump"
    added = store.add(cfg["series"], block, ts, v, src, mode)
    return added, v, ts


def check_rule(cfg: dict, store: Store, chain: Chain | None, live: bool, alerts: str) -> list[dict]:
    hist = store.history(cfg["series"])
    _, trigs = evaluate(hist, cfg["rule"])
    head_block = hist[-1]["block"] if hist else None
    new = []
    for t in trigs:
        historical = not (live and t["block"] == head_block)
        if store.record_trigger(cfg["series"], cfg["rule"]["id"], t, historical):
            t["historical"] = historical
            if not historical:
                t["action"] = act(cfg, chain, t, alerts)
                store.set_action(cfg["series"], cfg["rule"]["id"], t["block"], t["action"])
            new.append(t)
    return new


def act(cfg: dict, chain: Chain | None, t: dict, alerts: str) -> dict:
    alert = {"series": cfg["series"], "rule": cfg["rule"]["id"], "trigger_block": t["block"], "time": utc(t["ts"]),
             "detail": t["detail"]}
    with open(alerts, "a", encoding="utf-8") as f:
        f.write(json.dumps(alert) + "\n")
    a = cfg.get("action", {"type": "alert"})
    out = {"type": "alert", "written_to": alerts}
    if a.get("type") == "erc20_transfer" and chain is not None:
        try:
            p = chain.prepare(a["from"], a["to"], a.get("token", "USDC"), Decimal(str(a["amount"])),
                              reference=a.get("reference"))
            out.update(type="erc20_transfer", prepared=p.to_json(),
                       cast=cast_send_command(p.tx["to"], p.tx["data"], p.tx["value"]),
                       summary=render_payment(p, f"Prepared action: {a.get('label', 'transfer')}"))
        except PaymentError as e:
            out.update(type="erc20_transfer", error=f"action pre-check failed: {e}")
    return out


def print_trigger(t: dict) -> None:
    tag = "historical (recorded, no action)" if t["historical"] else "LIVE"
    print(f"*** TREND CONFIRMED [{tag}] at block {t['block']:,} ({utc(t['ts'])}): {t['detail']}")
    a = t.get("action")
    if a:
        print(f"    action: alert written to {a['written_to']}")
        if a.get("summary"):
            print(a["summary"])
            print(f"    sign it yourself (never done here): {a['cast']}")
        elif a.get("error"):
            print(f"    {a['error']}")


def print_history(cfg: dict, store: Store, last: int | None, single: Decimal | None = None) -> None:
    hist = store.history(cfg["series"])
    rows, trigs = evaluate(hist, cfg["rule"])
    rule = cfg["rule"]
    print(f"Series '{cfg['series']}' ({cfg['source']['type']}) · rule {rule['id']}: {json.dumps(rule)}")
    print(f"{len(rows)} stored observations (needs {need_obs(rule)} to trigger)")
    print(f"{'block':>11} {'block time (UTC)':<20} {'value':>14} {'move %':>9} {'streak':>6}  mode      note")
    shown = rows[-last:] if last else rows
    for x in shown:
        mv = f"{x['move_pct']:+.4f}" if x["move_pct"] is not None else ""
        mark = "  <== TRIGGER" if x["trigger"] else ""
        if single is not None and x["move_pct"] is not None and abs(x["move_pct"]) >= single:
            mark += f"  (a single-reading rule |move| >= {single}% would fire here)"
        print(f"{x['block']:>11,} {utc(x['ts'])[:19]:<20} {x['value']:>14.6f} {mv:>9} {x['streak']:>6}  "
              f"{x['mode']:<9} {x['note']}{mark}")
    st = store.triggers(cfg["series"])
    print(f"\nrecorded triggers: {len(st)}")
    for b, ts, fb, n, d, histo, action in st:
        print(f"  block {b:,} ({utc(ts)}) {'historical' if histo else 'LIVE'}: {d}" + (" [action ran]" if action else ""))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("observe", "watch", "backfill", "history", "events"):
        s = sub.add_parser(n)
        s.add_argument("--config", required=True)
        s.add_argument("--db", default="trend_state.db")
        s.add_argument("--alerts", default="alerts.jsonl")
        s.add_argument("--endpoint", default=None)
        if n == "watch":
            s.add_argument("--interval", type=int, default=60, help="seconds between observations (>= 5)")
            s.add_argument("--count", type=int, default=10)
        if n == "backfill":
            s.add_argument("--from-block", type=int, required=True)
            s.add_argument("--to-block", type=int, required=True)
            s.add_argument("--every", type=int, default=600, help="blocks between observations")
        if n == "history":
            s.add_argument("--last", type=int, default=None)
            s.add_argument("--compare-single", type=Decimal, default=None, metavar="PCT",
                           help="mark rows where a naive one-observation rule (|move| >= PCT) would have fired")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
    except (ValueError, OSError, json.JSONDecodeError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    store = Store(a.db)
    if a.cmd == "history":
        print_history(cfg, store, a.last, a.compare_single)
        return 0
    if a.cmd == "events":
        for b, ts, fb, n, d, histo, action in store.triggers(cfg["series"]):
            print(json.dumps({"trigger_block": b, "time": utc(ts), "first_block": fb, "observations": n, "detail": d,
                              "historical": bool(histo), "action": json.loads(action) if action else None}))
        return 0
    chain = Chain(a.endpoint)
    r = Reader(chain)
    if a.cmd == "backfill":
        if a.every < 1 or a.from_block > a.to_block or (a.to_block - a.from_block) // a.every > 2000:
            print("error: need from <= to, every >= 1 and at most 2000 observations per backfill", file=sys.stderr)
            return 2
        head = chain.block_number()
        if a.to_block > head:
            print(f"error: to-block {a.to_block} is above the head {head}", file=sys.stderr)
            return 2
        added = 0
        for b in range(a.from_block, a.to_block + 1, a.every):
            ok, v, ts = observe_at(cfg, store, r, b, "backfill")
            added += ok
        print(f"backfill {a.from_block:,}..{a.to_block:,} every {a.every}: {added} new observations "
              f"({len(range(a.from_block, a.to_block + 1, a.every)) - added} already stored)")
        for t in check_rule(cfg, store, chain, live=False, alerts=a.alerts):
            print_trigger(t)
        return 0
    rounds = a.count if a.cmd == "watch" else 1
    if a.cmd == "watch" and (a.interval < 5 or not 1 <= a.count <= 10_000):
        print("error: --interval >= 5 and 1 <= --count <= 10000", file=sys.stderr)
        return 2
    for i in range(rounds):
        head = chain.block_number()
        ok, v, ts = observe_at(cfg, store, r, head, "live")
        hist = store.history(cfg["series"])
        rows, _ = evaluate(hist, cfg["rule"])
        last = next(x for x in rows if x["block"] == head)
        mv = f"{last['move_pct']:+.4f}%" if last["move_pct"] is not None else "n/a"
        need = need_obs(cfg["rule"]) - (1 if cfg["rule"]["type"] == "consecutive" else 0)
        st = f"{last['streak']}/{need}" if last["streak"] <= need else f"{last['streak']} (>= {need}, already triggered)"
        print(f"[{utc()}] block {head:,} value {v:.6f} move {mv} streak {st} "
              f"{'stored' if ok else 'already stored (duplicate ignored)'}; history {len(hist)} obs")
        for t in check_rule(cfg, store, chain, live=True, alerts=a.alerts):
            print_trigger(t)
        sys.stdout.flush()
        if i + 1 < rounds:
            time.sleep(a.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
