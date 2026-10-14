# Pharos Agent Arena · Docs MCP + Chain MCP Agent

An agent that **explains, then acts**: given a contract function it

1. **EXPLAIN** — asks the **Pharos Developer Docs MCP** (`https://docs.pharos.xyz/~gitbook/mcp`, tool `askQuestion`)
   what the function does, its parameters and its return values (units/decimals), and keeps the doc **sources**;
2. **CALL** — ABI-encodes the call and runs it with the **Pharos Chain MCP** (`https://mcp.pharos.xyz/mcp`, tool
   `eth_call`) on Pharos Pacific mainnet (chainId 1672), pinned to one block, and decodes the result;
3. **INTERPRET** — reads the result *in the context of the explanation*: facts the docs state are pulled out of the
   explanation text and checked against / applied to the on-chain values, then the exact values are sent back to
   the Docs MCP for a sourced reading.

Read-only: only `eth_call`, `get_block`, `get_block_number` and `get_code` are used on chain. Nothing is signed or sent.

Built for the Pharos *Season of AI · Agent Arena* task
[Docs MCP + Chain MCP Agent](https://pharos-agent-arena.vercel.app/tasks/docs-chain-mcp-agent).

## How the interpretation uses the explanation

`Agent.interpret()` in [`docs_chain_agent.py`](docs_chain_agent.py) — deterministic rules, no LLM in this repo:

| What the docs explanation says | What the agent does with the on-chain result |
|---|---|
| "`N` decimals" | reads `decimals()` on chain, reports whether it **matches the docs**, scales amounts by 10^N |
| "wei" (+ "PROS") | no `decimals()` → divides by 10^18 and labels the amount PROS |
| a field is "always 0" | confirms the returned field is 0 "exactly as the docs say" |
| freshness / staleness, heartbeat `N` seconds | converts `*At`/timestamp fields to UTC, computes age vs the call block, compares with the documented heartbeat |
| — | price pair from `description()` or the contract name (`PROS/USD` → "USD per 1 PROS"); `symbol()` for token amounts; `get_code` classifies returned addresses (contract vs EOA) |

Then a second `askQuestion` sends the decoded values, block and time to the Docs MCP ("what do these values mean?")
and prints its answer with sources (`--no-docs-interpretation` skips it).

## Run it

Python 3.10+ (tested with Python 3.12).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # requests, pycryptodome (keccak-256 for function selectors)

# the 4 bundled examples (3 contracts, 4 functions) — ~45 s each because the docs server writes the answers
python docs_chain_agent.py --examples examples.json --json results.json
python docs_chain_agent.py --examples examples.json --only 2

# any function
python docs_chain_agent.py --contract 0xc879c018db60520f4355c26ed1a6d572cdac1815 \
  --name "USDC token (ERC-20) from the Pharos token registry" \
  --function "balanceOf(address account) returns (uint256 balance)" \
  --arg 0xf3503da2961ff5118adaa2c4ff8d9a6fb28a411f
```

Options: `--contract`, `--name` (used in the docs question), `--function "name(type arg, …) returns (type name, …)"`,
`--arg` (repeatable), `--examples FILE`, `--only N`, `--block N` (default latest), `--no-docs-interpretation`,
`--json FILE`, `--chain-endpoint` / `--docs-endpoint` (only `https://`, or `http://` to localhost).
Supported ABI types: `address`, `bool`, `bytes32`, `uintN`, `intN` (arguments and returns) and `string` (returns).
Inputs are validated before any network call (address format, integer ranges, signature must include `returns (…)`).

## Bundled examples — [`examples.json`](examples.json)

All contract addresses come from the docs themselves: Chainlink PROS/USD feed from
[Chainlink Push Engine Feeds](https://docs.pharos.xyz/tooling-and-infrastructure/oracles/chainlink-pe), Multicall3 from
[Canonical Contracts](https://docs.pharos.xyz/getting-started/canonical-contracts), USDC from the
[Token Registry](https://docs.pharos.xyz/getting-started/token-registry).

| # | Contract | Function | Argument |
|---|---|---|---|
| 1 | Chainlink PROS/USD feed `0x371Ed36a…5466` | `latestAnswer()` | — |
| 2 | Chainlink PROS/USD feed | `latestRoundData()` | — |
| 3 | Multicall3 `0xcA11bde0…CA11` | `getEthBalance(address)` | WPROS contract `0x52c48d42…f0b0` |
| 4 | USDC `0xc879c018…1815` | `balanceOf(address)` | ERC1967 proxy `0xf3503da2…411f` (large USDC holder) |

## Sample output (real runs on Pharos mainnet, 2026-10-14 ~20:17–20:20 UTC)

Full transcript of all 4 runs plus input-validation errors: [`sample_output.txt`](sample_output.txt); structured
data: [`sample_output.json`](sample_output.json). Docs MCP answers are generated text, so wording differs between runs.

Example 2 — `python docs_chain_agent.py --examples examples.json --only 2` (excerpt):

```
=== Chainlink PROS/USD price feed · latestRoundData()  (0x371ed36aee5bffbed1a2aa9b7ded7f4eb30f5466)
    https://www.pharosscan.xyz/address/0x371ed36aee5bffbed1a2aa9b7ded7f4eb30f5466

1) EXPLAIN — Pharos Developer Docs MCP (askQuestion)

   `latestRoundData()` takes **no parameters**, but the Pharos documentation does **not** list it in this proxy’s documented interface. That interface lists `latestAnswer()`, `latestTimestamp()`, and `getFeedId()`, so its availability and exact ABI aren’t confirmed. See [Chainlink Push Engine Feeds](https://docs.pharos.xyz/tooling-and-infrastructure/oracles/chainlink-pe).
   
   If the proxy exposes the Chainlink round-data fields referenced there, their documented meanings are:
   
   * `roundId` and `answeredInRound` are always `0`; these feeds don’t use aggregation rounds.
   * `startedAt` and `updatedAt` are identical: the report’s **observation timestamp**, not its on-chain transaction time.
   * `answer` is the PROS/USD price. The feed uses **18 decimals**, so interpret the integer as USD per PROS divided by `10^18`.
   
   For a read-only check, use `eth_call`, which Pharos documents as a read-only contract call. The docs don’t confirm `latestRoundData()`’s return types or that this method is implemented at the address. [Pharos MCP](https://docs.pharos.xyz/tooling-and-infrastructure/mcp).

   Sources:
   [1] Pacific Mainnet — https://docs.pharos.xyz/getting-started/network/pacific-mainnet
   [2] Get PROS Gas — https://docs.pharos.xyz/getting-started/get-pros-gas
   [3] Chainlink Push Engine Feeds — https://docs.pharos.xyz/tooling-and-infrastructure/oracles/chainlink-pe
   [4] Chainlink Data Feeds — https://docs.pharos.xyz/tooling-and-infrastructure/oracles/chainlink-ds
   [5] Chainlink CCIP — https://docs.pharos.xyz/tooling-and-infrastructure/cross-chain/chainlink-ccip

2) CALL — Pharos Chain MCP eth_call at block #20,249,424 (2026-10-14 20:18:06 UTC)
   args: (none)
   calldata: 0xfeaf968c
   raw result: 0x0000000000000000000000000000000000000000000000000000000000000000…
   decoded: {"roundId": "0", "answer": "583991671837939600", "startedAt": "1792006810", "updatedAt": "1792006810", "answeredInRound": "0"}

3) INTERPRET — result in the context of the explanation
   - Docs say values use 18 decimals; decimals() on chain = 18 -> matches the docs.
   - description() on chain = 'SelfManagedFeedsCacheProxy'.
   - Price pair PROS/USD -> a price value is USD per 1 PROS.
   - roundId = 0, exactly as the docs say for this contract (always 0).
   - answer = 583991671837939600 raw -> 0.583992 USD per PROS.
   - startedAt = 1792006810 -> 2026-10-14 19:40:10 UTC, 2276 s (37.9 min) before block #20,249,424.
   - updatedAt = 1792006810 -> 2026-10-14 19:40:10 UTC, 2276 s (37.9 min) before block #20,249,424.
   - answeredInRound = 0, exactly as the docs say for this contract (always 0).
   - Docs state a 3,600 s heartbeat; updatedAt is 2,276 s old -> within one heartbeat (fresh by that measure).

   Docs MCP reading of these exact values:
   ... (Docs MCP reading of these values + sources, see sample_output.txt)
```

Example 4 — USDC `balanceOf`:

```
2) CALL — Pharos Chain MCP eth_call at block #20,249,508 (2026-10-14 20:19:26 UTC)
   args: ['0xf3503da2961ff5118adaa2c4ff8d9a6fb28a411f']
   calldata: 0x70a08231000000000000000000000000f3503da2961ff5118adaa2c4ff8d9a6fb28a411f
   raw result: 0x00000000000000000000000000000000000000000000000000000c772d3dabf1
   decoded: {"balance": "13705999657969"}

3) INTERPRET — result in the context of the explanation
   - Docs say values use 6 decimals; decimals() on chain = 6 -> matches the docs.
   - balance = 13705999657969 raw -> 13,705,999.657969 USDC.
```

Explorer links for what was read: PROS/USD feed
https://www.pharosscan.xyz/address/0x371ed36aee5bffbed1a2aa9b7ded7f4eb30f5466 ·
USDC https://www.pharosscan.xyz/address/0xc879c018db60520f4355c26ed1a6d572cdac1815 ·
Multicall3 https://www.pharosscan.xyz/address/0xca11bde05977b3631167028862be2a173976ca11

Note from the run: for `latestRoundData()` the Docs MCP itself pointed out that the documented interface of the
PROS/USD feed lists `latestAnswer()`/`latestTimestamp()` rather than `latestRoundData()`; the call still succeeds on
chain and the returned fields match the documented behaviour (18 decimals, round ids always 0) — the agent shows both.

## How it covers the task requirements

| # | Requirement | Implementation |
|---|-------------|----------------|
| 01 | Explanation comes from Docs MCP, with sources | step 1 `askQuestion` on `https://docs.pharos.xyz/~gitbook/mcp`; answer + numbered `docs.pharos.xyz` sources |
| 02 | Then calls the same function on chain via Chain MCP | step 2 `eth_call` (selector = keccak256 of the canonical signature) on `https://mcp.pharos.xyz/mcp`, pinned block |
| 03 | Interprets the result in the context of the explanation | step 3: docs facts (decimals, wei/PROS, "always 0", heartbeat/freshness) checked against and applied to the values + Docs MCP reading of the exact values |
| 04 | At least 2 different functions or contracts | 4 functions on 3 contracts in `examples.json`, any other via `--contract/--function` |

## Files

`docs_chain_agent.py` (agent) · `pharos_mcp.py` (minimal MCP Streamable-HTTP client, shared by both servers) ·
`examples.json` · `sample_output.txt` / `sample_output.json`.

## Security

No wallet, private key or API key; no transactions (`send_raw_transaction` is never called). HTTPS endpoints only.

License: MIT.
