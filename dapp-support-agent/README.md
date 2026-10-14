# Pharos Agent Arena · dApp Support Agent (Chain MCP)

A support agent that takes a **failed transaction hash** on Pharos and explains, in plain language,
**why it failed and what the user can do about it**. Everything on-chain is read through the hosted
**Pharos Chain MCP server** (`https://mcp.pharos.xyz/mcp`, Pharos Pacific mainnet, chainId 1672). It never
signs or sends anything.

Built for the Pharos *Season of AI · Agent Arena* task
[dApp Support Agent](https://pharos-agent-arena.vercel.app/tasks/dapp-support-agent).

## How it works

1. **Fetch** — `get_transaction` + `get_transaction_receipt` via Chain MCP (client in `pharos_mcp.py`,
   MCP Streamable HTTP: `initialize` → `notifications/initialized` → `tools/call`). Unknown hash → "not found";
   no receipt → "pending"; `status = 1` → "this transaction did not fail" (no invented problems).
2. **Native balance check** — `get_balance(sender, block N-1)`; if the tx `value` is larger than what the
   sender held right before the block, the cause is *not enough PROS*.
3. **Replay** — the identical call (`from`, `to`, `input`, `value`, `gas`) is re-executed with `eth_call` on the
   state of block **N-1**; the node returns the revert message and revert data. Contract creations (no `to`) are
   replayed with `estimate_gas` (latest state) instead, because the hosted `eth_call` tool requires `to`.
   * If the N-1 replay passes, it is replayed again on the state **after block N**: if that reverts, an earlier
     tx in the same block most likely changed the state first (front-run / price moved); the revert reason found
     there is used and the explanation says so. If both pass → `state_changed` ("would succeed now, retry").
   * **Out of gas** — when gas used ≥ 99% of the gas limit, the call is replayed with 4× the gas limit
     (capped at 30M); if it then succeeds, the cause is *ran out of gas*.
   * Empty revert data → `get_code(to)`: no bytecode → *target is not a contract*; otherwise *rejected without a reason*.
4. **Decode** — `Error(string)` (revert strings), `Panic(uint256)` (overflow, division by zero, …), a built-in table
   of common custom errors (OpenZeppelin `ERC20InsufficientAllowance/Balance`, `OwnableUnauthorizedAccount`,
   `Unauthorized()`, `EnforcedPause()`, `InsufficientOutputAmount(...)`, `NoRedeemRequest()`, …) with static
   arguments decoded, and — for unknown selectors only — the public
   [Sourcify 4byte signature database](https://api.4byte.sourcify.dev) (disable with `--offline-signatures`).
5. **Classify + explain** — the reason is mapped by keyword/error name to a failure type, each with a plain-language
   explanation and a concrete fix: `insufficient_native_balance`, `out_of_gas`, `insufficient_allowance`,
   `insufficient_token_balance`, `slippage`, `deadline`, `permission`, `paused`, `nothing_to_do`, `panic`,
   `contract_error` (app-specific message shown verbatim), `silent_revert`, `no_contract`, `state_changed`, `unknown`.
   Each report also lists the **evidence** (receipt status, gas used vs limit, replay result) and the fee paid.

Limitations (honest): the replay uses the state at the block boundary (N-1 or N), not the exact intra-block state,
so for race conditions the agent says "most likely". App-specific short codes (e.g. `'BC'`) are shown verbatim
because their meaning is only known to that app.

## How it covers the task requirements

| # | Requirement | Implementation |
|---|-------------|----------------|
| 01 | Accepts a tx hash, fetches tx + receipt via Chain MCP | CLI positional hashes / `--file`; validated as `0x`+64 hex; `get_transaction`, `get_transaction_receipt` |
| 02 | Identifies the failure cause (revert reason, out of gas, insufficient balance/allowance, …) | balance check (`get_balance` N-1), `eth_call` replay N-1/N, high-gas replay for out-of-gas, revert-data decoding, classification table |
| 03 | Explains without jargon and suggests a fix | per-type "What happened" / "What to do" text, contract message quoted, evidence listed separately |
| 04 | Demonstrated on ≥ 3 different failure types | real mainnet run below: **7 distinct types on 8 failed txs** (+1 successful tx) |

## Run it

Python 3.10+ (tested with Python 3.12.15).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python dapp_support_agent.py 0x39e3bf27e71dfbe0e6e5cdb38d18d66c35bc334c14427223a01ae890f1585c05
python dapp_support_agent.py --file sample_txs.txt --json sample_output.json   # the demo below
python dapp_support_agent.py --offline-signatures <txhash>                     # no external signature lookup
```

Options: `tx ...`, `--file`, `--json`, `--offline-signatures`, `--endpoint` (default `https://mcp.pharos.xyz/mcp`;
only `https://` is accepted, plain `http://` only for localhost). Each hash costs ~4–7 MCP calls (server limit 120 req/min).

## Sample output (real run on Pharos mainnet, 2026-10-14)

`python dapp_support_agent.py --file sample_txs.txt --json sample_output.json` — full text in
[`sample_output.txt`](sample_output.txt), machine-readable in [`sample_output.json`](sample_output.json).
The 8 failed hashes in [`sample_txs.txt`](sample_txs.txt) are real status-0 mainnet transactions picked from the
explorer's recent-transactions list (one per failure pattern); the last hash is a successful USDC transfer.

```
Pharos dApp Support Agent · pharos-mcp-server 0.1.0 @ https://mcp.pharos.xyz/mcp
Run at 2026-10-14 19:20:37 UTC

=== 0x39e3bf27e71dfbe0e6e5cdb38d18d66c35bc334c14427223a01ae890f1585c05
    https://www.pharosscan.xyz/tx/0x39e3bf27e71dfbe0e6e5cdb38d18d66c35bc334c14427223a01ae890f1585c05
  Block  : #20,228,451   from 0x9ea92f9949e768b2985036409d5d6b815d2ee239  to 0xa5ca5fbe34e444f366b373170541ec6902b0f75c
  Call   : selector 0xff84aafa  value 0.000000 PROS  fee paid 0.002193 PROS
  Type   : slippage  ->  Price moved too much (slippage protection)
  What happened : The swap would have given you less than the minimum you agreed to accept, because the price moved between signing and execution. The contract cancelled it to protect you. (Contract message: 'DODORouteProxy: Return amount is not enough'.)
  What to do    : Retry with a fresh quote, a smaller trade size, or a slightly higher slippage tolerance (e.g. 0.5% -> 1%).
  Evidence:
    - receipt status 0 in block #20,228,451
    - gas used 199,350 of limit 425,621 (46.8%)
    - replay reverted: 'execution reverted: DODORouteProxy: Return amount is not enough', data selector 0x08c379a0

=== 0x45720e64df6d9a4bf8f16ba9a5a46ee5e403ad459bd5c5ff7babd9d856b9d03e
    https://www.pharosscan.xyz/tx/0x45720e64df6d9a4bf8f16ba9a5a46ee5e403ad459bd5c5ff7babd9d856b9d03e
  Block  : #20,224,910   from 0x0077777711ce3371d9a00b5ccb5600e2dd3ac991  to (contract creation -> 0x9c8e87a9b72f44b996fba184f8f14413eccbe1ec)
  Call   : selector 0x60806040  value 2,000,000,000,000,000.000000 PROS  fee paid 0.005000 PROS
  Type   : insufficient_native_balance  ->  Not enough PROS in the wallet
  What happened : The transaction tried to send more PROS than the wallet held at that moment, so the network could not carry it out. The network fee for the attempt was still charged.
  What to do    : Lower the amount (leave some PROS for fees) or top up the wallet first, then send again.
  Evidence:
    - receipt status 0 in block #20,224,910
    - gas used 500,000 of limit 500,000 (100.0%)
    - tx value 2,000,000,000,000,000.000000 PROS > sender balance 0.837318 PROS at block #20,224,909

=== 0xe92ccb92eb0285a7bff19f5b9c3d9d9ce5eca00f9630c407e435859b1c4f25ff
    https://www.pharosscan.xyz/tx/0xe92ccb92eb0285a7bff19f5b9c3d9d9ce5eca00f9630c407e435859b1c4f25ff
  Block  : #20,244,982   from 0xc5f6fe56976e0a55aed6c0e08951a8a2f8980f45  to 0x936564245b69f5a293b42cbd752e0ee3bb68b470
  Call   : selector 0x2f139e4f  value 0.000000 PROS  fee paid 0.002566 PROS
  Type   : slippage  ->  Price moved too much (slippage protection)
  What happened : The swap would have given you less than the minimum you agreed to accept, because the price moved between signing and execution. The contract cancelled it to protect you. Most likely another transaction in the same block got in first and changed the state this one relied on. (Contract message: 'InsufficientOutputAmount'.) Numbers from the error: 38,823,498 / 36,035,191,538 / 36,074,011,618 (raw units).
  What to do    : Retry with a fresh quote, a smaller trade size, or a slightly higher slippage tolerance (e.g. 0.5% -> 1%).
  Evidence:
    - receipt status 0 in block #20,244,982
    - gas used 166,077 of limit 540,000 (30.8%)
    - replay of the same call on block #20,244,981 state does not revert
    - replay on block #20,244,982 state (after the block) reverts -> most likely an earlier tx in the same block changed the state first
    - replay reverted: 'execution reverted: ', data selector 0x911bb2c9
    - custom error InsufficientOutputAmount(uint256,uint256,uint256) (from built-in table) args=[38823498, 36035191538, 36074011618]

=== 0xb778d422012ef4ec564229a46dfdd76a0edfe26461a2811a7b64c88cce617f3d
    https://www.pharosscan.xyz/tx/0xb778d422012ef4ec564229a46dfdd76a0edfe26461a2811a7b64c88cce617f3d
  Block  : #20,225,269   from 0x771ec62dbda31cb950684beadb0ac4bd5205709d  to 0x81cb6fd0f18b3423ff82dde6411e6d47b26bc76a
  Call   : selector 0xb26cc394  value 0.000000 PROS  fee paid 0.000509 PROS
  Type   : permission  ->  Not allowed to call this function
  What happened : The function is restricted (owner / admin / specific role) and the sending wallet does not have that permission. (Contract message: 'Unauthorized'.)
  What to do    : Use the wallet that holds the required role, or ask the contract owner to grant it. If you are a regular user, this action is not meant for you - check you are on the right page.
  Evidence:
    - receipt status 0 in block #20,225,269
    - gas used 46,229 of limit 500,000 (9.2%)
    - replay reverted: 'execution reverted: ', data selector 0x82b42900
    - custom error Unauthorized() (from built-in table)

...
Summary: 8 failed tx diagnosed, 7 distinct failure types: contract_error, insufficient_allowance, insufficient_native_balance, insufficient_token_balance, nothing_to_do, permission, slippage
```

| Tx (explorer) | Detected type |
|---|---|
| [`0x39e3bf27…5c05`](https://www.pharosscan.xyz/tx/0x39e3bf27e71dfbe0e6e5cdb38d18d66c35bc334c14427223a01ae890f1585c05) | slippage — Price moved too much (slippage protection) |
| [`0xd2c9c032…ac64`](https://www.pharosscan.xyz/tx/0xd2c9c032e2b5e3ebd67bddaeb1caabe5b89d74b03b6b8409191fdbf0413cac64) | insufficient_allowance — Token spending permission (allowance) too low |
| [`0xcc624ade…b6dc`](https://www.pharosscan.xyz/tx/0xcc624ade283e0d0f1f1df68d1b53aa741dfd4b92e4a42ca40bb9fd81eb12b6dc) | insufficient_token_balance — Not enough tokens |
| [`0x45720e64…d03e`](https://www.pharosscan.xyz/tx/0x45720e64df6d9a4bf8f16ba9a5a46ee5e403ad459bd5c5ff7babd9d856b9d03e) | insufficient_native_balance — Not enough PROS in the wallet |
| [`0xb778d422…7f3d`](https://www.pharosscan.xyz/tx/0xb778d422012ef4ec564229a46dfdd76a0edfe26461a2811a7b64c88cce617f3d) | permission — Not allowed to call this function |
| [`0xe92ccb92…25ff`](https://www.pharosscan.xyz/tx/0xe92ccb92eb0285a7bff19f5b9c3d9d9ce5eca00f9630c407e435859b1c4f25ff) | slippage — Price moved too much (slippage protection) |
| [`0xe654cacd…f285`](https://www.pharosscan.xyz/tx/0xe654cacd9dcf31f668f331f4c72c2822b7e0e32458baa6ac8f14c5f23b90f285) | nothing_to_do — Nothing to act on |
| [`0x0d826a72…086c`](https://www.pharosscan.xyz/tx/0x0d826a7211ed03613847c884761def69361f7e12c7f4e9020de78aa51cb2086c) | contract_error — The contract rejected the transaction |
| [`0x3e1168df…1a2a`](https://www.pharosscan.xyz/tx/0x3e1168df397e9da275e75329093106eee1ea938549be3d18a53b44f949ee1a2a) | success — This transaction did not fail |

**Out of gas:** the detection path is implemented (gas used ≥ 99% of limit → replay with 4× gas), but no genuine
out-of-gas failure turned up: among the ~18,000 most recent mainnet transactions in the explorer list (419 of them
failed) we checked gas used vs. limit for one or two samples of every distinct contract+method pattern, and the only
failures that used 100% of their gas were contract creations sending more PROS than the sender had — which the agent
correctly reports as `insufficient_native_balance` (the balance check runs before the gas check).

## Files

* `dapp_support_agent.py` — the agent (CLI)
* `pharos_mcp.py` — minimal Chain MCP client (Streamable HTTP, JSON-RPC, 429 back-off, `call_tool_raw` for full error payloads)
* `sample_txs.txt`, `sample_output.txt`, `sample_output.json` — the demo run above
* No API keys, no private keys, no transactions sent.

## License

MIT — see [LICENSE](LICENSE).
