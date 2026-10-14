---
name: pharos-swap-timing
description: >
  Decides whether NOW is a good time to swap on the Pharos mainnet WPROS/USDC pool, from live chain data.
  Use when the user asks things like "is now a good time to swap 500 USDC to WPROS?", "should I sell 20k PROS
  now?", "what would I get for $250 of PROS?". The skill parses the request, pulls live pool / oracle / gas /
  balance data through the Pharos Chain MCP in the same flow, and returns GO / WAIT / NO-GO with an exact
  in-range quote and the reason for every rule. Decision only: it never builds, signs or sends a swap.
version: 0.1.0
requires:
  anyBins:
  - python3
---

# Pharos Swap Timing (Skill + Chain MCP flow)

Layout follows the official [`pharos-skill-engine`](https://github.com/PharosNetwork/pharos-skill-engine)
(`SKILL.md` + `references/` + `assets/networks.json`). The skill is the decision layer; the hosted
**Pharos Chain MCP** (`https://mcp.pharos.xyz/mcp`, read-only, no key) is the data layer.

## Prerequisites

1. Python 3.10+, `pip install -r requirements.txt`.
2. No private key, no wallet connection. `--wallet` takes a public address only.

## Capability Index

| User need | Command | Details |
|---|---|---|
| "Is now a good time to swap X A to B?" | `python3 scripts/flow.py "<request>" [--wallet 0x…]` | → `references/flow.md` |
| Same, machine-readable | add `--json` | exit 0 GO · 3 WAIT · 4 NO-GO · 2 error |
| Stricter / looser rules | `--rules assets/rules.strict.json` | → `references/flow.md#rules` |
| Prove the quote math on real swaps | `python3 scripts/backtest.py --blocks 600` | → `references/flow.md#backtest` |

## Agent Guidelines

1. Pass the user's sentence unchanged as the request. If the skill answers with an error asking for the amount
   or token, ask the user that question — do not guess.
2. Show the verdict first, then the quote (expected output, price impact, Chainlink comparison) and every rule
   line. Mention the block number the data is from.
3. GO = all rules pass. WAIT = only soft rules failed (oracle deviation, oracle age, gas): suggest re-checking
   later. NO-GO = a hard rule failed (price impact, tick crossing, wallet balance): suggest a smaller amount.
4. Never present the quote as guaranteed: it is the exact in-range result at the read block; other swaps
   before the user's own will move it.
5. This skill does not swap. If the user wants to execute, hand over to a swap tool / the official Skill Engine
   and let the user sign.

## Security Reminders

- Read-only. Only `https://` endpoints. Addresses are validated (format and EIP-55 checksum).
