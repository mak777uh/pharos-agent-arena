# Pharos Agent Arena · Audit-Log Agent (Chain MCP)

Generates a **plain-language activity report for one wallet over a chosen date range** — the kind of summary a
compliance officer or accountant can read — grouped into **swaps, transfers out, transfers in, approvals and other
contract interactions**, with totals per category, notable transactions with explorer links, and **Markdown + CSV**
export. All chain data comes from the hosted **Pharos Chain MCP server** (`https://mcp.pharos.xyz/mcp`, Pharos Pacific
mainnet, chainId 1672), read-only; no indexer, no API key, nothing signed or sent.

Built for the Pharos *Season of AI · Agent Arena* task
[Audit-Log Agent](https://pharos-agent-arena.vercel.app/tasks/audit-log-agent).

## How it works

1. **Dates → blocks** — `--start` / `--end` are UTC (`YYYY-MM-DD` or `YYYY-MM-DDTHH:MM`; a bare end date means the end
   of that day). A **binary search on block timestamps** (`get_block`) finds the first block at/after the start and the
   last block at/before the end (~25 calls each).
2. **Events naming the wallet** — `get_logs` (no contract filter, so every token counts), paged in ≤1000-block windows:
   * `topics = [[Transfer, Approval, ApprovalForAll, Deposit, Withdrawal], wallet]` → tokens sent, approvals given,
     WPROS-style wrap/unwrap;
   * `topics = [Transfer, null, wallet]` → tokens received (ERC-20, and ERC-721 with the token id);
   * `topics = [[UniswapV2 Swap, UniswapV3 Swap, DODOSwap], null, wallet]` → DEX swaps whose recipient is the wallet.
3. **Transactions the wallet sent** — `get_transaction_count` at start-1 and end gives how many; for each nonce a
   **binary search on the nonce** finds the block containing it, `get_block(includeTransactions)` gives the tx and
   `get_transaction_receipt` its status, fee and emitted events (to spot swap events in the tx). This also finds
   txs that emit no wallet-related event (pure contract calls, failed txs, native PROS sends). Capped by `--max-sent`.
4. **One category per transaction** — `swap` (wallet gave one asset and got another in the same tx, or a known DEX swap
   event, or a wrap/unwrap) › `transfer_out` › `transfer_in` › `approval` › `contract_interaction`. Native PROS sent
   with a tx is an `out` leg (so a PROS→token trade counts as a swap). Transfers to/from the zero address are noted as
   mint/burn; an `Approval` amount ≥ 2²⁵⁵ (e.g. 2²⁵⁶-1) is flagged **UNLIMITED**, 0 as a revoke.
5. **Totals + notable** — per category: tx count, tokens in/out (symbol/decimals via `eth_call`), approvals; fees paid,
   failed txs, native PROS balance at start and end (`get_balance`). Notable: unlimited approvals, failed txs, calls per
   contract (for contract interactions), and the largest `--top` moves per token — each with a pharosscan link.
6. **Export** — Markdown report (`--markdown`, default `report.md`) and CSV line items (`--csv`, default `report.csv`:
   time, block, tx, category, direction, token, amount, counterparty, note, status, fee, link).

### Range limits — keep ranges small
The hosted `get_logs` accepts **≤1000 blocks per call** and the server allows **120 requests/min per IP**. Pharos makes
~1 block per second, so one hour ≈ 3,300 blocks ≈ 12 `get_logs` calls, plus ~15 calls per transaction the wallet
sent. The agent therefore **refuses ranges longer than `--max-blocks` (default 20,000 blocks ≈ 5.5 h)** — narrow the
dates or raise the flag knowingly. The 2 h 20 min sample below took ~55 s.

Known gaps (stated in every report): native PROS **received** from others and internal transfers emit no events, so
they are not itemised (start/end PROS balance is shown); swaps routed so that the wallet is not named in any event are
seen only as transfers; categorisation is heuristic.

## How it covers the task requirements

| # | Requirement | Implementation |
|---|-------------|----------------|
| 01 | Accepts a wallet address and a date range | `--wallet` (validated 0x+40 hex), `--start` / `--end` (UTC, validated) → blocks by timestamp binary search |
| 02 | Groups activity into transfers in/out, swaps, approvals, contract interactions | event + sent-tx classification above (`CATEGORIES` in the code) |
| 03 | Totals per category and notable transactions with links | "Totals per category" table + "Notable transactions" with `https://www.pharosscan.xyz/tx/…` links |
| 04 | Exports Markdown, PDF or CSV | Markdown (`--markdown`) **and** CSV (`--csv`) |

## Run it

Python 3.10+ (tested with Python 3.12.15).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python audit_log_agent.py --wallet 0x50fbb8651e332696ba14b04e17175f960ec18ab2 \
    --start 2026-10-14T17:20 --end 2026-10-14T19:40 --markdown sample_report.md --csv sample_report.csv
python audit_log_agent.py --wallet <0x…> --start 2026-10-14 --end 2026-10-14 --max-blocks 90000   # a full day (slow)
```

Options: `--wallet`, `--start`, `--end`, `--max-blocks` (20000), `--max-sent` (100), `--top` (3), `--markdown`
(`report.md`), `--csv` (`report.csv`), `--endpoint` (default `https://mcp.pharos.xyz/mcp`; only `https://`, or
`http://` to localhost). Progress lines go to stderr.

