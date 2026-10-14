# Pharos Agent Arena · Natural-Language Payment Agent (Chain MCP, prepare-only)

Turns a plain-language instruction such as **"send 100 USDC to alice"** into a fully validated, **unsigned**
EIP-1559 transaction for Pharos Pacific mainnet (chainId 1672) that you review and sign in your own wallet.
All chain data comes from the hosted **Pharos Chain MCP** (`https://mcp.pharos.xyz/mcp`): `get_chain_info`,
`get_code`, `eth_call` (`symbol`, `decimals`, `balanceOf`, `isBlacklisted`), `get_balance`, `estimate_gas`,
`get_gas_price`, `get_block`, `get_transaction_count`.

**Prepare-only.** The agent never holds or asks for a private key, never signs and never broadcasts. Its output
is a wallet signing request (EIP-1193 `eth_sendTransaction`), the canonical unsigned EIP-1559 serialization with
its signing hash, and an EIP-681 payment URI. Signing and sending is left to the user.

Built for the Pharos *Season of AI · Agent Arena* task
[Natural-Language Payment Agent](https://pharos-agent-arena.vercel.app/tasks/natural-language-payment-agent).

## Pipeline

1. **Parse** (`nl_pay.parse`, rule-based, no LLM key needed): amount (`100`, `1,250.5`, `2.5k`, `fifty`), token
   (`USDC`, `PROS`, `WPROS`, `LINK`, `WETH` — the Pacific mainnet
   [token registry](https://docs.pharos.xyz/getting-started/token-registry)), recipient (`0x…` address or a name
   from `contacts.json`).
2. **Follow-up questions** for anything missing or ambiguous instead of guessing: no token (`pay bob 25`),
   currency words that are not a unique token (`$`, `dollars`, `USDT` — not in the registry, `ETH` → WETH?),
   several amounts or tokens, unknown or misspelt contact (`alic` → "did you mean alice?"), ENS-style names
   (`alice.eth`: ENS does not resolve on Pharos), bad EIP-55 checksum, missing signer wallet.
   Interactive by default (answers read from stdin); `--non-interactive` prints the open questions as JSON and
   exits with code 3 so another agent/UI can ask them.
3. **Validate on chain** (`pay_core.Chain.prepare`): chainId is 1672; token contract has code and its `symbol()`
   matches the registry, `decimals()` read on chain; recipient is not zero / payer / the token contract, warns if
   it is a contract; USDC `isBlacklisted` for payer and recipient; **ERC-20 balance ≥ amount** (`balanceOf`);
   `estimate_gas` simulation must succeed; **PROS balance ≥ value + max gas fee**; nonce =
   `get_transaction_count(sender, "pending")`. Any failure → `REFUSED`, nothing is produced (exit 1).
4. **Human-readable summary** with every check, then a confirmation prompt (`--yes` skips it).
5. **Unsigned transaction**: `type 0x2`, `chainId 1672`, nonce, `gas` = estimate +20 %, `maxPriorityFeePerGas` =
   network suggestion (`get_gas_price`), `maxFeePerGas` = 2 × latest `baseFeePerGas` + tip, ERC-20
   `transfer(address,uint256)` calldata (or native `value`). `--out tx.json` saves everything as JSON.

## Run it

Python 3.10+ (tested with 3.12).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python nl_pay.py --from 0xYourWallet "send 100 USDC to alice"
python nl_pay.py --from 0xYourWallet --yes --out tx.json "transfer 0.25 PROS to 0x1F3546a29EeC2102c8d52E21a45114124014c85C"
python nl_pay.py --non-interactive "send 20 or 30 to alic"      # questions as JSON, exit 3
python -m unittest discover -s tests -v                         # offline tests
```

Options: `--contacts FILE` (address book `{name: address}`, default `contacts.json`, `''` = none),
`--endpoint` (Chain MCP URL; only `https://`, or `http://` to localhost), `--yes`, `--non-interactive`, `--out`.
Exit codes: 0 prepared/cancelled, 1 refused by a check, 2 bad input, 3 needs input.

`contacts.json` is a **demo** address book: the names `alice`/`bob` are our labels, the addresses are real public
EOAs on Pharos mainnet that were active in USDC at the time of the runs. The signer used in the samples,
[`0x84DBd231…F072a`](https://www.pharosscan.xyz/address/0x84dbd23178ba7fc6a2a396559139aef69d0f072a), is likewise a
real public EOA (6,845 USDC, 7.14 PROS at run time) used only so that balances, simulation and nonce are real —
we do not control it and nothing was signed.

## Sample output (real runs on Pharos mainnet, 2026-10-14 21:16 UTC)

All transcripts are in [`samples/`](samples/) (01–08), the saved JSON in
[`samples/tx_send_100_usdc.json`](samples/tx_send_100_usdc.json) and
[`samples/tx_native_pros.json`](samples/tx_native_pros.json).

**"send 100 USDC to alice"** ([`01`](samples/01_send_100_usdc_to_alice.txt)):

```
Instruction: 'send 100 USDC to alice'
Parsed     : amount=100 token=USDC recipient=alice
=== Payment summary (review before signing) ===
  Pay        : 100 USDC  (contract 0xc879c018db60520f4355c26ed1a6d572cdac1815)
  To         : 0x1F3546a29EeC2102c8d52E21a45114124014c85C  https://www.pharosscan.xyz/address/0x1f3546a29eec2102c8d52e21a45114124014c85c
  From/signer: 0x84DBd23178bA7fc6A2a396559139AEF69d0F072a
  Network    : Pharos mainnet, chainId 1672, nonce 2164, gas limit 70,344
  Fees       : maxFeePerGas 20 gwei, tip 0 gwei -> at most 0.00140688 PROS network fee
  Checks:
    [ok] Chain MCP is on Pharos mainnet (chainId 1672)
    [ok] token USDC 0xc879c018db60520f4355c26ed1a6d572cdac1815 has code, decimals 6 (read on chain)
    [ok] recipient is a regular account (no contract code)
    [ok] payer not blacklisted by USDC
    [ok] recipient not blacklisted by USDC
    [ok] payer USDC balance 6,845.265664 >= 100 (balanceOf)
    [ok] simulation OK: estimate_gas 58,620 -> gas limit 70,344 (+20%)
    [ok] signer PROS balance 7.142356537 >= value + max gas fee 0.00140688 (get_balance)
    [ok] nonce 2164 = get_transaction_count(sender, 'pending')
    [warn] recipient resolved from your address book: 'alice'
? Prepare this as an unsigned transaction for your wallet? [y/N]
> y
=== Unsigned transaction (NOT signed, NOT sent) ===
  Wallet signing request (EIP-1193):
  {"method": "eth_sendTransaction", "params": [{"from": "0x84DBd23178bA7fc6A2a396559139AEF69d0F072a", "to": "0xC879C018dB60520F4355C26eD1a6D572cdAC1815", "value": "0x0", "data": "0xa9059cbb0000000000000000000000001f3546a29eec2102c8d52e21a45114124014c85c0000000000000000000000000000000000000000000000000000000005f5e100", "gas": "0x112c8", "maxFeePerGas": "0x4a817c800", "maxPriorityFeePerGas": "0x0", "nonce": "0x874", "chainId": "0x688", "type": "0x2"}]}
  Unsigned EIP-1559 serialization: 0x02f86e820688820874808504a817c800830112c894c879c018db60520f4355c26ed1a6d572cdac181580b844a9059cbb0000000000000000000000001f3546a29eec2102c8d52e21a45114124014c85c0000000000000000000000000000000000000000000000000000000005f5e100c0
  Signing hash (keccak256 of the above): 0x9f3be79dc53cc7bec6681b8517477bf1349bd18263fb7be34c15da008c28e711
  EIP-681 URI: ethereum:0xC879C018dB60520F4355C26eD1a6D572cdAC1815@1672/transfer?address=0x1F3546a29EeC2102c8d52E21a45114124014c85C&uint256=100000000
  Sign it in your own wallet; this tool holds no key and never broadcasts.
```

**Follow-ups instead of guesses** — `'send $50 to alice.eth'` ([`03`](samples/03_followup_usd_and_ens.txt)):

```
? USD is not a token: did you mean USDC (Circle, the only USD stablecoin in the Pharos mainnet registry)?
> yes
? 'alice.eth' is a name-service name. ENS does not resolve on Pharos (chainId 1672) and it is not in your address book. What is the 0x address?
> 0x1F3546a29EeC2102c8d52E21a45114124014c85c
? recipient 0x1F3546a29EeC2102c8d52E21a45114124014c85c fails the EIP-55 checksum (typo?) - expected 0x1F3546a29EeC2102c8d52E21a45114124014c85C. Please give a contact name or a valid 0x address.
> 0x1F3546a29EeC2102c8d52E21a45114124014c85C
```

`pay bob 25` asks only for the token ([`02`](samples/02_followup_missing_token.txt)); `send 20 or 30 to alic`
with `--non-interactive` returns four questions as JSON (amount, token, "did you mean 'alice'?", signer)
([`04`](samples/04_non_interactive_questions.txt)).

**Refusals** ([`05`](samples/05_refused_insufficient_balance.txt), [`07`](samples/07_link_balance_zero.txt)):

```
Instruction: 'send 10,000 USDC to bob'
    [FAIL] payer USDC balance 6,845.265664 >= 10000 (balanceOf)
REFUSED: insufficient USDC: payer has 6,845.265664, needs 10000. No transaction was prepared.
```

Native PROS (`transfer 0.25 PROS to alice`, gas limit 25,200, EIP-681 `…@1672?value=250000000000000000`) is in
[`06`](samples/06_native_pros.txt).

## How it covers the task requirements

| # | Requirement | Implementation |
|---|-------------|----------------|
| 01 | Parses amount, token and recipient from free text; asks a follow-up when ambiguous | `parse()` + question loop (`ask`/`apply_answer`), `--non-interactive` JSON questions; samples 02–04, parser tests |
| 02 | Validates recipient format, token contract, sufficient balance | EIP-55 checksum, zero/self/token-contract guards, `get_code`, on-chain `symbol`/`decimals` vs registry, `isBlacklisted`, `balanceOf`, `estimate_gas`, PROS-for-gas; samples 05, 07 |
| 03 | Human-readable summary before the unsigned transaction | `render()` summary + every check, then the confirmation prompt; only after "y" (or `--yes`) is the tx printed |
| 04 | Never signs or sends; output is an unsigned tx or wallet signing request | no key handling anywhere; output = EIP-1193 request + unsigned serialization + signing hash + EIP-681 URI; `signed: false, broadcast: false` in the JSON |

## Honesty notes / limits

* The parser is deterministic (regex + small vocabulary), not an LLM: it covers common English phrasings and asks
  when unsure rather than inventing values. Names resolve only through the local address book (no ENS on Pharos).
* The unsigned serialization and signing hash were cross-checked with `eth-account` (`TypedTransaction`) while
  building; that vector is pinned in `tests/test_offline.py`. `eth-account` is not a dependency.
* Nonce, gas and fees are as of the run; if the signer sends other transactions first, re-run before signing.
* Nothing was signed or broadcast and no funds were moved, so there is no transaction hash of ours; the proof
  links are the explorer pages of the real accounts used.

## Security

No private key, wallet, seed or API key. HTTPS-only endpoint (Chain MCP), inputs validated (addresses + checksum,
amount > 0 and ≤ token decimals, 500-character limit on the instruction). License: MIT.
