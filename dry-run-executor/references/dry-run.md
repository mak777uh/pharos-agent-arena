# How the dry run works

1. **Build** the call: `transfer` (ERC-20 `transfer(address,uint256)` or a plain PROS send), `swap`
   (`exactInputSingle` on router `0xf38d34c8382b9079b0f85309578b43b8479cd875`, the router seen in the WPROS/USDC
   pool's real swaps; fee 100, recipient = sender, deadline = now + 10 min), `wrap`/`unwrap` (WPROS
   `deposit()` / `withdraw(uint256)`), or `raw`. Amounts use on-chain `decimals()`.
2. **Simulate** with Chain MCP: `get_chain_info` (must be 1672), `get_block_number`, `get_code(to)`,
   `eth_call` → success + return data, or revert data decoded as `Error(string)`, `Panic(uint256)`, a few common
   custom errors, or "reverted without a reason"; then `estimate_gas`.
3. **Cost**: expected fee = `estimate_gas × get_gas_price`; worst case = gas limit (estimate + 20 %) × maxFeePerGas
   (2 × base fee + tip); USD via Chainlink PROS/USD `0x371ed36a…5466` `latestRoundData()`.
4. **Predict** (successful simulations only): `debug_traceCall` with `callTracer` + `withLog` → all logs of
   non-failed frames. `Transfer` → −from/+to; WPROS `Deposit` → +dst; `Withdrawal` → −src; frames carrying value →
   native PROS −from/+to; the sender additionally pays the expected fee. Other events (`Swap`, `Approval`) are
   listed. "Before" balances are read via Chain MCP (`eth_call balanceOf` at the head block, `get_balance` for
   PROS); after = before + change.
5. **Gate**: BLOCKED if the simulation reverted, or if the sender's PROS < value + worst-case fee. Otherwise GO and
   an unsigned EIP-1559 transaction is prepared (pending nonce, fees, gas limit) with an EIP-1193 request and the
   `cast send … --private-key $PRIVATE_KEY` line for the user. `--no-prepare` skips the preparation.

Limits: the prediction is for the head block; a trace RPC outage leaves the simulation and cost intact and marks
the prediction unavailable. Tokens with non-standard events (rebasing, fee-on-transfer) are shown as their events
say.