## Sample output (real runs on Pharos mainnet, 2026-10-14)

### 1. A new wallet's first 2 h 20 min — all categories but "other contract interactions"
Console ([`sample_output.txt`](sample_output.txt)); report [`sample_report.md`](sample_report.md); CSV
[`sample_report.csv`](sample_report.csv).

```
Pharos Audit-Log Agent · pharos-mcp-server 0.1.0 @ https://mcp.pharos.xyz/mcp
  wallet 0x50fbb8651e332696ba14b04e17175f960ec18ab2
  period 2026-10-14 17:20:00 -> 2026-10-14 19:40:00 UTC  =  blocks #20,239,502 - #20,247,338 (7,837 blocks)
  logs: 10 wallet events; locating sent txs by nonce search ...
  sent txs: 7 of 7 located; reading receipts ...

Between 2026-10-14 17:20:00 and 2026-10-14 19:40:00 UTC (blocks 20,239,502–20,247,338) this wallet was involved in 9 transactions and itself sent 7, paying 0.010799 PROS in fees. It made 2 swaps (gave 15,008.156206 USDC, 14 PROS; got 14,576.357551 APC3M, 8.156206 USDC), 2 outgoing transfers (14,576.357551 APC3M, 11.622022 WPROS), 2 incoming transfers (15,000 USDC, 11.622022 WPROS), 3 token approvals and 0 other contract interactions. Native PROS balance went from 0 to 10.428802.

  Swaps                           2 txs   in: 14,576.357551 APC3M, 8.156206 USDC   out: 15,008.156206 USDC, 14 PROS
  Transfers out                   2 txs   in: -   out: 11.622022 WPROS, 14,576.357551 APC3M
  Transfers in                    2 txs   in: 11.622022 WPROS, 15,000 USDC   out: -
  Approvals                       3 txs   in: -   out: -   approvals: 3
  Other contract interactions     0 txs   in: -   out: -

Notable:
  - UNLIMITED approval of WPROS to 0xb300000b72deaeb607a12d5f54773d1c19c7028d  2026-10-14 17:51:17  https://www.pharosscan.xyz/tx/0xd260f86d7ec8f8a78dcbd4c2aac1a1c6f9b8e4d92c06d44258a445c6f7d8a14e
  - largest WPROS moves: out 11.622022 WPROS (transfer_out)  2026-10-14 17:51:34  https://www.pharosscan.xyz/tx/0xe9636fbac290f139f293e68aebc816862bdcc7ebe343cd4cce29c5fc5e560373
  - largest WPROS moves: in 11.622022 WPROS (transfer_in)  2026-10-14 17:44:49  https://www.pharosscan.xyz/tx/0x1b65ce158b364f776615ae92b82ecb00a9269cac5524347ae72bfef4d3e40916
  - largest USDC moves: out 15,008.156206 USDC (swap)  2026-10-14 18:05:37  https://www.pharosscan.xyz/tx/0xed75573c66ca0c9b211af8ea53858ce45de62dcf41640f53a3c8ec651ed004b3
  - largest USDC moves: in 15,000 USDC (transfer_in)  2026-10-14 17:52:28  https://www.pharosscan.xyz/tx/0xf4138f7552a1a145d1bc4ffda3f89470ced93fbce4bc4ac1dcf4888b58281c43
  - largest USDC moves: in 8.156206 USDC (swap)  2026-10-14 17:53:23  https://www.pharosscan.xyz/tx/0xce12eab1a8665ab71ddedb1fd47aec508ff44f7302c57803fbdda8ab68764370
  - largest APC3M moves: out 14,576.357551 APC3M (transfer_out)  2026-10-14 18:06:31  https://www.pharosscan.xyz/tx/0xf53c93ec1c1e550106d186922eb4b2733bc19c5dd7d4d8fc54c6d5ca06eb1f21
  - largest APC3M moves: in 14,576.357551 APC3M (swap)  2026-10-14 18:05:37  https://www.pharosscan.xyz/tx/0xed75573c66ca0c9b211af8ea53858ce45de62dcf41640f53a3c8ec651ed004b3
  - largest PROS moves: out 14 PROS (swap)  2026-10-14 17:53:23  https://www.pharosscan.xyz/tx/0xce12eab1a8665ab71ddedb1fd47aec508ff44f7302c57803fbdda8ab68764370

Wrote sample_report.md and sample_report.csv
```

