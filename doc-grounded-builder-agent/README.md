# Pharos Agent Arena · Doc-Grounded Builder Agent ("how do I X on Pharos?" → docs plan → do X → verify on chain)

Ask *"how do I … on Pharos?"*. The agent:

1. **plans from the Pharos Developer Docs MCP** (`https://docs.pharos.xyz/~gitbook/mcp`, `askQuestion`), shows the
   answer with its sources, and **extracts the facts the action needs from that answer** (contract address, chain
   id, RPC method, event topic, block-range cap) — each fact printed with the docs page it came from;
2. **does X with the Pharos Chain MCP** (`https://mcp.pharos.xyz/mcp`): reads directly, and anything that moves
   funds is only **prepared** (unsigned tx + `eth_call` simulation from the sender + `estimate_gas`);
3. **verifies the outcome on chain** with independent checks and reports `SUCCESS` / `FAILED` / `PREPARED`
   against the plan — and after the user has signed a prepared transfer, `--tx <hash>` checks its receipt against
   the plan;
4. **says so when the docs don't cover the request** instead of improvising.

No key, no signing, no broadcasting, no spending.

Built for the Pharos *Season of AI · Agent Arena* task
[Doc-Grounded Builder Agent](https://pharos-agent-arena.vercel.app/tasks/doc-grounded-builder-agent).

## Requirement map

| # | Requirement | How |
|---|---|---|
| 01 | Docs MCP plans, sources cited, before acting | step **1) PLAN**: the docs answer (code blocks folded), its sources, then *"plan facts taken from the docs"* — every fact with its docs URL. Follow-up docs questions are asked when the plan needs another fact (e.g. the Multicall3 address from *Canonical Contracts*, the `get_logs` cap from *Pharos MCP*). No chain call happens before this step. |
| 02 | executes with Chain MCP; fund-moving steps prepared for signature | step **2) ACT**: `get_chain_info`, `get_balance`, `eth_call`, `get_code`, `get_logs`, `get_transaction_receipt`, `estimate_gas`, `get_gas_price`. The send recipe emits an **unsigned** ERC-20 `transfer` (`chainId`, `from`, `to`, `data`, `value`) and simulates it from the sender. |
| 03 | verifies on chain, success/failure vs. the plan | step **3) VERIFY**: e.g. chain id = documented value and a new block appears 4 s later; balance equals an independent read via Multicall3 `getEthBalance` at the same block; the documented token address has code and `symbol()` matches; the newest `Transfer` log is really in its receipt (status 1, same token/topic/amount); a signed transfer's receipt matches the plan's token, from, to and amount. Verdict line `⇒ SUCCESS / FAILED / PREPARED / STOPPED`. |
| 04 | ≥ 3 different "how do I…" requests | 5 recipes, 6 executed requests in [`sample_output.txt`](sample_output.txt): chain-id check, PROS balance, USDC address + total supply, USDC `Transfer` events, send USDC (prepare) and send USDC (verify a signed tx). |
| 05 | request the docs don't cover → say so | *"How do I wrap PROS into WPROS?"* (the docs give the WPROS address but no wrapping method) and *"How do I borrow USDC on Morpho on Pharos?"* → `NOT COVERED BY THE DOCS … I won't improvise steps or send anything`. Also: when a covered answer lacks a fact a recipe needs (e.g. no mainnet address in that particular answer), the run **STOPS** with *"the docs answer did not give …; not improvising"* instead of falling back to a built-in value (this happened in test runs when an answer omitted the address; the final sample run had all facts). |

## Recipes (no hard-coded addresses)

| Recipe (matched on the request) | Facts it needs from the docs | Chain MCP actions | Verification |
|---|---|---|---|
| `chain_check` — *chain ID / connected to mainnet* | chain id (`1672`), hex form | `get_chain_info` | id and hex equal the docs; block number advances |
| `native_balance` — *balance* (`--address`) | `eth_getBalance`, chain id, wei → PROS; follow-up: Multicall3 address | `get_balance` at block N | equals `Multicall3.getEthBalance` at block N; chain id matches |
| `token_supply` — *total supply / contract address* (`--token`) | mainnet token address, `totalSupply()`, `decimals()` | `eth_call` totalSupply/decimals/symbol | address has code; `symbol()` = requested token; supply > 0 |
| `token_events` — *events / logs* (`--token`) | Transfer topic (literal hash checked against keccak256, or the docs' instruction to filter by the Transfer event signature → keccak256 computed); follow-ups: token address, `get_logs` cap | `get_logs` over exactly the documented cap (1,000 blocks) | newest log present in its receipt with the same token/topic/amount, status 1 |
| `send_token` — *send / transfer / pay* (`--from --to --amount [--token]`) | mainnet token address; "keep PROS for fees"; same-network warning | build unsigned `transfer`; `eth_call` from sender; `estimate_gas`; balances | `symbol()` matches; sender balance ≥ amount; PROS ≥ fee; simulation ok → `PREPARED`. With `--tx`: receipt status 1 + `Transfer` from the documented contract with the planned from/to/amount → `SUCCESS` |

Addresses are parsed from the docs answer: an address counts as the mainnet one when the network keyword right
after it (`0x… on Pacific Mainnet`) or the nearest one before it (`Mainnet: 0x…`) names the mainnet.
`askQuestion` is generative, so wording differs between runs; the extractors accept the phrasings seen in testing
and otherwise stop.

## Real run

[`sample_output.txt`](sample_output.txt) (+ [`sample_output.json`](sample_output.json)): `python builder_agent.py
--examples examples.json` on 2026-10-14 against the live Docs MCP and Pharos mainnet — see the file for the full
plan / act / verify trace of every request. The post-signature verifier is shown on a **real 600 USDC transfer made
by someone else**
([`0xe12e4b54…09e5`](https://www.pharosscan.xyz/tx/0xe12e4b5452d0bd36900458f26148e3810051792d407e3c49075275edd2db09e5),
0x84ddbccb… → 0x3e79f7ff…), because this agent never signs anything itself; note that tx was an operator's
`transferFrom`, and the verifier checks the resulting `Transfer` event, which is identical.

## Run it

Python 3.10+ (tested with 3.12). ~20–40 s per request (the docs server generates each answer).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # requests, pycryptodome

python builder_agent.py "How do I check that I am connected to Pharos mainnet (chain ID)?"
python builder_agent.py "How do I check the PROS balance of an address?" --address 0xd27cb53f5920d484261cc14d32094d3f043265c9
python builder_agent.py "How do I find the USDC contract address and read its total supply?" --token USDC
python builder_agent.py "How do I query Transfer events of a token on Pharos?" --token USDC
python builder_agent.py "How do I send USDC to another address on Pharos?" --from 0xSENDER --to 0xRECIPIENT --amount 25
python builder_agent.py "How do I send USDC to another address on Pharos?" --from 0xSENDER --to 0xRECIPIENT --amount 25 --tx 0xSIGNED_TX_HASH
python builder_agent.py --examples examples.json --json out.json      # the batch in sample_output.txt
```

