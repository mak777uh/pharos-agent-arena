#!/usr/bin/env python3
"""Threshold Guardian — evaluate several on-chain conditions together, then FIRE or HOLD one action.

    python3 scripts/guardian.py --config assets/guardian.rebalance.json
    python3 scripts/guardian.py --config assets/guardian.rebalance.json --set price_move.threshold=3
    python3 scripts/guardian.py --config ... --json            # machine-readable decision

Every condition is read live through the hosted Pharos Chain MCP (https://mcp.pharos.xyz/mcp), all pinned to
the same block. The decision trace (value, operator, threshold, pass/fail, source) is printed and appended to a
JSONL log. When the guardian FIRES, the configured action is PREPARED as an unsigned EIP-1559 transaction
(after the payment pre-checks in pay_core.prepare); it is never signed or broadcast — the user signs it.

Exit codes: 0 = FIRE (action prepared), 3 = HOLD, 2 = error (bad config / chain error; nothing prepared).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from pay_core import Chain, PaymentError, check_address, h2i, render  # noqa: E402
from skill_chain import (Reader, CHAINLINK_PROS_USD, WPROS_USDC_POOL, cast_send_command, utc)  # noqa: E402

OPS = {"lt": lambda a, b: a < b, "lte": lambda a, b: a <= b, "gt": lambda a, b: a > b,
       "gte": lambda a, b: a >= b, "eq": lambda a, b: a == b}
OP_SYM = {"lt": "<", "lte": "<=", "gt": ">", "gte": ">=", "eq": "=="}


# ---------------------------------------------------------------- condition readers
# Each reader gets (reader, params, block, head_ts) and returns (value: Decimal, unit, detail, source).
def c_pool_price_change_pct(r: Reader, p: dict, block: int, head_ts: int):
    pool = check_address(p.get("pool", WPROS_USDC_POOL), "pool")
    lb = int(p.get("lookback_blocks", 3600))
    if not 1 <= lb <= 2_000_000:
        raise ValueError("lookback_blocks must be 1..2,000,000")
    now, meta = r.pool_price(pool, block)
    then, _ = r.pool_price(pool, block - lb)
    chg = abs(now - then) / then * 100
    if p.get("signed"):
        chg = (now - then) / then * 100
    return (chg, "%", f"{meta['token0']}/{meta['token1']} {then:.6f} @#{block - lb:,} -> {now:.6f} @#{block:,}",
            f"{pool} slot0() @ {block - lb} and @ {block}")


def c_pool_oracle_deviation_pct(r: Reader, p: dict, block: int, head_ts: int):
    pool = check_address(p.get("pool", WPROS_USDC_POOL), "pool")
    feed = check_address(p.get("feed", CHAINLINK_PROS_USD), "feed")
    px, meta = r.pool_price(pool, block)
    ox, _ = r.oracle(feed, block)
    dev = abs(px - ox) / ox * 100
    return dev, "%", f"pool {px:.6f} vs Chainlink {ox:.6f}", f"{pool} slot0() + {feed} latestRoundData() @ {block}"


def c_oracle_age_seconds(r: Reader, p: dict, block: int, head_ts: int):
    feed = check_address(p.get("feed", CHAINLINK_PROS_USD), "feed")
    ox, meta = r.oracle(feed, block)
    age = Decimal(head_ts - meta["updatedAt"])
    return age, "s", f"answer {ox:.6f}, updatedAt {utc(meta['updatedAt'])}", f"{feed} latestRoundData() @ {block}"


def c_gas_price_gwei(r: Reader, p: dict, block: int, head_ts: int):
    g, meta = r.gas_price_gwei()
    return g, "gwei", f"priority fee {meta['priorityFeeWei']} wei", "Chain MCP get_gas_price (latest)"


def c_morpho_liquidity(r: Reader, p: dict, block: int, head_ts: int):
    mid = p["market_id"]
    if not (isinstance(mid, str) and mid.startswith("0x") and len(mid) == 66):
        raise ValueError("market_id must be a 0x-prefixed 32-byte hex id")
    m, meta = r.morpho_market(mid, block)
    return (m["liquidity"], m["loanToken"],
            f"{m['collateralToken']}/{m['loanToken']} supply {m['totalSupply']:,.2f} borrow {m['totalBorrow']:,.2f}",
            meta["source"])


def c_morpho_utilization_pct(r: Reader, p: dict, block: int, head_ts: int):
    mid = p["market_id"]
    m, meta = r.morpho_market(mid, block)
    return m["utilization"] * 100, "%", f"{m['collateralToken']}/{m['loanToken']}", meta["source"]


def c_erc20_balance(r: Reader, p: dict, block: int, head_ts: int):
    tok = check_address(p["token"], "token")
    owner = check_address(p["owner"], "owner")
    v, meta = r.erc20_balance(tok, owner, block)
    return v, meta["token"], f"owner {owner}", meta["source"]


def c_native_balance(r: Reader, p: dict, block: int, head_ts: int):
    owner = check_address(p["owner"], "owner")
    v, meta = r.native_balance(owner)
    return v, "PROS", f"owner {owner}", meta["source"]


READERS = {f.__name__[2:]: f for f in [c_pool_price_change_pct, c_pool_oracle_deviation_pct, c_oracle_age_seconds,
                                       c_gas_price_gwei, c_morpho_liquidity, c_morpho_utilization_pct,
                                       c_erc20_balance, c_native_balance]}


# ---------------------------------------------------------------- config
def load_config(path: str, overrides: list[str]) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    conds = cfg.get("conditions")
    if not isinstance(conds, list) or len(conds) < 1:
        raise ValueError("config needs a non-empty 'conditions' list")
    ids = set()
    for c in conds:
        for k in ("id", "type", "op", "threshold"):
            if k not in c:
                raise ValueError(f"condition {c.get('id', '?')} is missing {k!r}")
        if c["id"] in ids:
            raise ValueError(f"duplicate condition id {c['id']!r}")
        ids.add(c["id"])
        if c["type"] not in READERS:
            raise ValueError(f"unknown condition type {c['type']!r}; known: {', '.join(READERS)}")
        if c["op"] not in OPS:
            raise ValueError(f"unknown op {c['op']!r}; known: {', '.join(OPS)}")
    for ov in overrides:  # --set <id>.<field>=<value>
        key, _, val = ov.partition("=")
        cid, _, fld = key.partition(".")
        target = next((c for c in conds if c["id"] == cid), None)
        if target is None or not fld or not val:
            raise ValueError(f"bad --set {ov!r} (use <condition-id>.<field>=<value>)")
        if fld == "threshold":
            Decimal(val)
            target["threshold"] = val
        elif fld == "op":
            if val not in OPS:
                raise ValueError(f"unknown op {val!r}")
            target["op"] = val
        elif fld == "enabled":
            target["enabled"] = val.lower() in ("1", "true", "yes")
        else:
            target.setdefault("params", {})[fld] = val
    mode = cfg.get("mode", "all")
    if mode not in ("all", "any") and not (isinstance(mode, int) and mode >= 1):
        raise ValueError("mode must be 'all', 'any' or an integer k (at least k conditions must pass)")
    return cfg


# ---------------------------------------------------------------- run
def evaluate(cfg: dict, chain: Chain) -> dict:
    r = Reader(chain)
    head = chain.block_number()
    blk = chain.block(head)
    head_ts = h2i(blk["timestamp"])
    rows = []
    for c in cfg["conditions"]:
        if c.get("enabled", True) is False:
            continue
        thr = Decimal(str(c["threshold"]))
        row = {"id": c["id"], "type": c["type"], "op": c["op"], "threshold": str(thr),
               "description": c.get("description", "")}
        try:
            v, unit, detail, src = READERS[c["type"]](r, c.get("params", {}), head, head_ts)
            row.update(value=str(v.quantize(Decimal("0.000001"))),
                       unit=unit, detail=detail, source=src, passed=OPS[c["op"]](v, thr))
        except Exception as e:  # noqa: BLE001 - a condition we cannot read must never pass
            row.update(value=None, unit="", detail=f"READ ERROR: {str(e)[:160]}", source="", passed=False,
                       error=True)
        rows.append(row)
    n_pass = sum(1 for x in rows if x["passed"])
    mode = cfg.get("mode", "all")
    need = len(rows) if mode == "all" else 1 if mode == "any" else int(mode)
    fire = bool(rows) and n_pass >= need and not any(x.get("error") for x in rows if mode == "all")
    return {"guardian": cfg.get("name", "guardian"), "time": utc(head_ts), "block": head, "blockTimestamp": head_ts,
            "mode": mode, "passed": n_pass, "required": need, "total": len(rows), "conditions": rows,
            "decision": "FIRE" if fire else "HOLD"}


def do_action(cfg: dict, chain: Chain, res: dict) -> dict | None:
    a = cfg.get("action")
    if not a or a.get("type") in (None, "none"):
        return {"type": "none", "note": "no action configured - decision only"}
    if a["type"] != "erc20_transfer":
        raise ValueError(f"unsupported action type {a['type']!r} (supported: erc20_transfer, none)")
    p = chain.prepare(a["from"], a["to"], a.get("token", "USDC"), Decimal(str(a["amount"])),
                      reference=a.get("reference"))
    out = p.to_json()
    out["castCommandForUser"] = cast_send_command(p.tx["to"], p.tx["data"], p.tx["value"])
    return {"type": "erc20_transfer", "summary": render(p, f"Prepared action: {a.get('label', 'transfer')}"),
            "prepared": out}


def print_trace(res: dict) -> None:
    print(f"Threshold Guardian · {res['guardian']} · Pharos mainnet block #{res['block']:,} ({res['time']})")
    print(f"mode: {res['mode']} (need {res['required']} of {res['total']} conditions)")
    print("-" * 100)
    for x in res["conditions"]:
        mark = "PASS" if x["passed"] else "FAIL"
        val = f"{Decimal(x['value']):,.4f} {x['unit']}" if x["value"] is not None else "n/a"
        print(f"[{mark}] {x['id']:<16} {val:>22} {OP_SYM[x['op']]:>2} {Decimal(x['threshold']):,} {x['unit']}")
        print(f"       {x['description']}")
        print(f"       {x['detail']}")
        print(f"       source: {x['source']}")
    print("-" * 100)
    print(f"DECISION: {res['decision']}  ({res['passed']}/{res['total']} passed, {res['required']} required)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="guardian config JSON (conditions, mode, action)")
    ap.add_argument("--set", action="append", default=[], metavar="ID.FIELD=VALUE",
                    help="override a threshold/op/param, e.g. price_move.threshold=3")
    ap.add_argument("--log", default="guardian_log.jsonl", help="JSONL decision log (appended every run)")
    ap.add_argument("--json", action="store_true", help="print the decision record as JSON")
    ap.add_argument("--endpoint", default=None, help="Chain MCP endpoint (https only)")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config, a.set)
        chain = Chain(a.endpoint)
        res = evaluate(cfg, chain)
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if res["decision"] == "FIRE":
        try:
            res["action"] = do_action(cfg, chain, res)
        except (PaymentError, ValueError) as e:
            res["decision"] = "HOLD"
            res["action"] = {"type": "blocked", "note": f"conditions passed but the action failed its pre-checks: {e}"}
    else:
        res["action"] = {"type": "skipped", "note": "HOLD - nothing prepared"}
    rec = {k: v for k, v in res.items()}
    if rec["action"] and "summary" in rec["action"]:
        rec["action"] = {k: v for k, v in rec["action"].items() if k != "summary"}
    with open(a.log, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    if a.json:
        print(json.dumps(rec, indent=2, default=str))
    else:
        print_trace(res)
        act = res["action"]
        if act.get("summary"):
            print()
            print(act["summary"])
            print("Unsigned - sign it yourself (this skill never signs or broadcasts):")
            print(f"  wallet (EIP-1193): {json.dumps(act['prepared']['walletRequest'])}")
            print(f"  or with the official pharos-skill-engine / Foundry: {act['prepared']['castCommandForUser']}")
        else:
            print(f"action: {act['note']}")
        print(f"\nlogged to {a.log}")
    return 0 if res["decision"] == "FIRE" else 3


if __name__ == "__main__":
    sys.exit(main())
