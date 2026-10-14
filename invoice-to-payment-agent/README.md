# Pharos Agent Arena · Invoice-to-Payment Agent (Chain MCP, prepare-only)

Reads an invoice or payment request — **plain text, PDF, structured JSON, or an EIP-681 payment URI** — extracts
payee, wallet, amount, token/currency, reference, due date and network, cross-checks everything, and only when
nothing is missing or mismatched prepares the matching **unsigned** EIP-1559 transaction on Pharos Pacific mainnet
(chainId 1672) **with the invoice reference embedded in the calldata**. On-chain validation goes through the
hosted **Pharos Chain MCP** (`https://mcp.pharos.xyz/mcp`).

**Prepare-only.** No private key, no signing, no broadcasting: the output is a wallet signing request
(EIP-1193 `eth_sendTransaction`), the unsigned EIP-1559 serialization and its signing hash. Signing and sending
is left to the user's own wallet.

Built for the Pharos *Season of AI · Agent Arena* task
[Invoice-to-Payment Agent](https://pharos-agent-arena.vercel.app/tasks/invoice-to-payment-agent).

## What it does

1. **Load** (`load_invoice`): `%PDF-` → text via `pypdf` (falls back to the `pdftotext` CLI); `{…}` → JSON
   schema (`invoice_number`/`reference`, `payee.{name,wallet}`, `payer.wallet`, `currency`, `total`,
   `due_date`, `network`, `line_items[]`, `subtotal`, `tax`); `ethereum:` → EIP-681 (`…/transfer?address=&uint256=`
   or native `?value=`, optional `ref=` parameter); anything else → text. Inputs over 200 kB are rejected.
2. **Extract** (text/PDF, `parse_text`): labelled fields (`Invoice No/#/Number`, `Reference`, `PO`, `From/Payee/
   Vendor`, `Due date`, `Network/Chain`, `Currency/Token`, `Pay to wallet / Wallet / Address / Remit`, `Bill to
   wallet`), the totals block (`Subtotal`, `Tax/VAT (x%)`, `Discount`, `Total due/Amount due/Total`) with currency
   before or after the number, and line items (`description   [qty x price]   amount`) between the
   `Description/Item` header and the totals.
3. **Flag instead of guessing** (`check`) — any of these blocks the payment (exit 1):
   * `MISSING` reference, payee wallet, amount or currency;
   * `MALFORMED`/`INVALID` wallet (format or EIP-55 checksum), `AMBIGUOUS` payee (two different pay-to addresses);
   * `MISMATCH` line `qty × price ≠ amount`, subtotal ≠ Σ items, total ≠ subtotal + tax − discount;
   * `MISMATCH` currency (total in one, `Currency:` line in another); fiat (`EUR`, `GBP`, …) or tokens not on
     Pharos mainnet (`USDT`, `ETH`) → refused; `USD` only with an explicit `--accept-usd-as-usdc`;
   * `MISMATCH` network (invoice asks for another chain);
   * `MISMATCH` payee wallet vs the vendor book (`vendors.json`) — the classic "our wallet changed" invoice fraud;
   * `MISMATCH` payer (invoice billed to a different wallet than `--from`);
   * `DUPLICATE` reference already in the local ledger (`--ledger`, written with `--record`).
   Notes (non-blocking): overdue, no network stated (assumes Pharos), no line items to cross-check, new vendor.
4. **On-chain validation + unsigned tx** (`pay_core.Chain.prepare`, same checks as a normal payment): chainId,
   token contract code + `symbol`/`decimals` read on chain, USDC `isBlacklisted`, `balanceOf` ≥ amount,
   `estimate_gas` simulation, PROS for gas, nonce from `get_transaction_count(…, "pending")`, fees from
   `get_gas_price` + latest `baseFeePerGas` (`maxFeePerGas` = 2 × base + tip).
5. **Invoice reference in the transaction**: calldata = `transfer(payee, amount)` **followed by the bytes
   `PHAROS-REF:<reference>`**. The ERC-20 ABI decoder ignores trailing calldata, so the transfer is unchanged, but
   the reference is stored on chain in the tx input and anyone can read it back (`pay_core.decode_memo`). We
   verified on mainnet with `estimate_gas`/`eth_call` that USDC accepts it (estimate_gas 58,620 for a plain 100-USDC transfer vs 59,110 with `PHAROS-REF:INV-2026-0042`).
   For native PROS the reference is the tx `data` itself. (EIP-681 has no field for extra calldata, so for
   referenced payments the EIP-1193 request is the signing format.)

## Run it

Python 3.10+ (tested with 3.12).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python invoice_agent.py --from 0xYourWallet samples/invoices/inv_text_lumen.txt
python invoice_agent.py --from 0xYourWallet --record --out payment.json samples/invoices/inv_pdf_orbit.pdf
python invoice_agent.py --from 0xYourWallet "ethereum:0xC879C018dB60520F4355C26eD1a6D572cdAC1815@1672/transfer?address=0x…&uint256=15000000&ref=TIP-1"
python make_sample_pdf.py samples/invoices/inv_pdf_orbit.pdf      # regenerates the sample PDF
python -m unittest discover -s tests -v                            # offline tests
```

Options: `--vendors` (default `vendors.json`), `--ledger` (default `ledger.jsonl`), `--record`,
`--accept-usd-as-usdc`, `--today YYYY-MM-DD` (fixes "today" for the overdue check), `--out`, `--endpoint`
(`https://` only, or `http://` to localhost). Exit codes: 0 prepared, 1 flagged/refused, 2 unreadable input.

## Demo with sample invoices

[`samples/invoices/`](samples/invoices/) holds the demo invoices (the businesses are invented for the demo; every
wallet address is a real public account on Pharos mainnet so that balances and simulation are real). The payer in
all runs is the real public EOA
[`0x84DBd231…F072a`](https://www.pharosscan.xyz/address/0x84dbd23178ba7fc6a2a396559139aef69d0f072a)
(6,845 USDC, 7.14 PROS at run time); we do not control it and nothing was signed.

| Sample run | Invoice | Format | Result |
|---|---|---|---|
| [01](samples/01_text_invoice_ok.txt) | [`inv_text_lumen.txt`](samples/invoices/inv_text_lumen.txt) | text | 250 USDC, ref `INV-2026-0042` → unsigned tx, recorded in ledger |
| [02](samples/02_json_invoice_ok.txt) | [`inv_json_northwind.json`](samples/invoices/inv_json_northwind.json) | JSON | 129.50 USDC, ref `NW-7781` → unsigned tx |
| [03](samples/03_pdf_invoice_ok.txt) | [`inv_pdf_orbit.pdf`](samples/invoices/inv_pdf_orbit.pdf) | PDF | 90 USDC, ref `ORB-1009` → unsigned tx |
| [04](samples/04_eip681_request_ok.txt) | [`payment_request_eip681.txt`](samples/invoices/payment_request_eip681.txt) | EIP-681 | 15 USDC, ref `TIP-2026-10` → unsigned tx |
| [05](samples/05_flag_mismatches.txt) | [`inv_text_mismatch.txt`](samples/invoices/inv_text_mismatch.txt) | text | flagged: bad checksum, network Ethereum, total 300 ≠ items 280 (+ overdue note) |
| [06](samples/06_flag_missing_fields.txt) | [`inv_text_missing.txt`](samples/invoices/inv_text_missing.txt) | text | flagged: no reference, no wallet, EUR is fiat |
| [07](samples/07_flag_changed_vendor_wallet.txt) | [`inv_json_fraud.json`](samples/invoices/inv_json_fraud.json) | JSON | flagged: wallet differs from vendor book ("bank details changed") |
| [08](samples/08_flag_duplicate.txt) | `inv_text_lumen.txt` again | text | flagged: DUPLICATE `INV-2026-0042` |
| [09](samples/09_flag_wrong_payer.txt) | `inv_json_northwind.json`, other `--from` | JSON | flagged: billed to a different wallet |
| [10](samples/10_offline_tests.txt) | — | — | 10 offline tests pass |

The saved payment JSONs are [`samples/payment_INV-2026-0042.json`](samples/payment_INV-2026-0042.json),
[`samples/payment_NW-7781.json`](samples/payment_NW-7781.json), [`samples/payment_ORB-1009.json`](samples/payment_ORB-1009.json),
and the ledger after runs 01–03 is [`samples/ledger.jsonl`](samples/ledger.jsonl).

**Text invoice → payment summary + unsigned tx with the reference** (run 01, 2026-10-14 21:24 UTC):

```
Extracted fields:
  format                : text
  reference             : INV-2026-0042
  payee                 : Lumen Design Studio
  payee wallet          : 0x1F3546a29EeC2102c8d52E21a45114124014c85C
  amount                : 250.00
  currency/token        : USDC
  due date              : 2026-10-31
  network               : Pharos mainnet (chainId 1672)
  bill-to wallet        : 0x84DBd23178bA7fc6A2a396559139AEF69d0F072a
  line items            : 2
  subtotal/tax/discount : 250.00/None/None
  [note] payee wallet matches the vendor book entry for 'Lumen Design Studio'
=== Payment for invoice INV-2026-0042 (Lumen Design Studio), due 2026-10-31 (review before signing) ===
  Pay        : 250 USDC  (contract 0xc879c018db60520f4355c26ed1a6d572cdac1815)
  To         : 0x1F3546a29EeC2102c8d52E21a45114124014c85C  https://www.pharosscan.xyz/address/0x1f3546a29eec2102c8d52e21a45114124014c85c
  From/signer: 0x84DBd23178bA7fc6A2a396559139AEF69d0F072a
  Reference  : INV-2026-0042  (embedded in calldata as 'PHAROS-REF:INV-2026-0042')
  Network    : Pharos mainnet, chainId 1672, nonce 2164, gas limit 70,932
  Fees       : maxFeePerGas 20 gwei, tip 0 gwei -> at most 0.00141864 PROS network fee
  Checks:
    [ok] Chain MCP is on Pharos mainnet (chainId 1672)
    [ok] token USDC 0xc879c018db60520f4355c26ed1a6d572cdac1815 has code, decimals 6 (read on chain)
    [ok] recipient is a regular account (no contract code)
    [ok] payer not blacklisted by USDC
    [ok] recipient not blacklisted by USDC
    [ok] payer USDC balance 6,845.265664 >= 250.00 (balanceOf)
    [ok] simulation OK: estimate_gas 59,110 -> gas limit 70,932 (+20%)
    [ok] signer PROS balance 7.142356537 >= value + max gas fee 0.00141864 (get_balance)
    [ok] nonce 2164 = get_transaction_count(sender, 'pending')
  Calldata reference check: decode_memo(data) = 'INV-2026-0042'
=== Unsigned transaction (NOT signed, NOT sent) ===
  Wallet signing request (EIP-1193):
  {"method": "eth_sendTransaction", "params": [{"from": "0x84DBd23178bA7fc6A2a396559139AEF69d0F072a", "to": "0xC879C018dB60520F4355C26eD1a6D572cdAC1815", "value": "0x0", "data": "0xa9059cbb0000000000000000000000001f3546a29eec2102c8d52e21a45114124014c85c000000000000000000000000000000000000000000000000000000000ee6b280504841524f532d5245463a494e562d323032362d30303432", "gas": "0x11514", "maxFeePerGas": "0x4a817c800", "maxPriorityFeePerGas": "0x0", "nonce": "0x874", "chainId": "0x688", "type": "0x2"}]}
  Unsigned EIP-1559 serialization: 0x02f886820688820874808504a817c8008301151494c879c018db60520f4355c26ed1a6d572cdac181580b85ca9059cbb0000000000000000000000001f3546a29eec2102c8d52e21a45114124014c85c000000000000000000000000000000000000000000000000000000000ee6b280504841524f532d5245463a494e562d323032362d30303432c0
  Signing hash (keccak256 of the above): 0xe440e30cd930ba25e7b9da31f66519a8ad5b1f37607fa1f154e1ecbbfda84787
  Sign it in your own wallet; this tool holds no key and never broadcasts.
```

(`…0ee6b280` = 250,000,000 units = 250 USDC; the trailing `504841524f532d5245463a…` is `PHAROS-REF:INV-2026-0042`.)

**Mismatches flagged, nothing prepared** (run 05):

```
  [note] invoice is OVERDUE (due 2026-10-01)
FLAGGED - not preparing a payment until these are resolved with the payee:
  [!] INVALID payee wallet 0x1F3546a29EeC2102c8d52E21a45114124014c85c fails the EIP-55 checksum (typo?) - expected 0x1F3546a29EeC2102c8d52E21a45114124014c85C
  [!] MISMATCH network: invoice asks for 'Ethereum', this agent pays on Pharos mainnet (chainId 1672)
  [!] MISMATCH total 300.00 != line items 280.00 = 280.00
```

**Changed vendor wallet** (run 07):

```
FLAGGED - not preparing a payment until these are resolved with the payee:
  [!] MISMATCH payee wallet: 'Northwind Hosting' is on file with 0x4ffd9c7e8641947662a43af298b734ca93530002, invoice says 0x1f3546a29eec2102c8d52e21a45114124014c85c (possible invoice fraud - verify out of band)
```

## How it covers the task requirements

| # | Requirement | Implementation |
|---|-------------|----------------|
| 01 | Extracts payee, amount, token/currency and reference from ≥ 2 invoice formats | 4 formats: text, PDF, JSON, EIP-681 (`load_invoice`, `parse_text`, `parse_json`, `parse_eip681`); runs 01–04 |
| 02 | Flags mismatches or missing fields instead of guessing | `check()`: MISSING / INVALID / AMBIGUOUS / MISMATCH (items, subtotal, total, currency, network, vendor wallet, payer) / DUPLICATE → no tx; runs 05–09 |
| 03 | Payment summary and an unsigned transaction that includes the invoice reference | `render()` summary + `PHAROS-REF:<ref>` appended to the ERC-20 `transfer` calldata, decoded back in the output; EIP-1193 request + unsigned EIP-1559 serialization + signing hash |
| 04 | Demo with sample invoices | `samples/invoices/` (7 inputs incl. a PDF made by `make_sample_pdf.py`) and the 10 recorded runs in `samples/` |

## Honesty notes / limits

* Extraction is deterministic (labels + regexes), not OCR or an LLM: it reads text-based PDFs; a scanned image
  PDF yields no text and is then flagged as missing fields, never guessed.
* The ledger is local (duplicate guard for this agent). It records `prepared`, not `paid`: the agent never sees the
  signed transaction. The reference is in the calldata, so a paid invoice can be found on chain by its input data.
* Nonce, gas and fees are as of the run; re-run right before signing if the payer sent other transactions.
* Nothing was signed or broadcast and no funds moved, so there is no tx hash of ours; proof links are the explorer
  pages of the real accounts used. The unsigned serialization/signing hash is cross-checked against an
  `eth-account` vector in `tests/test_pay_core.py`.

## Security

No private key, wallet, seed or API key. HTTPS-only Chain MCP endpoint, every address validated (format +
EIP-55), file size capped, amounts must be positive and fit the token's decimals. License: MIT.
