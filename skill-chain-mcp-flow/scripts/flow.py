#!/usr/bin/env python3
"""Skill + Chain MCP flow: "is now a good time to swap X on the WPROS/USDC pool?"

One user request in, one verdict out:

    python3 scripts/flow.py "Is now a good time to swap 500 USDC to WPROS?"
    python3 scripts/flow.py "swap 20000 WPROS for USDC" --wallet 0x...      # also checks the wallet balance
    python3 scripts/flow.py "..." --json

  1. SKILL  parses the request (amount, tokens, direction) and builds a data plan.
  2. CHAIN MCP (https://mcp.pharos.xyz/mcp) supplies live data: chain id, head block, pool slot0/liquidity/
     tickBitmap, Chainlink PROS/USD, gas price, wallet balance.
  3. SKILL  decides from that data with the rules in assets/rules.json: expected output and price impact
     (Uniswap-V3 in-range math, tick-crossing checked against tickBitmap), pool-vs-oracle deviation,
     oracle freshness, gas -> GO / WAIT / NO-GO with the reason for each rule.

Every Chain MCP call is recorded (tool, arguments, latency, result digest) and printed as the flow trace.
The skill only answers the question; it does not build, sign or send a swap.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from pay_core import Chain, check_address, h2i, CHAIN_ID  # noqa: E402
from skill_chain import Reader, WPROS_USDC_POOL, CHAINLINK_PROS_USD, addr_link, calldata, utc  # noqa: E402

RULES_FILE = os.path.join(HERE, "..", "assets", "rules.json")
Q96 = Decimal(2) ** 96
ALIASES = {"usdc": "USDC", "usd": "USDC", "$": "USDC", "wpros": "WPROS", "pros": "WPROS"}


# ================================================================ 1. SKILL: understand the request
def parse_request(text: str) -> dict:
    """Extract amount, input token and output token from a short English request."""
    t = text.strip()
    if not t or len(t) > 300:
        raise ValueError("request must be 1-300 characters")
    low = t.lower().replace(",", "")
    m = re.search(r"(\$)?\s*(\d+(?:\.\d+)?)\s*(k)?\s*(usdc|usd|wpros|pros)?\b", low)
    if not m:
        raise ValueError("could not find an amount in the request (e.g. 'swap 500 USDC to WPROS')")
    amount = Decimal(m.group(2)) * (1000 if m.group(3) else 1)
    tok_in = ALIASES.get(m.group(4) or ("$" if m.group(1) else ""), None)
    rest = low[m.end():]
    m2 = re.search(r"\b(?:to|for|into|->)\s+(usdc|usd|wpros|pros)\b", rest)
    tok_out = ALIASES.get(m2.group(1)) if m2 else None
    if tok_in is None and tok_out:
        tok_in = "USDC" if tok_out == "WPROS" else "WPROS"
    if tok_in is None:
        raise ValueError("which token do you want to sell? say e.g. '500 USDC' or '1000 WPROS'")
    if tok_out is None:
        tok_out = "WPROS" if tok_in == "USDC" else "USDC"
    if tok_in == tok_out:
        raise ValueError(f"input and output token are both {tok_in}")
    if amount <= 0:
        raise ValueError("amount must be positive")
    return {"amount": amount, "tokenIn": tok_in, "tokenOut": tok_out, "text": t}


# ================================================================ 2. CHAIN MCP: live data (recorded)
FN_NAMES = {calldata(sig)[2:10]: sig for sig in [
    "token0()", "token1()", "decimals()", "symbol()", "slot0()", "liquidity()", "fee()", "tickSpacing()",
    "latestRoundData()", "balanceOf(address)", "tickBitmap(int16)"]}


class Recorder:
    """Wraps PharosMCP.call_tool so every Chain MCP call in the flow is logged."""

    def __init__(self, mcp):
        self.mcp, self.calls, self._orig = mcp, [], mcp.call_tool
        mcp.call_tool = self

    def __call__(self, name, arguments=None):
        t0 = time.time()
        out = self._orig(name, arguments)
        a = dict(arguments or {})
        if "transaction" in a:
            tx = a["transaction"]
            sel = (tx.get("data") or "")[2:10]
            a = {"to": tx.get("to"), "fn": FN_NAMES.get(sel, "0x" + sel), "block": a.get("block")}
        digest = json.dumps(out, default=str)
        self.calls.append({"tool": name, "args": a, "ms": round((time.time() - t0) * 1000),
                           "result": digest[:90] + ("…" if len(digest) > 90 else "")})
        return out


def fetch(chain: Chain, req: dict, wallet: str | None, pool: str, feed: str) -> dict:
    r = Reader(chain)
    info = chain.m.call_tool("get_chain_info")
    if int(info["chainId"]) != CHAIN_ID:
        raise RuntimeError(f"Chain MCP is on chainId {info['chainId']}, expected {CHAIN_ID}")
    head = chain.block_number()
    ts = h2i(chain.block(head)["timestamp"])
    t0, t1, d0, d1 = r.pool_meta(pool)
    s0, s1 = r.symbol(t0), r.symbol(t1)
    if {s0, s1} != {req["tokenIn"], req["tokenOut"]}:
        raise RuntimeError(f"pool {pool} is {s0}/{s1}, request is {req['tokenIn']}->{req['tokenOut']}")
    price, meta = r.pool_price(pool, head)
    fee = int.from_bytes(r.call(pool, "fee()", block=head)[:32], "big")
    spacing = int.from_bytes(r.call(pool, "tickSpacing()", block=head)[:32], "big", signed=True)
    oracle, ometa = r.oracle(feed, head)
    gas, _ = r.gas_price_gwei()
    data = {"head": head, "timestamp": ts, "pool": pool, "token0": s0, "token1": s1, "dec0": d0, "dec1": d1,
            "addr0": t0, "addr1": t1, "sqrtPriceX96": meta["sqrtPriceX96"], "tick": meta["tick"],
            "liquidity": meta["liquidity"], "fee": fee, "tickSpacing": spacing, "poolPrice": price,
            "oracle": oracle, "oracleUpdatedAt": ometa["updatedAt"], "gasGwei": gas, "reader": r}
    if wallet:
        tok = t0 if req["tokenIn"] == s0 else t1
        bal, _ = r.erc20_balance(tok, wallet, head)
        data["walletBalance"] = bal
    return data


def bitmap_word(r: Reader, pool: str, word_pos: int, block: int) -> int:
    out = r.call(pool, "tickBitmap(int16)", ["int16"], [word_pos], block=block)
    return int.from_bytes(out[:32], "big")


MAX_SCAN_WORDS = 8


def crosses_initialized_tick(r: Reader, pool: str, tick_now: int, tick_new: int, spacing: int, block: int):
    """First initialized tick the swap would cross, or None. Moving down crosses t when tick_new < t <= tick_now,
    moving up when tick_now < t <= tick_new. Reads tickBitmap words through Chain MCP (one eth_call per 256
    compressed ticks). A move wider than MAX_SCAN_WORDS words is reported as 'too-far' (treated as crossing)."""
    lo, hi = sorted((tick_now, tick_new))
    c_lo, c_hi = lo // spacing, hi // spacing
    if (c_hi >> 8) - (c_lo >> 8) + 1 > MAX_SCAN_WORDS:
        return "too-far"
    for wp in range(c_lo >> 8, (c_hi >> 8) + 1):
        word = bitmap_word(r, pool, wp, block)
        if not word:
            continue
        for bit in range(256):
            if word >> bit & 1:
                ct = (wp << 8) + bit
                # ticks in (lo, hi]: moving down crosses tick t when price falls below t (t <= tick_now)
                if c_lo < ct <= c_hi:
                    return ct * spacing
    return None


# ================================================================ 3. SKILL: decide
def quote(d: dict, req: dict) -> dict:
    """In-range Uniswap-V3 swap math on the live slot0/liquidity (exact-input)."""
    L = Decimal(d["liquidity"])
    sp = Decimal(d["sqrtPriceX96"]) / Q96            # sqrt(raw token1 / raw token0)
    fee = Decimal(d["fee"]) / Decimal(1_000_000)
    zero_for_one = req["tokenIn"] == d["token0"]
    dec_in, dec_out = (d["dec0"], d["dec1"]) if zero_for_one else (d["dec1"], d["dec0"])
    a_in = req["amount"] * Decimal(10) ** dec_in * (1 - fee)
    if zero_for_one:   # sell token0 -> price (token1/token0) goes down
        sp_new = L * sp / (L + a_in * sp)
        out = L * (sp - sp_new)
    else:              # sell token1 -> price goes up
        sp_new = sp + a_in / L
        out = L * (1 / sp - 1 / sp_new)
    out_h = out / Decimal(10) ** dec_out
    p_now = sp * sp * Decimal(10) ** (d["dec0"] - d["dec1"])
    p_new = sp_new * sp_new * Decimal(10) ** (d["dec0"] - d["dec1"])
    # execution price in "token1 per token0"
    exec_p = (out_h / req["amount"]) if zero_for_one else (req["amount"] / out_h)
    impact = abs(exec_p - p_now) / p_now * 100
    tick_new = math.floor((sp_new * sp_new).ln() / Decimal("1.0001").ln())
    return {"amountOut": out_h, "execPrice": exec_p, "priceAfter": p_new, "impactPct": impact,
            "tickAfter": tick_new, "zeroForOne": zero_for_one}


def decide(d: dict, req: dict, rules: dict, wallet: str | None) -> dict:
    q = quote(d, req)
    cross = crosses_initialized_tick(d["reader"], d["pool"], d["tick"], q["tickAfter"], d["tickSpacing"], d["head"])
    dev = abs(d["poolPrice"] - d["oracle"]) / d["oracle"] * 100
    age = d["timestamp"] - d["oracleUpdatedAt"]
    checks = []

    def add(rid, ok, severity, text):
        checks.append({"rule": rid, "ok": ok, "severity": severity, "text": text})

    def le(ok):
        return "<=" if ok else ">"

    span = abs(q["tickAfter"] - d["tick"])
    if cross is None:
        rtxt = f"stays inside the current liquidity range (tick {d['tick']} -> {q['tickAfter']}, no initialized tick crossed)"
    elif cross == "too-far":
        rtxt = f"moves the price {span} ticks ({d['tick']} -> {q['tickAfter']}) - far outside the current range, quote unreliable; split the order"
    else:
        rtxt = f"crosses initialized tick {cross} (tick {d['tick']} -> {q['tickAfter']}) - in-range quote unreliable; split the order"
    add("range", cross is None, "nogo", rtxt)
    ok = q["impactPct"] <= Decimal(str(rules["max_price_impact_pct"]))
    add("impact", ok, "nogo", f"price impact {q['impactPct']:.4f}% {le(ok)} {rules['max_price_impact_pct']}%")
    ok = dev <= Decimal(str(rules["max_oracle_deviation_pct"]))
    add("oracle_dev", ok, "wait", f"pool {d['poolPrice']:.6f} vs Chainlink {d['oracle']:.6f}: deviation {dev:.3f}% "
                                  f"{le(ok)} {rules['max_oracle_deviation_pct']}%")
    ok = age <= int(rules["max_oracle_age_s"])
    add("oracle_fresh", ok, "wait", f"Chainlink answer age {age}s {le(ok)} {rules['max_oracle_age_s']}s")
    ok = d["gasGwei"] <= Decimal(str(rules["max_gas_gwei"]))
    add("gas", ok, "wait", f"gas {d['gasGwei']} gwei {le(ok)} {rules['max_gas_gwei']} gwei")
    if wallet:
        ok = d["walletBalance"] >= req["amount"]
        add("balance", ok, "nogo", f"wallet holds {d['walletBalance']:,} {req['tokenIn']} "
                                   f"{'>=' if ok else '<'} {req['amount']:,}")
    if any(not c["ok"] and c["severity"] == "nogo" for c in checks):
        verdict = "NO-GO"
    elif any(not c["ok"] for c in checks):
        verdict = "WAIT"
    else:
        verdict = "GO"
    oracle_out = req["amount"] / d["oracle"] if req["tokenIn"] == "USDC" else req["amount"] * d["oracle"]
    return {"verdict": verdict, "checks": checks, "quote": q, "oracleDeviationPct": dev, "oracleAgeS": age,
            "oracleFairOut": oracle_out}


# ================================================================ orchestration (single request)
def run(text: str, wallet: str | None = None, endpoint: str | None = None, rules_file: str = RULES_FILE,
        pool: str = WPROS_USDC_POOL, feed: str = CHAINLINK_PROS_USD) -> dict:
    with open(rules_file, encoding="utf-8") as f:
        rules = json.load(f)
    steps = [("skill", "parse request")]
    req = parse_request(text)
    if wallet:
        wallet = check_address(wallet, "wallet")
    if req["amount"] > Decimal(str(rules["max_request_amount"])):
        raise ValueError(f"amount above max_request_amount {rules['max_request_amount']} in rules.json")
    chain = Chain(endpoint)
    rec = Recorder(chain.m)
    steps.append(("chain-mcp", "fetch live state"))
    d = fetch(chain, req, wallet, check_address(pool, "pool"), check_address(feed, "feed"))
    steps.append(("skill", "decide"))
    dec = decide(d, req, rules, wallet)   # tickBitmap reads also go through Chain MCP (recorded)
    d.pop("reader")
    return {"request": req, "data": d, "decision": dec, "mcpCalls": rec.calls, "steps": steps,
            "endpoint": chain.m.endpoint, "server": chain.m.server_info}


def render(res: dict) -> str:
    req, d, dec = res["request"], res["data"], res["decision"]
    q = dec["quote"]
    L = [f'Request: "{req["text"]}"',
         f"[1 SKILL] parsed -> sell {req['amount']:,} {req['tokenIn']}, buy {req['tokenOut']} on pool "
         f"{d['token0']}/{d['token1']} {d['pool']}",
         f"[2 CHAIN MCP] {res['endpoint']} ({res['server'].get('name', '?')} {res['server'].get('version', '')}) - "
         f"{len(res['mcpCalls'])} calls, block #{d['head']:,} ({utc(d['timestamp'])})"]
    for i, c in enumerate(res["mcpCalls"], 1):
        a = c["args"]
        what = f"{a['to'][:10]}…{a['to'][-4:]}.{a['fn']} @{a['block']}" if "fn" in a else json.dumps(a)
        L.append(f"   {i:>2}. {c['tool']:<16} {what:<52} {c['ms']:>4} ms -> {c['result'][:48]}")
    L += [f"   live: slot0 sqrtPriceX96={d['sqrtPriceX96']} tick={d['tick']} liquidity={d['liquidity']} fee={d['fee']}",
          f"         pool price {d['poolPrice']:.6f} {d['token1']}/{d['token0']}, Chainlink PROS/USD {d['oracle']:.6f}, "
          f"gas {d['gasGwei']} gwei" + (f", wallet {d['walletBalance']:,} {req['tokenIn']}" if "walletBalance" in d else ""),
          f"[3 SKILL] quote: {req['amount']:,} {req['tokenIn']} -> ~{q['amountOut']:,.6f} {req['tokenOut']} "
          f"(exec {q['execPrice']:.6f}, after {q['priceAfter']:.6f}, impact {q['impactPct']:.4f}%, fee {d['fee']/10000}%)",
          f"          at the Chainlink price it would be {dec['oracleFairOut']:,.6f} {req['tokenOut']}"]
    for c in dec["checks"]:
        tag = "ok" if c["ok"] else ("NO-GO" if c["severity"] == "nogo" else "WAIT")
        L.append(f"   [{tag:>5}] {c['rule']:<12} {c['text']}")
    L.append(f"VERDICT: {dec['verdict']}")
    L.append(f"pool: {addr_link(d['pool'])}")
    L.append("(decision only - no swap transaction is built, signed or sent)")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("request", help='e.g. "Is now a good time to swap 500 USDC to WPROS?"')
    ap.add_argument("--wallet", help="optional: also check that this address holds the input amount")
    ap.add_argument("--rules", default=RULES_FILE)
    ap.add_argument("--endpoint", default=None, help="Chain MCP endpoint (https only)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        res = run(a.request, a.wallet, a.endpoint, a.rules)
    except (ValueError, RuntimeError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        print(render(res))
    return {"GO": 0, "WAIT": 3, "NO-GO": 4}[res["decision"]["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
