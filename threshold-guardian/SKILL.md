---
name: pharos-threshold-guardian
description: >
  Multi-condition decision gate for Pharos mainnet (chainId 1672). Use when the user wants an action
  (rebalance, transfer, top-up) to happen ONLY if several on-chain conditions hold at once — e.g. "only
  rebalance if the price moved less than 3%, gas is below 20 gwei and the Morpho market still has > 50k USDC
  liquidity". Reads every condition live through the Pharos Chain MCP, logs a decision trace, and on FIRE
  prepares the action as an UNSIGNED transaction for the user to sign. Never signs or broadcasts.
version: 0.1.0
requires:
  anyBins:
  - python3
---

# Pharos Threshold Guardian

Companion skill to the official [`pharos-skill-engine`](https://github.com/PharosNetwork/pharos-skill-engine)
(same layout: `SKILL.md` + `references/` + `assets/networks.json`). The engine executes; this skill decides
**whether** an action should run at all.

## Prerequisites

1. Python 3.10+ and `pip install -r requirements.txt` (requests, pycryptodome, eth-abi).
2. No private key. This skill is read-only + prepare-only. Signing stays with the user (wallet or
   `cast send ... --private-key $PRIVATE_KEY` from the official engine — the user runs it, not the agent).

## Network Configuration

`assets/networks.json` is the official engine's file. Chain data comes from the hosted Pharos Chain MCP
`https://mcp.pharos.xyz/mcp` (Pacific mainnet, chainId 1672); the agent checks the chain id before preparing.

## Capability Index

| User need | Command | Details |
|---|---|---|
| Decide FIRE/HOLD for a configured action | `python3 scripts/guardian.py --config <file>` | → `references/conditions.md` |
| Change a threshold / operator / param for one run | `--set <id>.threshold=<v>` / `--set <id>.op=gt` / `--set <id>.<param>=<v>` | → `references/conditions.md#overrides` |
| Machine-readable decision | `--json` | exit code 0 = FIRE, 3 = HOLD, 2 = error |

## Agent Guidelines

1. Build or pick a config (see `assets/guardian.rebalance.json`): `conditions[]` (≥ 3), `mode`
   (`all` | `any` | integer k), optional `action` (`erc20_transfer` or `none`).
2. Run the guardian. Show the user the full decision trace: every condition's value, operator, threshold,
   PASS/FAIL and source (contract + function + block). Do not summarise away failing conditions.
3. If **HOLD** (exit 3): tell the user which condition(s) failed; nothing was prepared. Do not retry with
   looser thresholds unless the user asks.
4. If **FIRE** (exit 0): show the prepared unsigned transaction and its pre-checks. Tell the user the target
   network is **Pharos mainnet** and that they must sign it themselves. Never sign, never broadcast.
5. A condition that cannot be read (RPC error, bad address) is FAIL, never PASS.

## Security Reminders

- No private keys are read, stored or requested. Output contains only public addresses and unsigned data.
- Only `https://` endpoints are accepted. All addresses are validated (format + EIP-55 checksum when mixed case).
