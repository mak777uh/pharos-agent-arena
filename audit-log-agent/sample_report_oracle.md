# Wallet activity report — `0x339d413ccefd986b1b3647a9cfa9cbbe70a30749`

* Network: Pharos Pacific mainnet (chainId 1672) · data via Pharos Chain MCP (`get_logs`, nonce search, receipts)
* Period: 2026-10-14 19:20:00 → 2026-10-14 19:35:00 UTC = blocks 20,246,273–20,247,070
* Explorer: https://www.pharosscan.xyz/address/0x339d413ccefd986b1b3647a9cfa9cbbe70a30749

## Summary

Between 2026-10-14 19:20:00 and 2026-10-14 19:35:00 UTC (blocks 20,246,273–20,247,070) this wallet was involved in 7 transactions and itself sent 7, paying 0.003317 PROS in fees. It made 0 swaps (gave nothing; got nothing), 0 outgoing transfers (nothing), 0 incoming transfers (nothing), 0 token approvals and 7 other contract interactions. Native PROS balance went from 755.799472 to 755.796155.

## Totals per category

| Category | Transactions | Tokens in | Tokens out | Approvals |
|---|---|---|---|---|
| Swaps | 0 | — | — | — |
| Transfers out | 0 | — | — | — |
| Transfers in | 0 | — | — | — |
| Approvals | 0 | — | — | — |
| Other contract interactions | 7 | — | — | — |

Fees paid: 0.003317 PROS for 7 sent transactions (0 failed). Native PROS: 755.799472 → 755.796155.

## Notable transactions

* **5 call(s) to contract 0x3c2269811836af69497e5f486a85d7316753cf62 (function 0x3161b7f6)** — 2026-10-14 19:32:26 — [0x0b1c19a262…](https://www.pharosscan.xyz/tx/0x0b1c19a262d7539b01ff51924fb0b0f541d1e312bc2a23533e00b977344a3b23)
* **2 call(s) to contract 0x3c2269811836af69497e5f486a85d7316753cf62 (function 0x7dc438a4)** — 2026-10-14 19:32:25 — [0x9976ac1286…](https://www.pharosscan.xyz/tx/0x9976ac1286323be6ddd466ba06553fdb6a4274b853764aba70120f251a505e03)

## Swaps (0)

None in this period.

## Transfers out (0)

None in this period.

## Transfers in (0)

None in this period.

## Approvals (0)

None in this period.

## Other contract interactions (7)

| Time (UTC) | Block | What happened | Tx |
|---|---|---|---|
| 2026-10-14 19:22:23 | 20,246,403 | called 0x3c2269811836af69497e5f486a85d7316753cf62 (0x3161b7f6), success | [0x8c6e2ffd…](https://www.pharosscan.xyz/tx/0x8c6e2ffdfc4b52e298b1d2f174959f216d05e2cf502b512f1522abdeaa70b1ae) |
| 2026-10-14 19:22:24 | 20,246,405 | called 0x3c2269811836af69497e5f486a85d7316753cf62 (0x3161b7f6), success | [0xb8b6cd68…](https://www.pharosscan.xyz/tx/0xb8b6cd684036971d93a4f42372a4db9e5ebfb20322442e82fecd12806d8fbdb3) |
| 2026-10-14 19:27:22 | 20,246,674 | called 0x3c2269811836af69497e5f486a85d7316753cf62 (0x7dc438a4), success | [0x1d13375b…](https://www.pharosscan.xyz/tx/0x1d13375b43935e2c0de0bd1d05e37c01fe88c0ca2811503a4386de664744805c) |
| 2026-10-14 19:27:24 | 20,246,676 | called 0x3c2269811836af69497e5f486a85d7316753cf62 (0x3161b7f6), success | [0xc32a2922…](https://www.pharosscan.xyz/tx/0xc32a29221fd2b0183c0e285f643b65b8585226032e47e7a2759c3d756a5194f5) |
| 2026-10-14 19:32:25 | 20,246,933 | called 0x3c2269811836af69497e5f486a85d7316753cf62 (0x7dc438a4), success | [0x9976ac12…](https://www.pharosscan.xyz/tx/0x9976ac1286323be6ddd466ba06553fdb6a4274b853764aba70120f251a505e03) |
| 2026-10-14 19:32:25 | 20,246,933 | called 0x3c2269811836af69497e5f486a85d7316753cf62 (0x3161b7f6), success | [0xbd29386e…](https://www.pharosscan.xyz/tx/0xbd29386e4fcad0ffc671b3a74ed525fb84257d7a4f2e3c39d3e7af48bc127943) |
| 2026-10-14 19:32:26 | 20,246,934 | called 0x3c2269811836af69497e5f486a85d7316753cf62 (0x3161b7f6), success | [0x0b1c19a2…](https://www.pharosscan.xyz/tx/0x0b1c19a262d7539b01ff51924fb0b0f541d1e312bc2a23533e00b977344a3b23) |

## Coverage notes

* Sent transactions: 7 of 7 located via nonce binary search.
* Token movements come from ERC-20/721 `Transfer`, `Approval`, `ApprovalForAll` and WPROS `Deposit`/`Withdrawal` events that name the wallet; swaps are detected from opposite token legs in one tx or Uniswap V2/V3 / DODO swap events.
* Native PROS received from others (and internal transfers) do not emit events, so they are not itemised; the start/end PROS balance is shown instead.
