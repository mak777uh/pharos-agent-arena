---
name: pharos-trend-watcher
description: >
  A Skill with memory for Pharos mainnet (chainId 1672): stores on-chain observations (WPROS/USDC pool price,
  Chainlink PROS/USD, Morpho market utilization or liquidity, an ERC-20 balance) in SQLite across runs and acts
  only when a trend is confirmed over several observations — e.g. "price down 3 checks in a row" or "utilization
  above 90 % for 3 checks" — never on a single noisy reading. Use for "tell me / act when X keeps falling",
  "only if it stays above Y". Restart-safe (no lost or duplicated history); actions are alerts or UNSIGNED
  transactions. Never signs or broadcasts.
version: 0.1.0
requires:
  anyBins:
  - python3
---

# Pharos Trend Watcher

Same layout as the official [`pharos-skill-engine`](https://github.com/PharosNetwork/pharos-skill-engine).

## Prerequisites

1. Python 3.10+ (sqlite3 is in the standard library), `pip install -r requirements.txt`.
2. No private key. The optional action only prepares an unsigned transaction.

## Capability Index

| Need | Command | Details |
|---|---|---|
| One observation now (cron / scheduler) | `python3 scripts/trend.py observe --config <cfg>` | → `references/trend.md` |
| Observe in a loop | `... watch --config <cfg> --interval 60 --count 10` | |
| Seed history from archive state | `... backfill --config <cfg> --from-block A --to-block B --every 600` | idempotent |
| Show stored history + trigger moments | `... history --config <cfg> [--last N] [--compare-single 0.5]` | |
| List recorded triggers (JSON lines) | `... events --config <cfg>` | |

All commands take `--db` (default `trend_state.db`) and `--alerts` (default `alerts.jsonl`).

## Agent Guidelines

1. Pick or write a config: `series`, `source` (type + params, `recheck_pct`), `rule` (`consecutive` with
   `moves` ≥ 2 / `sustained` with `observations` ≥ 3), optional `action`.
2. Schedule `observe` (or run `watch`). Keep the same `--db` between runs — that file is the memory.
3. When a run prints `TREND CONFIRMED [LIVE]`, report the trigger block/time, the observations that formed the
   trend and the action result. Historical triggers (found by `backfill`) are informational only.
4. To explain a decision, run `history` and show the rows with their streak counter.

## Security Reminders

- Read-only on chain; an action never signs or sends. `https://` endpoints only; inputs and configs validated.
