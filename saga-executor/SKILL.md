---
name: pharos-saga-executor
description: >
  Runs a multi-step dependent DeFi workflow on Pharos mainnet state (chainId 1672) as a saga:
  approve USDC -> swap USDC->WPROS on the WPROS/USDC pool -> approve WPROS -> deposit into the Vishwa WPROS
  ERC-4626 vault. Each step is sent, its receipt checked and its post-condition verified before the next step
  starts; transient failures (dropped tx, base-fee spike, slippage from a competing swap) are retried by a defined
  policy, permanent failures trigger compensation (cancel the abandoned nonce, revoke approvals, swap back to USDC).
  Every transition is written to a JSONL state log. Execution happens on a local anvil fork of mainnet; for
  mainnet the skill only prepares UNSIGNED transactions and verifies receipts of txs the user signed.
  Use when the user asks to "swap and deposit", "zap USDC into the WPROS vault", "run these steps safely with rollback".
version: 0.1.0
requires:
  anyBins:
  - python3
  - anvil
---

# Pharos Saga Executor

Layout of the official [`pharos-skill-engine`](https://github.com/PharosNetwork/pharos-skill-engine)
(SKILL.md + references/ + assets/networks.json, Foundry as the chain tool). The engine itself executes with
`cast send --private-key`; this skill never holds a key — it hands the user unsigned transactions and the
`cast send … --private-key $PRIVATE_KEY` line to run themselves.

## Prerequisites

1. Python 3.10+, `pip install -r requirements.txt`.
2. Foundry (`anvil`) on PATH or `--anvil /path/to/anvil` — the saga runs on a local fork of Pharos mainnet.
3. No private key. `sender` in `assets/saga.json` is a public address (impersonated on the fork only).

## Capability Index

| Need | Command | Details |
|---|---|---|
| Run the saga (fork) + unsigned mainnet plan | `python3 scripts/saga.py run [--amount 25] [--sender 0x…]` | → `references/saga.md` |
| Force a failure to see retry / compensation | `--inject drop@swap · spike@approve_usdc · frontrun@swap · revert@deposit · spike-persist@deposit` | → `references/saga.md#fault-injection` |
| Only simulate + prepare compensation | `--no-execute-compensation` | |
| Which step is the saga in? | `python3 scripts/saga.py status --log runs/<id>.jsonl [--history]` | |
| Check a mainnet receipt before the next step | `python3 scripts/saga.py verify-receipt --plan <json> --step <name> --tx 0x…` | Chain MCP |

Exit codes: 0 completed · 5 failed and compensated · 4 failed, compensation incomplete · 2 bad input.

## Agent Guidelines

1. Run `run` first. Show the user the state log as it goes, then the balance table and the outcome.
2. Never describe fork transactions as mainnet transactions: fork hashes are not on pharosscan. Only the
   preflight reads, the step-1 simulation and `verify-receipt` touch mainnet (read-only, via Chain MCP).
3. On a completed run, give the user the unsigned plan step by step. After they sign and send step N, run
   `verify-receipt` for step N; prepare step N+1 only when it says VERIFIED. If the price moved, re-run the saga.
4. On failure, say which step failed, whether it was classified transient (retried) or permanent, and what the
   compensation did. If the result is `NEEDS_MANUAL_ACTION`, list the remaining prepared compensation txs.

## Security Reminders

- Exact-amount approvals only (no infinite approvals); the approve for step 3 uses the amount the swap
  **receipt** delivered, not the quote.
- Swap `amountOutMinimum` = fresh quote − `slippage_bps`; a slippage revert is retried with a new quote at most
  `max_attempts` times; anything else that reverts is permanent and is never resent blindly.
- `https://` endpoints only; nothing is signed, nothing is broadcast to mainnet.
