---
name: pharos-arbitrage-scanner
description: >
  Scans WPROS/USDC prices across every Uniswap-V3-style pool the Pharos mainnet factory knows (fee tiers 0.01 %,
  0.05 %, 0.3 %, 1 %) plus the Chainlink PROS/USD feed as a reference, all read at the same block through the
  Pharos Chain MCP. For each pool pair it computes the gross spread, the swap fees, the slippage (exact V3 tick
  traversal) and the gas, finds the best size, and prepares ONE atomic unsigned router.exactInput round-trip only
  when the net profit clears a configurable threshold; otherwise it logs why it skipped. Every scan is logged.
  Simulated execution on a local anvil fork shows the realized result. Use when the user asks "is there an
  arbitrage between the PROS pools?", "scan for arb", "is the spread worth it after fees?". Prepare-only.
version: 0.1.0
requires:
  anyBins:
  - python3
---

# Pharos Arbitrage Scanner

Layout of the official [`pharos-skill-engine`](https://github.com/PharosNetwork/pharos-skill-engine)
(SKILL.md + references/ + assets/networks.json). The engine executes with `cast send --private-key`; this skill
never holds a key — it prints the unsigned tx and the `cast send … --private-key $PRIVATE_KEY` line for the user.

## Prerequisites

1. Python 3.10+, `pip install -r requirements.txt`.
2. Optional: Foundry `anvil` for `--simulate` and `fork-scenario` (simulated execution on a local fork).
3. No private key. `wallet` in `assets/arb.json` is a public address (caps the size at its USDC balance).

## Capability Index

| Need | Command | Details |
|---|---|---|
| Scan now (mainnet, Chain MCP) | `python3 scripts/arb.py scan [--count N --interval S]` | → `references/scanner.md` |
| Scan a past block | `scan --block <n>` · `backscan --from A --to B --step K` | archive `eth_call` |
| Change the threshold | `--threshold-usd 2 --threshold-bps 10` (default `assets/arb.json`) | |
| Simulate the prepared tx | `scan --simulate` (anvil fork at the scan block) | |
| See a profitable case end to end, safely | `fork-scenario --whale-usdc 1500 --pool-fee 3000` | fork only |
| Machine output | `--json`, `--json-out file` · every scan is appended to `runs/scans.jsonl` | |

## Agent Guidelines

1. Run `scan`. Report the sources (each pool's mid price and its deviation from Chainlink), the best route, and
   the decision with its reason, using the numbers in the scan table (gross, fees, slippage, gas, net).
2. A SKIP is the normal answer: say why (spread below the fee floor, or slippage/gas eat it). Do not suggest
   lowering the threshold below zero.
3. On PREPARE, hand the user the unsigned approve (if needed) + exactInput and say the tx is atomic: it reverts
   unless it returns at least `amountOutMinimum` (expected out minus the buffer, never below amountIn + gas), so
   the downside is the gas of a reverted tx. Opportunities close fast — re-scan right before signing.
4. Fork-scenario results are not mainnet opportunities; say so.

## Security Reminders

- Exact-amount approve only; the prepared swap pays out to the wallet itself.
- Pool reads that deviate from Chainlink by more than `max_oracle_deviation_pct` are re-read once and excluded
  if still off (the archive RPC has been seen returning an inconsistent `slot0`).
- `https://` endpoints only; nothing is signed or broadcast.
