# Batching reference

## Action types

| type | fields | becomes |
|---|---|---|
| `send_pros` | `to`, `amount` (PROS) | Multicall3 call with `value`, empty calldata |
| `morpho_accrue` | `market_id` (bytes32) | `accrueInterest(MarketParams)` on Morpho Blue `0x18573fa1…4efb`; params read on chain via `idToMarketParams(id)` |
| `call` | `to`, `data`, optional `value` (PROS) | raw call; `msg.sender` is Multicall3 — only for permissionless calls |

3–100 actions per batch, unique ids. ERC-20 `transfer`/`approve`/`transferFrom` to registry tokens are refused.

## Pipeline (all through Chain MCP `https://mcp.pharos.xyz/mcp`)

1. `get_chain_info` (1672), `get_code(Multicall3)`, `get_block_number`, `get_gas_price`, Chainlink PROS/USD.
2. **Alone**: `eth_call` + `estimate_gas` of each action from the sender → success, revert reason, gas.
3. **In batch**: `eth_call` of `aggregate3Value` with `allowFailure=true` for every call, `value = Σ values` →
   decoded `Result[] (success, returnData)`; a failed call's revert data is decoded (Error/Panic/custom/empty).
4. **Partial failure** (`--on-failure`):
   * `drop` (default): failed actions are removed; the rest is re-encoded with `allowFailure=false` and
     re-simulated (`eth_call` + `estimate_gas`). If that still reverts, nothing is prepared.
   * `abort`: nothing is prepared when any action fails.
5. **Prepare**: unsigned EIP-1559 tx to Multicall3 `0xcA11bde0…CA11` (gas limit = estimate + 20 %, pending nonce,
   fees), EIP-1193 request and the `cast send` line for the user. The sender's PROS must cover value + max fee.
6. **Saving**: Σ individual `estimate_gas` vs the batch `estimate_gas`, fees at the current gas price, txs and
   signatures saved.

## Why `allowFailure=false` in the final tx

With `aggregate3Value`, a sub-call that fails under `allowFailure=true` leaves its `value` inside Multicall3, where
anyone can take it. The skill finds failures in simulation (step 3), removes them and then submits an
all-or-nothing batch, so if state changes before mining the whole tx reverts instead of stranding funds.

## When batching saves gas

Every separate transaction pays the 21,000 base cost. Batching saves it for actions with execution cost of their
own (keeper calls: 5 actions 298,644 → 216,731 gas, −27.4 % on 2026-10-14). Plain PROS transfers have no
execution cost besides the base, and a value call made by a contract costs ~9,000 gas, so 4 transfers cost
84,000 gas separately and 88,060 batched (+4.8 %) — the batch still saves 3 signatures and nonces.
