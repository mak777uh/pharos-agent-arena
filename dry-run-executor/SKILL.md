---
name: pharos-dry-run-executor
description: >
  Simulates a Pharos mainnet (chainId 1672) transaction before it is signed: predicts success or revert (with the
  decoded revert reason), the gas and PROS/USD cost, and the balance changes it would cause, then blocks it
  automatically if the simulation fails. Use before any transfer, swap, wrap/unwrap or raw contract call an agent
  or user is about to sign ("will this go through?", "what will this cost?", "what will I get?"). On GO it
  prepares an UNSIGNED transaction; it never signs or broadcasts.
version: 0.1.0
requires:
  anyBins:
  - python3
---

# Pharos Dry-Run Executor

Layout of the official [`pharos-skill-engine`](https://github.com/PharosNetwork/pharos-skill-engine). Run it before
the engine's `cast send`: a BLOCKED result means the `cast send` must not be run.

## Prerequisites

1. Python 3.10+, `pip install -r requirements.txt`.
2. No private key. `--from` is a public address; simulation works for any address.

## Network Configuration

Simulation (`eth_call`, `estimate_gas`), balances, nonce and fees come from the hosted Pharos Chain MCP
`https://mcp.pharos.xyz/mcp`. State prediction uses `debug_traceCall` on the `rpcUrl` of the `mainnet` entry of
`assets/networks.json` (`https://rpc.pharos.xyz`), because the Chain MCP has no trace tool.

## Capability Index

| Need | Command | Details |
|---|---|---|
| Dry-run a token / PROS transfer | `python3 scripts/dryrun.py transfer --from A --to B --token USDC --amount 100` | → `references/dry-run.md` |
| Dry-run a WPROS/USDC swap | `... swap --from A --sell USDC --amount 10 [--min-out N]` | router `0xf38d…d875`, pool fee 0.01 % |
| Dry-run wrap / unwrap | `... wrap --from A --amount 1` / `... unwrap --from A --amount 1` | WPROS `deposit` / `withdraw` |
| Dry-run any call | `... raw --from A --to C --data 0x… [--value-wei N]` | |
| JSON | `--json` (global, before the subcommand) | exit 0 GO · 4 BLOCKED · 2 error |

## Agent Guidelines

1. Always dry-run before asking the user to sign. Show: result (SUCCESS / REVERT + reason), expected and
   worst-case fee, the event list and the before → after balance table.
2. **BLOCKED** (exit 4) — do not offer the transaction for signing. Explain the reason (revert reason, or not
   enough PROS for value + max fee) and what would fix it.
3. **GO** (exit 0) — hand over the prepared unsigned transaction; the user signs. State is predicted at the head
   block; other transactions landing first can change the result (e.g. swap output).

## Security Reminders

- Never signs, never broadcasts, never asks for a key. `https://` endpoints only; inputs are validated.