Excerpt of [`sample_report.md`](sample_report.md):

#### Totals per category

| Category | Transactions | Tokens in | Tokens out | Approvals |
|---|---|---|---|---|
| Swaps | 2 | 14,576.357551 APC3M, 8.156206 USDC | 15,008.156206 USDC, 14 PROS | — |
| Transfers out | 2 | — | 11.622022 WPROS, 14,576.357551 APC3M | — |
| Transfers in | 2 | 11.622022 WPROS, 15,000 USDC | — | — |
| Approvals | 3 | — | — | 3 |
| Other contract interactions | 0 | — | — | — |

Fees paid: 0.010799 PROS for 7 sent transactions (0 failed). Native PROS: 0 → 10.428802.

#### Notable transactions

* **UNLIMITED approval of WPROS to 0xb300000b72deaeb607a12d5f54773d1c19c7028d** — 2026-10-14 17:51:17 — [0xd260f86d7e…](https://www.pharosscan.xyz/tx/0xd260f86d7ec8f8a78dcbd4c2aac1a1c6f9b8e4d92c06d44258a445c6f7d8a14e)
* **largest WPROS moves: out 11.622022 WPROS (transfer_out)** — 2026-10-14 17:51:34 — [0xe9636fbac2…](https://www.pharosscan.xyz/tx/0xe9636fbac290f139f293e68aebc816862bdcc7ebe343cd4cce29c5fc5e560373)
* **largest WPROS moves: in 11.622022 WPROS (transfer_in)** — 2026-10-14 17:44:49 — [0x1b65ce158b…](https://www.pharosscan.xyz/tx/0x1b65ce158b364f776615ae92b82ecb00a9269cac5524347ae72bfef4d3e40916)
* **largest USDC moves: out 15,008.156206 USDC (swap)** — 2026-10-14 18:05:37 — [0xed75573c66…](https://www.pharosscan.xyz/tx/0xed75573c66ca0c9b211af8ea53858ce45de62dcf41640f53a3c8ec651ed004b3)
* **largest USDC moves: in 15,000 USDC (transfer_in)** — 2026-10-14 17:52:28 — [0xf4138f7552…](https://www.pharosscan.xyz/tx/0xf4138f7552a1a145d1bc4ffda3f89470ced93fbce4bc4ac1dcf4888b58281c43)
* **largest USDC moves: in 8.156206 USDC (swap)** — 2026-10-14 17:53:23 — [0xce12eab1a8…](https://www.pharosscan.xyz/tx/0xce12eab1a8665ab71ddedb1fd47aec508ff44f7302c57803fbdda8ab68764370)
* **largest APC3M moves: out 14,576.357551 APC3M (transfer_out)** — 2026-10-14 18:06:31 — [0xf53c93ec1c…](https://www.pharosscan.xyz/tx/0xf53c93ec1c1e550106d186922eb4b2733bc19c5dd7d4d8fc54c6d5ca06eb1f21)
* **largest APC3M moves: in 14,576.357551 APC3M (swap)** — 2026-10-14 18:05:37 — [0xed75573c66…](https://www.pharosscan.xyz/tx/0xed75573c66ca0c9b211af8ea53858ce45de62dcf41640f53a3c8ec651ed004b3)
* **largest PROS moves: out 14 PROS (swap)** — 2026-10-14 17:53:23 — [0xce12eab1a8…](https://www.pharosscan.xyz/tx/0xce12eab1a8665ab71ddedb1fd47aec508ff44f7302c57803fbdda8ab68764370)

#### Swaps (2)

| Time (UTC) | Block | What happened | Tx |
|---|---|---|---|
| 2026-10-14 17:53:23 | 20,241,454 | received 8.156206 USDC from 0xff70f4a1…; sent 14 PROS to 0xb300000b… (native PROS); called 0xb300000b72deaeb607a12d5f54773d1c19c7028d (0x810c705b), success; DEX event: Uniswap V3 Swap | [0xce12eab1…](https://www.pharosscan.xyz/tx/0xce12eab1a8665ab71ddedb1fd47aec508ff44f7302c57803fbdda8ab68764370) |
| 2026-10-14 18:05:37 | 20,242,156 | sent 15,008.156206 USDC to 0xf3503da2…; received 14,576.357551 APC3M from 0x00000000… (minted to wallet); called 0xd0428799fbc35557834d33121ba4472692c8908a (0x9f68b575), success | [0xed75573c…](https://www.pharosscan.xyz/tx/0xed75573c66ca0c9b211af8ea53858ce45de62dcf41640f53a3c8ec651ed004b3) |

…

CSV ([`sample_report.csv`](sample_report.csv)), first rows:

```
time_utc,block,tx_hash,category,direction,token,token_address,amount,counterparty,note,tx_status,fee_pros,explorer_link
2026-10-14 17:44:49,20240926,0x1b65ce158b364f776615ae92b82ecb00a9269cac5524347ae72bfef4d3e40916,transfer_in,in,WPROS,0x52c48d4213107b20bc583832b0d951fb9ca8f0b0,11.622022,0xcb79097744d5266bfca287a43612d9613be46300,,not sent by wallet,,https://www.pharosscan.xyz/tx/0x1b65ce158b364f776615ae92b82ecb00a9269cac5524347ae72bfef4d3e40916
2026-10-14 17:51:17,20241312,0xd260f86d7ec8f8a78dcbd4c2aac1a1c6f9b8e4d92c06d44258a445c6f7d8a14e,approval,approval,WPROS,0x52c48d4213107b20bc583832b0d951fb9ca8f0b0,1.158e+59,0xb300000b72deaeb607a12d5f54773d1c19c7028d,UNLIMITED approval,success,0.000464,https://www.pharosscan.xyz/tx/0xd260f86d7ec8f8a78dcbd4c2aac1a1c6f9b8e4d92c06d44258a445c6f7d8a14e
2026-10-14 17:51:34,20241332,0xe9636fbac290f139f293e68aebc816862bdcc7ebe343cd4cce29c5fc5e560373,transfer_out,out,WPROS,0x52c48d4213107b20bc583832b0d951fb9ca8f0b0,11.622022,0xb300000b72deaeb607a12d5f54773d1c19c7028d,,success,0.001867,https://www.pharosscan.xyz/tx/0xe9636fbac290f139f293e68aebc816862bdcc7ebe343cd4cce29c5fc5e560373
```

### 2. An oracle updater — "other contract interactions"
`--wallet 0x339d413ccefd986b1b3647a9cfa9cbbe70a30749 --start 2026-10-14T19:20 --end 2026-10-14T19:35`
→ 7 sent txs, all `contract_interaction` (no token events): [`sample_output_oracle.txt`](sample_output_oracle.txt),
[`sample_report_oracle.md`](sample_report_oracle.md), [`sample_report_oracle.csv`](sample_report_oracle.csv).

```
Notable:
  - 5 call(s) to contract 0x3c2269811836af69497e5f486a85d7316753cf62 (function 0x3161b7f6)  2026-10-14 19:32:26  https://www.pharosscan.xyz/tx/0x0b1c19a262d7539b01ff51924fb0b0f541d1e312bc2a23533e00b977344a3b23
  - 2 call(s) to contract 0x3c2269811836af69497e5f486a85d7316753cf62 (function 0x7dc438a4)  2026-10-14 19:32:25  https://www.pharosscan.xyz/tx/0x9976ac1286323be6ddd466ba06553fdb6a4274b853764aba70120f251a505e03
```

## Files

* `audit_log_agent.py` — the agent (CLI)
* `pharos_mcp.py` — minimal Chain MCP client (Streamable HTTP, JSON-RPC, 429 back-off)
* `sample_*` — the real runs above
* No API keys, no private keys, no transactions sent.

## License

MIT — see [LICENSE](LICENSE).
