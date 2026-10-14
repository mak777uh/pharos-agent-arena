#!/usr/bin/env python3
"""Check the skill's quote math against REAL swaps: for recent Swap events of the pool, read the pool state at
block N-1 through Chain MCP, run the same quote() the flow uses on the real input amount, and compare with the
amount the swap actually paid out.

    python3 scripts/backtest.py --blocks 600 --max 8

Only swaps that are the first swap of the pool in their block and did not cross an initialized tick are compared
(otherwise the N-1 state is not the state the swap saw, or the in-range formula does not apply).
"""
from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from eth_abi import decode as abi_decode  # noqa: E402

from pay_core import Chain, h2i  # noqa: E402
from skill_chain import Reader, WPROS_USDC_POOL, tx_link  # noqa: E402
import flow  # noqa: E402

SWAP_V3 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", type=int, default=600, help="look back this many blocks (<= 5000)")
    ap.add_argument("--max", type=int, default=8)
    a = ap.parse_args(argv)
    if not 1 <= a.blocks <= 5000:
        raise SystemExit("--blocks must be 1..5000")
    c = Chain()
    r = Reader(c)
    head = c.block_number()
    logs, b = [], head - a.blocks + 1
    while b <= head:
        e = min(b + 999, head)
        logs += c.m.get_logs(WPROS_USDC_POOL, [SWAP_V3], b, e)
        b = e + 1
    t0, t1, d0, d1 = r.pool_meta(WPROS_USDC_POOL)
    s0, s1 = r.symbol(t0), r.symbol(t1)
    fee = int.from_bytes(r.call(WPROS_USDC_POOL, "fee()")[:32], "big")
    seen_blocks, rows = set(), []
    for lg in sorted(logs, key=lambda x: (h2i(x["blockNumber"]), h2i(x["logIndex"]))):
        bn = h2i(lg["blockNumber"])
        if bn in seen_blocks:
            continue
        seen_blocks.add(bn)
        a0, a1, sp_after, liq, tick_after = abi_decode(["int256", "int256", "uint160", "uint128", "int24"],
                                                      bytes.fromhex(lg["data"][2:]))
        price, meta = r.pool_price(WPROS_USDC_POOL, bn - 1)
        if meta["liquidity"] != liq:
            continue  # liquidity changed -> a tick was crossed (or LP change); in-range math does not apply
        zfo = a0 > 0
        amt_in = Decimal(a0 if zfo else a1) / Decimal(10) ** (d0 if zfo else d1)
        actual = Decimal(-(a1 if zfo else a0)) / Decimal(10) ** (d1 if zfo else d0)
        d = {"liquidity": meta["liquidity"], "sqrtPriceX96": meta["sqrtPriceX96"], "fee": fee,
             "token0": s0, "token1": s1, "dec0": d0, "dec1": d1}
        req = {"amount": amt_in, "tokenIn": s0 if zfo else s1, "tokenOut": s1 if zfo else s0}
        q = flow.quote(d, req)
        err = (q["amountOut"] - actual) / actual * 100 if actual else Decimal(0)
        rows.append((bn, lg["transactionHash"], req, actual, q["amountOut"], err))
        print(f"block {bn:,}  sell {amt_in:,.6f} {req['tokenIn']:<5} actual out {actual:,.6f} {req['tokenOut']:<5} "
              f"skill quote {q['amountOut']:,.6f}  diff {err:+.6f}%")
        print(f"   {tx_link(lg['transactionHash'])}")
        if len(rows) >= a.max:
            break
    if rows:
        worst = max(abs(x[5]) for x in rows)
        print(f"\n{len(rows)} real swaps compared (of {len(logs)} Swap events in {a.blocks} blocks); "
              f"worst |diff| {worst:.6f}%")
    else:
        print(f"no comparable swaps in the last {a.blocks} blocks ({len(logs)} Swap events)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
