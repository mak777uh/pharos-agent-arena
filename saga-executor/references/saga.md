# Saga reference

## Steps (forward)

| # | step | tx | depends on | verified by (receipt + chain read) |
|---|---|---|---|---|
| 1 | `approve_usdc` | `USDC.approve(router, amount)` | preflight | status 1 · `Approval(sender, router)` = amount · `allowance()` ≥ amount |
| 2 | `swap` | `router.exactInputSingle(USDC→WPROS, fee 100, amountIn, minOut)` | allowance from 1 | status 1 · USDC `Transfer` from sender = amountIn · WPROS `Transfer` to sender ≥ minOut → `wpros_received` |
| 3 | `approve_wpros` | `WPROS.approve(vault, wpros_received)` | the swap **receipt** amount | status 1 · `Approval(sender, vault)` = wpros_received |
| 4 | `deposit` | `vault.deposit(wpros_received, sender)` | allowance from 3 | status 1 · ERC-4626 `Deposit` assets = wpros_received, shares > 0 · vault `balanceOf` grew by exactly `shares` |

Contracts (re-checked on mainnet in the preflight: code, `vault.asset() == WPROS`, pool tokens, pool fee):
USDC `0xc879…1815`, WPROS `0x52c4…f0b0`, SwapRouter `0xf38d…d875`, pool WPROS/USDC `0x912c…f647` (0.01 %),
Vishwa WPROS Vault `0x6ce7…3e73`.

## Where it runs

* **Mainnet, read-only, Chain MCP** (`https://mcp.pharos.xyz/mcp`): `get_chain_info`, `get_code` ×5, `eth_call`
  (asset, token0/1, fee, slot0, liquidity, Chainlink, balances), `get_balance`, `get_transaction_count`,
  `get_gas_price`, `get_block`; step 1 `eth_call` + `estimate_gas`; `verify-receipt` uses `get_transaction` +
  `get_transaction_receipt`.
* **Local fork**: `anvil --fork-url https://rpc.pharos.xyz --no-mining` (upstream from `assets/networks.json`).
  The sender is impersonated (no key). Blocks are mined by the executor one at a time, which lets it model a
  mempool: drop a tx, spike the base fee, order a competing tx first. Receipts are real EVM receipts from real
  mainnet contract code and state at the fork block.

## Retry policy (`assets/saga.json → retry`)

| failure | detected by | class | action |
|---|---|---|---|
| tx dropped from mempool | no receipt and `eth_getTransactionByHash` = null after mining | transient | resend, **same nonce** |
| underpriced / base-fee spike | evicted or not mined in `confirm_blocks`, base fee > maxFee | transient | replace same nonce, `maxFee = max(prev × (1+fee_bump_pct), 2 × base + tip)` |
| fee needed above `max_fee_gwei` | the bump rule | budget exhausted → permanent | stop, compensate |
| `Too little received` (price moved before inclusion) | receipt status 0, reason via `debug_traceTransaction` | transient if `requote_on_slippage` | new quote, new minOut, new tx (the failed tx consumed its nonce) |
| simulation reverts (any other reason) | `eth_call` before sending | permanent | not sent, compensate |
| receipt status 0 (other reason) / post-condition mismatch | receipt + logs + chain read | permanent | compensate |
| `max_attempts` reached | | permanent | compensate |

`backoff_seconds` between attempts. Nonces are read from the chain (`latest`) at the start of every step.

## Compensation (reverse order, each skipped when the chain shows nothing to undo)

0. `cancel_pending` — if the last sent tx has no receipt, a 0-PROS self-transfer at the same nonce with a
   higher fee supersedes it, so the abandoned tx can never land later. Verified: original hash has no receipt
   and the nonce is used.
1. `revoke_wpros_vault` — `WPROS.approve(vault, 0)` if an allowance is left.
2. `approve_wpros_router` + `swap_back` — `exactInputSingle(WPROS→USDC, wpros_received, minOut)` with a fresh
   quote − slippage; verified by the USDC `Transfer` to the sender.
3. `revoke_usdc_router` — `USDC.approve(router, 0)` if an allowance is left (after a successful swap it is
   already 0, so this is usually SKIPPED).

Every compensation step is first **simulated** (`eth_call` + `estimate_gas`) against the stranded fork state and
**prepared** as an unsigned chainId-1672 EIP-1559 tx (in the JSON report). By default it is then executed on the
fork with the same retry policy to prove the funds come back; `--no-execute-compensation` stops after
simulate + prepare. If a compensation step fails, the saga ends `NEEDS_MANUAL_ACTION` (exit 4).

## Fault injection

| `--inject` | what happens on the fork | expected path |
|---|---|---|
| `drop@<step>` | first send of the step is removed with `anvil_dropTransaction` | RETRYING (dropped) → same nonce → VERIFIED |
| `spike@<step>` | base fee forced to 40 gwei while the step is pending | evicted underpriced → fee bump 20 → 80 gwei → VERIFIED |
| `frontrun@swap` | a competing 3,000 USDC→WPROS swap from a real USDC holder (SharpByte USDC vault `0x15c2…aff1`, impersonated on the fork, PROS for gas set with `anvil_setBalance`) is sent with a higher tip and lands first in the same block | RECEIPT_FAIL `Too little received` → re-quote → VERIFIED |
| `revert@deposit` | deposit amount doubled and sent without the pre-send simulation gate | RECEIPT_FAIL `TransferFromReverted()` → permanent → compensation |
| `spike-persist@<step>` | base fee 400 gwei, fee cap 100 gwei | budget exhausted → `cancel_pending` + compensation |

## State log

`runs/<saga_id>.jsonl`, one line per transition, flushed immediately:
`{seq, ts, saga_id, phase (forward|compensation|saga), step, index (1/4…, C1…), state, detail, attempt, tx, block, gas}`.
States: STARTED, INJECT, SIMULATED, SIM_REVERT, SENT, RETRYING, RECEIPT_FAIL, VERIFIED, VERIFY_FAIL, FAILED,
COMPENSATING, PREPARED, SKIPPED, COMPLETED, COMPENSATED, COMPENSATION_PREPARED, NEEDS_MANUAL_ACTION.
`saga.py status --log …` prints the current phase/state and the last step; `--history` prints all of it.
