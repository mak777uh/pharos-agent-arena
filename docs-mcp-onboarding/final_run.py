#!/usr/bin/env python3
"""What I built at the end of the Docs-MCP-only onboarding: a read-only Pharos agent check.

    python final_run.py            # human-readable report
    python final_run.py --json     # same, machine-readable

Every constant below was taken from a Docs MCP answer in transcript.json (step number in the comment) — no
other source. The script connects to the hosted Chain MCP and checks each documented fact against the live
server, so the result doubles as verification of the gaps reported in ONBOARDING_LOG.md:

  A. MCP handshake + `get_chain_info` -> chain id must be 1672 (step 4/10)
  B. GET /health (step 3)
  C. latest block, USDC totalSupply()/decimals() via eth_call on the registry address (step 13)
  D. USDC Transfer logs over the last 1,000 blocks = the documented get_logs cap (step 14), and one window of
     1,001 blocks to see whether the cap is really enforced
  E. tools/list input schemas compared with the argument names the Docs MCP gave (step 12)
  F. whether the hosted `send_raw_transaction` is enabled (steps 1/6 contradict each other). Probed with the
     single byte 0x00, which is not a transaction and cannot be decoded as one: nothing can be broadcast.

Read-only: no key, no signing, no real transaction.
"""
from __future__ import annotations

import argparse
import json
import sys

import requests

from pharos_mcp import DEFAULT_ENDPOINT, MCPError, PharosMCP

CHAIN_ID = 1672                                              # step 4 / step 10
USDC = "0xc879c018db60520f4355c26ed1a6d572cdac1815"           # step 13 (Token Registry)
LOG_CAP = 1000                                               # step 14
HEALTH = "https://mcp.pharos.xyz/health"                     # step 3
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DOCUMENTED_ARGS = {                                          # step 12, as the Docs MCP stated them
    "get_block": ["block", "fullTransactions"],
    "get_logs": ["filter"],
    "estimate_gas": ["transaction", "block"],
    "eth_call": ["transaction", "block"],
}


def h2i(x) -> int:
    s = str(x)
    return int(s, 16) if s.startswith("0x") else int(s)


def run() -> dict:
    r: dict = {"checks": []}

    def check(name, ok, detail):
        r["checks"].append({"check": name, "ok": ok, "detail": detail})

    m = PharosMCP(DEFAULT_ENDPOINT)
    r["server"], r["protocol"] = m.server_info, m.protocol_version
    info = m.call_tool("get_chain_info")
    cid = h2i(info.get("chainId", info.get("chain_id", -1))) if isinstance(info, dict) else -1
    check("A chain id = 1672", cid == CHAIN_ID, f"get_chain_info -> {json.dumps(info)[:160]}")

    try:
        h = requests.get(HEALTH, timeout=15)
        check("B /health", h.ok, f"HTTP {h.status_code} {h.text[:120]}")
    except requests.RequestException as e:
        check("B /health", False, str(e))

    head = m.block_number()
    supply = h2i(m.eth_call(USDC, "0x18160ddd", head))
    dec = h2i(m.eth_call(USDC, "0x313ce567", head))
    r["block"] = head
    check("C USDC totalSupply via eth_call", supply > 0,
          f"block {head:,}: totalSupply = {supply / 10 ** dec:,.6f} USDC (decimals {dec})")

    logs = m.get_logs(USDC, [TRANSFER], head - LOG_CAP + 1, head)
    sample = logs[-1] if logs else None
    r["sample_transfer_tx"] = sample.get("transactionHash") if sample else None
    check("D1 get_logs over 1,000 blocks", True,
          f"{len(logs)} USDC Transfer logs in blocks {head - LOG_CAP + 1:,}..{head:,}"
          + (f"; latest tx {r['sample_transfer_tx']}" if sample else ""))
    try:
        over = m.get_logs(USDC, [TRANSFER], head - LOG_CAP, head)
        check("D2 get_logs over 1,001 blocks", True,
              f"NOT rejected: returned {len(over)} logs (documented cap is 1,000 blocks)")
    except MCPError as e:
        check("D2 get_logs over 1,001 blocks", True, f"rejected as documented: {str(e)[:140]}")

    tools = {t["name"]: sorted((t.get("inputSchema") or {}).get("properties", {}))
             for t in m._request("tools/list", {})["tools"]}
    r["tool_count"] = len(tools)
    for name, doc_args in DOCUMENTED_ARGS.items():
        live = tools.get(name, [])
        missing = [a for a in doc_args if a not in live]
        check(f"E schema {name}", not missing,
              f"docs: {doc_args} · live schema: {live}" + (f" · docs args NOT in schema: {missing}" if missing else ""))

    err, text = m.call_tool_raw("send_raw_transaction", {"rawTransaction": "0x00"})
    disabled = any(w in text.lower() for w in ("disabled", "not enabled", "not allowed", "forbidden"))
    r["send_raw_transaction"] = "disabled" if disabled else ("enabled (rejected the invalid 0x00 payload)" if err else "?")
    check("F hosted send_raw_transaction", True, f"'send_raw_transaction' in tools/list: {'send_raw_transaction' in tools}; "
          f"probe with 0x00 -> {text[:200]}")
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Final read-only check built from Docs-MCP answers only.")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        r = run()
    except (MCPError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(r, indent=2))
        return 0
    print(f"Chain MCP {DEFAULT_ENDPOINT} · server {r['server'].get('name')} {r['server'].get('version')} · "
          f"protocol {r['protocol']} · {r['tool_count']} tools · block #{r['block']:,}")
    for c in r["checks"]:
        print(f"[{'OK ' if c['ok'] else 'GAP'}] {c['check']}: {c['detail']}")
    if r.get("sample_transfer_tx"):
        print(f"Proof: https://www.pharosscan.xyz/tx/{r['sample_transfer_tx']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
