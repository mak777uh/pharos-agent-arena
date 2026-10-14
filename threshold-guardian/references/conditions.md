# Conditions reference

All conditions of one run are read at the **same block** (the head block when the run starts), except
`gas_price_gwei` (the MCP `get_gas_price` tool has no block parameter). Archive reads (`eth_call` with an old
block number) are used for look-backs.

| `type` | Value | Params | Chain MCP tools / contract calls |
|---|---|---|---|
| `pool_price_change_pct` | % change of a Uniswap-V3-style pool spot price vs `lookback_blocks` ago (absolute; `signed: true` keeps the sign) | `pool`, `lookback_blocks` (1..2,000,000; default 3600 ≈ 1 h) | `eth_call` `slot0()` at head and at head − lookback |
| `pool_oracle_deviation_pct` | \|pool − Chainlink\| / Chainlink × 100 | `pool`, `feed` | `eth_call` `slot0()`, `latestRoundData()`, `decimals()` |
| `oracle_age_seconds` | head block timestamp − feed `updatedAt` | `feed` | `get_block`, `eth_call` `latestRoundData()` |
| `gas_price_gwei` | network gas price | – | `get_gas_price` |
| `morpho_liquidity` | Morpho Blue market supply − borrow, in loan-token units | `market_id` (bytes32) | `eth_call` `market(bytes32)`, `idToMarketParams(bytes32)` |
| `morpho_utilization_pct` | borrow / supply × 100 | `market_id` | same |
| `erc20_balance` | `balanceOf(owner)` in token units | `token`, `owner` | `eth_call` `balanceOf`, `decimals`, `symbol` |
| `native_balance` | PROS balance | `owner` | `get_balance` |

Operators: `lt`, `lte`, `gt`, `gte`, `eq`. Thresholds are decimal strings (no float rounding).

Defaults used in the sample config (all verified on chain on 2026-10-14):
WPROS/USDC pool `0x912c9ade24d44d8922f0866d8dcb079f1363f647` (token0 WPROS, token1 USDC, fee 100),
Chainlink PROS/USD `0x371ed36aee5bffbed1a2aa9b7ded7f4eb30f5466`, Morpho Blue `0x18573fa18fd17ddfd790b4a5b5b2977aad3b4efb`,
market `0xb3b8a31c…49e1ba` (collateral WPROS, loan USDC).

## Decision

`mode: "all"` — every enabled condition must PASS; `"any"` — at least one; integer `k` — at least k.
A condition that raised an error is FAIL (and in `all` mode the run HOLDs).

## Action

`erc20_transfer` (`from`, `to`, `token` symbol or address, `amount`, optional `reference`) is prepared with the
same pre-checks as a payment: chain id, token code/decimals, blacklist, balance, `estimate_gas`, PROS for gas,
pending nonce. If any pre-check fails the decision becomes HOLD with the reason. `none` = decision only.

## Overrides

`--set <id>.threshold=<decimal>`, `--set <id>.op=<op>`, `--set <id>.enabled=false`,
`--set <id>.<param>=<value>` (e.g. `price_move.lookback_blocks=600`). Unknown ids or bad values → exit 2.

## Log

Every run appends one JSON line to `--log` (default `guardian_log.jsonl`): block, time, each condition
(value, threshold, op, passed, source), decision and the prepared action (unsigned) or the hold reason.
