#!/usr/bin/env python3
"""Gas Fee Optimizer for Pharos mainnet (Chain MCP, read-only by default).

Sub-commands
  watch    poll the network gas price (get_gas_price + latest block baseFeePerGas) and show recent history
  history  sample baseFeePerGas of past blocks spread over a time window (backfill, no waiting)
  hold     take a transaction + deadline, hold it until gas is favourable (<= target) or the deadline is reached,
           then SUBMIT: by default a dry run that prints the prepared unsigned EIP-1559 tx with the chosen fee
           fields; with --raw-tx (a transaction YOU signed) and --broadcast it is sent with the Chain MCP tool
           `send_raw_transaction` and the receipt is reported. It never misses the deadline: it submits at the
           last poll that still fits before the deadline, even if gas is high.
  report   compare what a mined transaction actually paid (receipt effectiveGasPrice x gasUsed) with the gas price
           at request time (a block number or a gwei value), in gwei, PROS and USD (Chainlink PROS/USD feed).
This tool never holds or asks for a private key.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pharos_mcp import DEFAULT_ENDPOINT, MCPError, PharosMCP, check_address

__version__ = "1.0.0"
CHAIN_ID = 1672
EXPLORER = "https://www.pharosscan.xyz"
PROS_USD_FEED = "0x371Ed36aEe5bFFbED1A2aa9b7deD7F4EB30F5466"  # Chainlink PROS/USD (docs.pharos.xyz, 18 decimals)
GWEI = 10 ** 9
SPARK = "▁▂▃▄▅▆▇█"


def utc(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def gwei(wei: int | float) -> str:
    return f"{Decimal(wei) / GWEI:,.4f}".rstrip("0").rstrip(".") + " gwei"


def h2i(x) -> int:
    return int(x, 16) if isinstance(x, str) and x.startswith("0x") else int(x)


def parse_deadline(s: str, now: float) -> float:
    m = re.fullmatch(r"(\d+)\s*(s|m|h)", s.strip())
    if m:
        secs = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)]
        if not 10 <= secs <= 7 * 86400:
            raise ValueError("deadline must be between 10 s and 7 days from now")
        return now + secs
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"bad --deadline {s!r}: use e.g. 90s, 15m, 2h or an ISO time like 2026-10-15T08:00:00Z")
    if dt.tzinfo is None:
        raise ValueError("ISO deadline needs a timezone, e.g. ...Z or +03:00")
    t = dt.timestamp()
    if not now + 10 <= t <= now + 7 * 86400:
        raise ValueError("deadline must be between 10 s and 7 days from now")
    return t


class Gas:
    def __init__(self, mcp: PharosMCP, history_file: str | None):
        self.m, self.history_file = mcp, history_file
        self.history: list[dict] = []
        if history_file and os.path.exists(history_file):
            self.history = json.load(open(history_file))[-2000:]

    def sample(self) -> dict:
        gp = self.m.call_tool("get_gas_price")
        blk = self.m.call_tool("get_block", {"block": "latest"})
        s = {"time": time.time(), "block": h2i(blk["number"]), "base_fee": h2i(blk.get("baseFeePerGas", "0x0")),
             "gas_price": int(gp["gasPriceWei"]), "priority": int(gp.get("maxPriorityFeePerGasWei", 0)),
             "gas_used": h2i(blk["gasUsed"]), "gas_limit": h2i(blk["gasLimit"])}
        s["effective"] = s["base_fee"] + s["priority"]
        self.history.append(s)
        if self.history_file:
            with open(self.history_file, "w") as f:
                json.dump(self.history[-2000:], f)
        return s

    def price_at(self, block: int) -> int:
        b = self.m.call_tool("get_block", {"block": block})
        return h2i(b.get("baseFeePerGas", "0x0"))

    def pros_usd(self) -> Decimal | None:
        try:
            return Decimal(int(self.m.eth_call(PROS_USD_FEED, "0x50d25bcd"), 16)) / Decimal(10 ** 18)  # latestAnswer()
        except (MCPError, ValueError):
            return None


def show_history(samples: list[dict], last: int = 20, key: str = "effective") -> str:
    if not samples:
        return "(no samples yet)"
    vals = [s[key] for s in samples]
    lo, hi = min(vals), max(vals)
    spark = "".join(SPARK[0 if hi == lo else int((v - lo) / (hi - lo) * 7)] for v in vals[-60:])
    L = [f"  {'time (UTC)':<19}  {'block':>11}  {'baseFee':>12}  {'tip':>9}  {'eth_gasPrice':>12}  {'block gas used':>15}"]
    for s in samples[-last:]:
        L.append(f"  {utc(s['time'])[:19]:<19}  {s['block']:>11,}  {gwei(s['base_fee']):>12}  {gwei(s.get('priority', 0)):>9}"
                 f"  {gwei(s.get('gas_price', s['base_fee'])):>12}  {s.get('gas_used', 0):>8,}/{s.get('gas_limit', 0) // 10**6:,}M")
    med = statistics.median(vals)
    L.append(f"  {len(vals)} samples · min {gwei(lo)} · median {gwei(med)} · max {gwei(hi)} · trend {spark}")
    if hi == lo:
        L.append(f"  gas price has been FLAT at {gwei(lo)} over this window (no variation observed).")
    return "\n".join(L)


def percentile(vals: list[int], p: float) -> int:
    v = sorted(vals)
    return v[min(len(v) - 1, max(0, int(round(p / 100 * (len(v) - 1)))))]


def cmd_watch(g: Gas, a) -> int:
    for i in range(a.samples):
        s = g.sample()
        print(f"[{utc(s['time'])}] block #{s['block']:,}  baseFee {gwei(s['base_fee'])}  tip {gwei(s['priority'])}"
              f"  eth_gasPrice {gwei(s['gas_price'])}  (block gas used {s['gas_used']:,})")
        if i < a.samples - 1:
            time.sleep(a.interval)
    print("\nRecent history (this file + earlier runs):")
    print(show_history(g.history, a.show))
    return 0


def cmd_history(g: Gas, a) -> int:
    head = g.m.block_number()
    hb = g.m.call_tool("get_block", {"block": head})
    t_head = h2i(hb["timestamp"])
    back = g.m.call_tool("get_block", {"block": max(1, head - 10000)})
    bps = 10000 / max(1, t_head - h2i(back["timestamp"]))  # blocks per second, measured
    span = int(a.hours * 3600 * bps)
    step = max(1, span // max(1, a.points - 1))
    rows = []
    for k in range(a.points - 1, -1, -1):
        n = max(1, head - k * step)
        b = g.m.call_tool("get_block", {"block": n})
        rows.append({"time": h2i(b["timestamp"]), "block": n, "base_fee": h2i(b.get("baseFeePerGas", "0x0")),
                     "priority": 0, "gas_price": h2i(b.get("baseFeePerGas", "0x0")), "gas_used": h2i(b["gasUsed"]),
                     "gas_limit": h2i(b["gasLimit"]), "effective": h2i(b.get("baseFeePerGas", "0x0"))})
    print(f"baseFeePerGas of {a.points} blocks spread over the last {a.hours:g} h "
          f"(~{bps:.2f} blocks/s measured, every {step:,} blocks; tip column not available for past blocks):")
    print(show_history(rows, a.points, key="base_fee"))
    return 0


def submit(g: Gas, a, s: dict, gas_limit: int, req: dict, reason: str) -> int:
    tip = s["priority"] if a.tip_gwei is None else int(Decimal(str(a.tip_gwei)) * GWEI)
    max_fee = s["base_fee"] * 2 + tip  # standard headroom: survives a doubling of the base fee before inclusion
    exp_now, exp_req = gas_limit * (s["base_fee"] + tip), gas_limit * (req["base_fee"] + tip)
    usd = g.pros_usd()
    fx = (lambda w: f"{Decimal(w) / 10**18:.8f} PROS" + (f" (~${Decimal(w) / 10**18 * usd:.6f})" if usd else ""))
    print(f"[{utc()}] SUBMIT ({reason}) at block #{s['block']:,}: baseFee {gwei(s['base_fee'])} + tip {gwei(tip)}")
    print(f"    request-time price {gwei(req['base_fee'] + tip)} at block #{req['block']:,} ({utc(req['time'])})")
    print(f"    expected fee now  {fx(exp_now)}  vs at request time {fx(exp_req)}  "
          f"-> {('saved ' + fx(exp_req - exp_now)) if exp_now < exp_req else ('extra ' + fx(exp_now - exp_req)) if exp_now > exp_req else 'same price'}")
    if not a.broadcast:
        tx = {"chainId": CHAIN_ID, "type": "0x2", "to": a.to, "value": hex(int(Decimal(a.value) * 10**18)), "data": a.data,
              "gas": hex(gas_limit), "maxFeePerGas": hex(max_fee), "maxPriorityFeePerGas": hex(tip)}
        if a.sender:
            tx["from"] = a.sender
        print("    DRY RUN (prepare-only): nothing was signed or sent. Sign this with your own wallet now:")
        print("    " + json.dumps(tx))
        print("    (actual-vs-request comparison for a mined tx: `gas_fee_optimizer.py report --tx <hash> "
              f"--request-block {req['block']}`)")
        return 0
    res = g.m.call_tool("send_raw_transaction", {"signedTransaction": a.raw_tx_hex})
    txh = res.get("transactionHash") if isinstance(res, dict) else res
    print(f"    broadcast via Chain MCP send_raw_transaction -> {txh}")
    for _ in range(60):
        try:
            rc = g.m.call_tool("get_transaction_receipt", {"hash": txh})
            if rc:
                break
        except MCPError:
            pass
        time.sleep(2)
    else:
        print("    no receipt after 120 s; check later with `report`")
        return 1
    return report(g, txh, req["block"], None)


def cmd_hold(g: Gas, a) -> int:
    now = time.time()
    deadline = parse_deadline(a.deadline, now)
    a.to = check_address(a.to)
    if a.sender:
        a.sender = check_address(a.sender)
    if not re.fullmatch(r"0x([0-9a-fA-F]{2})*", a.data):
        raise ValueError("--data must be 0x-prefixed hex bytes")
    if Decimal(a.value) < 0:
        raise ValueError("--value must be >= 0")
    if a.broadcast:
        if not a.raw_tx:
            raise ValueError("--broadcast needs --raw-tx FILE (a transaction you signed yourself)")
        a.raw_tx_hex = open(a.raw_tx).read().strip()
        if not re.fullmatch(r"0x[0-9a-fA-F]{100,}", a.raw_tx_hex):
            raise ValueError("--raw-tx file must contain one 0x-prefixed signed transaction")
    req = g.sample()  # gas price at request time
    est_args = {"transaction": {"to": a.to, "value": hex(int(Decimal(a.value) * 10**18)), "data": a.data}}
    if a.sender:
        est_args["transaction"]["from"] = a.sender
    if a.gas_limit:
        gas_limit = a.gas_limit
    else:
        try:
            est = g.m.call_tool("estimate_gas", est_args)
        except MCPError as e:
            raise ValueError(f"estimate_gas failed ({' '.join(str(e).split())[:200]}); pass --from <funded sender> or --gas-limit N")
        gas_limit = int(int(est["gas"]) * 1.2)  # Chain MCP returns {"gas": "21000", "gasHex": ...}; +20 % margin
    if a.target_gwei is not None:
        target = int(Decimal(str(a.target_gwei)) * GWEI)
        rule = f"target {gwei(target)}"
    else:
        vals = [s["effective"] for s in g.history]
        target = percentile(vals, a.percentile)
        rule = f"p{a.percentile:g} of {len(vals)} recorded samples = {gwei(target)}"
    print(f"[{utc(req['time'])}] REQUEST to={a.to} value={a.value} PROS data={a.data[:18]}{'…' if len(a.data) > 18 else ''}"
          f" gasLimit={gas_limit:,}{' (estimate_gas +20%)' if not a.gas_limit else ''}")
    print(f"    deadline {utc(deadline)} ({int(deadline - now)} s) · favourable when baseFee+tip <= {rule} · poll every {a.interval} s")
    s = req
    while True:
        eff = s["effective"]
        if eff <= target:
            return submit(g, a, s, gas_limit, req, f"gas favourable: {gwei(eff)} <= {gwei(target)}")
        left = deadline - time.time()
        if left <= a.interval + a.safety:
            return submit(g, a, s, gas_limit, req, f"DEADLINE: {int(left)} s left, submitting even though {gwei(eff)} > {gwei(target)}")
        print(f"[{utc(s['time'])}] HOLD block #{s['block']:,} baseFee+tip {gwei(eff)} > {gwei(target)} · {int(left)} s to deadline")
        time.sleep(a.interval)
        s = g.sample()


def report(g: Gas, txh: str, req_block: int | None, req_gwei: float | None) -> int:
    tx = g.m.call_tool("get_transaction", {"hash": txh})
    rc = g.m.call_tool("get_transaction_receipt", {"hash": txh})
    if not tx or not rc:
        raise ValueError(f"transaction {txh} not found / not mined yet")
    used, eff = h2i(rc["gasUsed"]), h2i(rc.get("effectiveGasPrice", tx.get("gasPrice", "0x0")))
    blk = h2i(rc["blockNumber"])
    incl_base = g.price_at(blk)
    if req_block is not None:
        req_price, req_label = g.price_at(req_block), f"baseFee at request block #{req_block:,}"
    else:
        req_price, req_label = int(Decimal(str(req_gwei)) * GWEI), "price given at request time"
    paid, at_req = used * eff, used * req_price
    usd = g.pros_usd()
    fx = (lambda w: f"{Decimal(w) / 10**18:.8f} PROS" + (f" (~${Decimal(w) / 10**18 * usd:.6f})" if usd else ""))
    diff = paid - at_req
    print(f"Gas report for {EXPLORER}/tx/{txh}")
    print(f"    mined in block #{blk:,} · status {'success' if h2i(rc['status']) == 1 else 'FAILED'} · gasUsed {used:,}")
    print(f"    tx fee fields: " + (f"maxFeePerGas {gwei(h2i(tx['maxFeePerGas']))}, maxPriorityFeePerGas {gwei(h2i(tx['maxPriorityFeePerGas']))}"
                                    if tx.get("maxFeePerGas") else f"gasPrice {gwei(h2i(tx['gasPrice']))} (legacy)"))
    print(f"    actual price paid  {gwei(eff)} (baseFee at inclusion {gwei(incl_base)} + tip {gwei(eff - incl_base)})")
    print(f"    {req_label}: {gwei(req_price)}")
    print(f"    actual fee {fx(paid)}  vs at request-time price {fx(at_req)}  -> "
          f"{'paid MORE by ' + fx(diff) if diff > 0 else 'paid LESS by ' + fx(-diff) if diff < 0 else 'identical'}"
          f" ({(Decimal(eff) / Decimal(req_price) - 1) * 100:+.1f}% per gas)" if req_price else "")
    if eff > incl_base:
        print(f"    note: the tip of {gwei(eff - incl_base)} was above the network's suggested tip "
              f"({gwei(int(g.m.call_tool('get_gas_price').get('maxPriorityFeePerGasWei', 0)))}) — the optimizer would have used the suggested tip.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pharos gas fee optimizer (Chain MCP).")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--history-file", default="gas_history.json", help="where samples are kept between runs ('' = memory only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("watch", help="sample gas price over time and show history")
    w.add_argument("--samples", type=int, default=10)
    w.add_argument("--interval", type=int, default=30)
    w.add_argument("--show", type=int, default=20, help="history rows to print")
    h = sub.add_parser("history", help="backfill: baseFee of past blocks over a window")
    h.add_argument("--hours", type=float, default=24)
    h.add_argument("--points", type=int, default=24)
    d = sub.add_parser("hold", help="hold a tx until gas is favourable or the deadline")
    d.add_argument("--to", required=True)
    d.add_argument("--value", default="0", help="PROS to send (decimal)")
    d.add_argument("--data", default="0x")
    d.add_argument("--from", dest="sender", help="sender address (improves estimate_gas)")
    d.add_argument("--gas-limit", type=int, help="skip estimate_gas and use this")
    d.add_argument("--deadline", required=True, help="e.g. 90s, 15m, 2h or ISO time with zone")
    g_ = d.add_mutually_exclusive_group()
    g_.add_argument("--target-gwei", type=float, help="favourable when baseFee+tip <= this")
    g_.add_argument("--percentile", type=float, default=25, help="else: favourable when <= this percentile of recorded history (default 25)")
    d.add_argument("--tip-gwei", type=float, help="priority fee to use (default: network suggestion from get_gas_price)")
    d.add_argument("--interval", type=int, default=15, help="poll interval seconds (>= 2)")
    d.add_argument("--safety", type=int, default=5, help="seconds kept before the deadline for submission (default 5)")
    d.add_argument("--raw-tx", help="file with a transaction you signed yourself (only used with --broadcast)")
    d.add_argument("--broadcast", action="store_true", help="really send --raw-tx via Chain MCP send_raw_transaction (default: dry run)")
    r = sub.add_parser("report", help="actual gas paid vs price at request time")
    r.add_argument("--tx", required=True)
    rq = r.add_mutually_exclusive_group(required=True)
    rq.add_argument("--request-block", type=int)
    rq.add_argument("--request-gwei", type=float)
    a = ap.parse_args(argv)
    try:
        for name in ("samples", "interval", "points", "show"):
            if getattr(a, name, 1) is not None and getattr(a, name, 1) < 1:
                raise ValueError(f"--{name} must be >= 1")
        if a.cmd == "watch" and a.interval < 2:
            raise ValueError("--interval must be >= 2")
        if a.cmd == "hold" and a.interval < 2:
            raise ValueError("--interval must be >= 2")
        if a.cmd == "hold" and not 0 < a.percentile <= 100:
            raise ValueError("--percentile must be in (0, 100]")
        if a.cmd == "history" and not (0 < a.hours <= 24 * 30 and 2 <= a.points <= 200):
            raise ValueError("--hours in (0, 720], --points in [2, 200]")
        if a.cmd == "report" and not re.fullmatch(r"0x[0-9a-fA-F]{64}", a.tx):
            raise ValueError("--tx must be a 32-byte hash")
        g = Gas(PharosMCP(a.endpoint), a.history_file or None)
        return {"watch": cmd_watch, "history": cmd_history, "hold": cmd_hold,
                "report": lambda gg, aa: report(gg, aa.tx, aa.request_block, aa.request_gwei)}[a.cmd](g, a)
    except (ValueError, OSError) as e:
        ap.error(str(e))
    except MCPError as e:
        print(f"error: Chain MCP: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