## Files

| File | Role |
|---|---|
| `builder_agent.py` | the agent: Docs client (answer + sources + coverage detection), fact extractors, 5 recipes, verifier, renderer |
| `examples.json` | the 8 requests of the sample run |
| `pharos_mcp.py` | MCP Streamable-HTTP client (HTTPS only, session handling, 429 back-off, UTF-8 SSE decoding) |
| `sample_output.txt` / `sample_output.json` | real run output |
| `run_samples.sh` | regenerates the sample |

## Limits (honest)

* Recipe selection is a keyword router over 5 recipes, not an LLM; a covered request with no recipe gets
  `EXPLAINED ONLY` (docs answer shown, nothing done on chain).
* SkillEngine is not used — everything executes through the Chain MCP.
* "Not covered" detection relies on the docs server saying so ("can't find", "don't document", "can't provide
  reliable steps", or no sources); a confidently wrong docs answer would be followed — the on-chain checks
  (code present, `symbol()` match, independent re-reads) are there to catch that.

## Safety

Read-only + prepare-only; no private key, no signing, no `send_raw_transaction`; HTTPS endpoints only; addresses,
amounts, tx hashes and question length validated.

## Proof

Verified transfer (post-signature verification against the plan):
https://www.pharosscan.xyz/tx/0xe12e4b5452d0bd36900458f26148e3810051792d407e3c49075275edd2db09e5

License: MIT.
