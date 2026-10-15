# Scanner reference

## Sources (one scan = one block)

| source | read | role |
|---|---|---|
| WPROS/USDC pools from `factory.getPool(USDC, WPROS, fee)` for fee ∈ {100, 500, 3000, 10000} | `slot0`, `liquidity` (+ `fee`, `tickSpacing`, `token0/1` once), and for quotes `tickBitmap(word)` / `ticks(t)` actually crossed | tradable legs |
| Chainlink PROS/USD `0x371e…5466` | `latestRoundData`, `decimals` | reference: sanity check of each pool's mid price (not tradable) |
| wallet | `USDC.balanceOf(wallet)` | size cap |
| gas | `get_block(block).baseFeePerGas` + `get_gas_price` tip | gas cost |

All `eth_call`s carry the same block number (live: the head read at the start of the scan; `--block`/backscan:
archive). Pools found on 15 Oct 2026: 0.01 % `0x912c…f647`, 0.05 % `0x9e1e…651d`, 0.3 % `0x4146…5623`,
1 % `0xfc5f…5049`.

## Math

* Mid price `P = (sqrtPriceX96 / 2^96)^2 · 10^(18−6)` USDC per WPROS (token0 = WPROS, token1 = USDC in every pool).
* Route A→B: buy WPROS on A with `x` USDC (exact input, token1 in), sell all of it on B (token0 in).
  `scripts/v3math.py` ports Uniswap V3 `SwapMath.computeSwapStep`, `SqrtPriceMath` and `TickBitmap` and walks
  initialized ticks, so liquidity changes are honoured for any size. Checked against the router on a fork:
  identical output for in-range swaps, ≤ 18 wei of WPROS difference after a tick crossing (sqrt-at-tick via
  80-digit Decimal instead of the TickMath table).
* Cost decomposition (USD = USDC):
  * `gross = x · P_B / P_A − x` (what the spread is worth with no fees and no impact)
  * `fees` = fee charged in each V3 step (leg 1 in USDC, leg 2 in WPROS × P_B)
  * `slippage = gross − fees − (out − x)` (price impact of both legs)
  * `gas = gas_units × (baseFee + tip) × PROS/USD` — `gas_units_fallback` 200,000 (a 2-hop exactInput used
    198,569–199,523 gas on the fork; the fork simulation reports the real figure)
  * `net = out − x − gas`, `net bps = net / x`
* Size: grid `sizes_usdc` capped by `max_size_usdc` and the wallet's USDC, then golden-section refinement
  between the grid neighbours of the best point.
* Branch and bound over pairs: fees ≥ fee-tier floor and slippage ≥ 0, so
  `UB = size · (gross bps − floor bps) − gas` bounds a pair's net. Pairs are fully quoted in descending UB order
  until no remaining UB can beat the best net found; the rest are printed with their bound
  ("upper bound, not quoted").

## Decision

`PREPARE` iff `net ≥ threshold_usd` **and** `net bps ≥ threshold_bps` (defaults $1.00 and 5 bps; override with
`--threshold-usd/--threshold-bps`). Otherwise `SKIP` with every failed condition named: spread ≤ fee floor,
slippage + gas eat the spread at every size, net below the USD threshold, net below the bps threshold.

PREPARE output (unsigned, chainId 1672): `USDC.approve(router, amountIn)` if the allowance is short, then
`router.exactInput({path: USDC‖feeA‖WPROS‖feeB‖USDC, recipient: wallet, deadline: +300 s, amountIn,
amountOutMinimum})` with `amountOutMinimum = max(expected − slippage_buffer_bps, amountIn + gas)`. Atomic: either
the round trip returns at least that much or the whole tx reverts.

## Logging

Every scan appends one line to `runs/scans.jsonl` (backscan: `runs/backscan.jsonl` in the samples):
`{ts, block, source, decision, reason, oracle, pools{fee: mid}, route, grossBps, sizeUSDC, grossUSD, feesUSD,
slippageUSD, gasUSD, netUSD, netBps}`. `--json-out` writes the full record (all pairs, pool states, action).

## Safety

* Mainnet is only read (Chain MCP). Simulated execution (`--simulate`, `fork-scenario`) runs on a local anvil
  fork with the wallet impersonated; nothing is signed with a key; fork tx hashes are not on pharosscan.
* `fork-scenario` creates the dislocation itself: a real USDC holder (SharpByte USDC vault `0x15c2…aff1`) is
  impersonated **on the fork** and buys WPROS on one pool; the scanner then scans the fork, prepares the round
  trip and executes it there to show predicted vs realized profit.
* Oracle guard: a pool read more than `max_oracle_deviation_pct` (5 %) away from Chainlink is re-read and
  excluded if still off.
