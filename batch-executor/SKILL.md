---
name: pharos-batch-executor
description: >
  Bundles several independent actions on Pharos mainnet (chainId 1672) — PROS payouts, permissionless keeper calls
  such as Morpho Blue accrueInterest, other msg.sender-independent contract calls — into ONE prepared Multicall3
  aggregate3Value transaction. Simulates each action alone and inside the batch through the Pharos Chain MCP,
  reports which action fails and what happens to the rest, and the gas / transaction saving vs one tx per action.
  Use when the user wants to "send these 5 payments at once", "batch these calls", "do it in one transaction".
  Prepare-only: never signs or broadcasts.
version: 0.1.0
requires:
  anyBins:
  - python3
---

# Pharos Batch Executor

Layout of the official [`pharos-skill-engine`](https://github.com/PharosNetwork/pharos-skill-engine). For
**ERC-20 airdrops** use the engine's own Batch Transfer capability (it deploys a distributor contract —
`assets/airdrop` in the engine); this skill refuses ERC-20 transfers through Multicall3 on purpose.

## Prerequisites

1. Python 3.10+, `pip install -r requirements.txt`.
2. No private key. The `sender` is a public address; the user signs the prepared transaction.

## Capability Index

| Need | Command | Details |
|---|---|---|
| Batch ≥ 3 actions into one tx | `python3 scripts/batch.py --actions <file.json>` | → `references/batching.md` |
| Stop if any action fails | `--on-failure abort` (default `drop`) | → `references/batching.md#partial-failure` |
| JSON report | `--json` | exit 0 all · 6 partial (failed dropped) · 4 nothing prepared · 2 invalid |

## Agent Guidelines

1. Build the actions file: `send_pros`, `morpho_accrue` (market id) or `call` (only for calls whose effect does
   not depend on `msg.sender`, because inside the batch `msg.sender` is Multicall3).
2. Show the per-action table (alone vs in batch), then the outcome. On partial failure name the failed action and
   its reason, and say clearly that the remaining actions were re-simulated and bundled without it (drop) or that
   nothing was prepared (abort).
3. Report the saving honestly: transactions/signatures always drop to 1, but gas can go **up** for plain PROS
   transfers (value calls through a contract cost more than a bare transfer). Say so when the report says so.
4. Hand the unsigned transaction to the user to sign. Never sign or broadcast.

## Security Reminders

- The prepared batch uses `allowFailure=false` for every call: on chain it is all-or-nothing, so PROS can never be
  left behind in Multicall3 by a failing sub-call.
- ERC-20 `transfer`/`approve`/`transferFrom` through Multicall3 are refused (would need an approval to a contract
  anyone can call). `https://` endpoints only; all inputs validated.
