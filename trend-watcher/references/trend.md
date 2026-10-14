# Trend Watcher reference

## Config

```json
{
  "series": "wpros-usdc-pool-price",
  "source": {"type": "pool_price", "params": {"pool": "0x912c…f647"}, "recheck_pct": "1"},
  "rule": {"id": "down-3-checks", "type": "consecutive", "direction": "down", "moves": 3,
           "min_step_pct": "0.02", "max_step_pct": "2", "min_total_pct": "0.15", "max_gap_blocks": 1200},
  "action": {"type": "alert"}
}
```

Sources (all through Chain MCP, read **at the observation's block** — archive `eth_call` for backfill):
`pool_price` (`slot0`, token0 in token1), `oracle_price` (Chainlink `latestRoundData`), `morpho_utilization_pct`,
`morpho_liquidity` (Morpho Blue `market(id)`), `erc20_balance` (`balanceOf`). Block timestamp from `get_block`.

## Rules

* **consecutive** — `moves` (≥ 2 → ≥ 3 observations) consecutive moves in `direction`; each move must be at least
  `min_step_pct` and at most `max_step_pct` (a bigger one-step jump is treated as an outlier and resets the streak);
  the streak's total change must be ≥ `min_total_pct`.
* **sustained** — `value op threshold` for `observations` (≥ 3) consecutive observations.
* Both: a gap of more than `max_gap_blocks` between two observations resets the streak (stale data never
  completes a trend).

The rule is a pure function of the stored, block-ordered history (`evaluate()`), so the result is the same after a
restart, a re-run or a backfill that fills older blocks.

## Triggers and actions

A trigger is the observation at which the streak **first** reaches the required length; a longer streak does not
re-trigger until it breaks. Triggers are stored with primary key (series, rule, trigger block) → recorded exactly
once, whatever the number of runs. A trigger at the live head runs the action (`alert`: one JSON line in
`--alerts`; `erc20_transfer`: an unsigned EIP-1559 transfer prepared with full pre-checks). Triggers found in
backfilled history are stored as `historical` and do not act.

## Storage and restarts

SQLite (WAL), table `observations` with primary key (series, block): `INSERT OR IGNORE` makes repeated
observations of the same block no-ops; every insert is its own transaction, so a killed process loses at most the
observation in flight and the next run simply continues (samples/04: SIGKILL mid-backfill, 74 rows = 74 distinct
blocks afterwards).

## Source sanity check (`recheck_pct`, default 2)

A reading that differs by more than `recheck_pct` from the previous stored observation is read again (and a third
time if the two disagree); the value two reads agree on is stored and the rejected answer is noted in the row. In the stored
samples the hosted endpoint answered 5 archive `slot0` reads with an inconsistent value (0.644557 or 0.384643 instead
of ~0.58; 1 in samples/01, 4 in samples/07 — `cast call … -b <block>` against https://rpc.pharos.xyz gives the normal
value for those blocks); each was rejected on re-read, while the real −1.42 % move at block 20,228,606 was
confirmed by its re-read.
