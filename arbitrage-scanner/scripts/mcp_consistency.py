#!/usr/bin/env python3
"""Evidence for the Limits section: read a pool's slot0 at one archive block N times through the hosted Chain MCP
and once through the official Pharos RPC, and print every answer.

    python3 scripts/mcp_consistency.py --pool 0x912c9ade24d44d8922f0866d8dcb079f1363f647 --block 20262739 --n 6
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pay_core import Chain  # noqa: E402
from skill_chain import network  # noqa: E402
from arb import RpcSrc  # noqa: E402


def decode(raw: bytes):
    sp = int.from_bytes(raw[:32], "big")
    tick = int.from_bytes(raw[32:64], "big", signed=True)
    return tick, (sp / 2 ** 96) ** 2 * 1e12


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pool", required=True)
    p.add_argument("--block", type=int, required=True)
    p.add_argument("--n", type=int, default=6)
    a = p.parse_args()
    c = Chain()
    rpc = RpcSrc(network("mainnet")["rpcUrl"])
    t, px = decode(rpc.call(a.pool, "0x3850c7bd", a.block))
    print(f"official RPC  slot0 @ {a.block}: tick {t}  price {px:.6f} USDC/WPROS")
    bad = 0
    for i in range(a.n):
        r = c.m.call_tool("eth_call", {"transaction": {"to": a.pool, "data": "0x3850c7bd"}, "block": a.block})
        r = r["result"] if isinstance(r, dict) else r
        tm, pm = decode(bytes.fromhex(r[2:]))
        ok = tm == t
        bad += not ok
        print(f"Chain MCP #{i + 1}  slot0 @ {a.block}: tick {tm}  price {pm:.6f}  {'matches' if ok else 'DIFFERENT'}")
    print(f"{bad}/{a.n} Chain MCP answers differed from the official RPC for the same block")


if __name__ == "__main__":
    main()
