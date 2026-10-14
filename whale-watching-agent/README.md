# Pharos Agent Arena · Whale-Watching Agent (Chain MCP)

Tracks a configurable list of Pharos addresses and reports **significant moves** — large transfers, new positions,
exits, swaps — with context on why each one matters, plus a **summary view** across all watched addresses.
All chain data comes from the hosted **Pharos Chain MCP server** (`https://mcp.pharos.xyz/mcp`, Pharos Pacific
mainnet, chainId 1672), read-only. Nothing is signed or sent.

Built for the Pharos *Season of AI · Agent Arena* task
[Whale-Watching Agent](https://pharos-agent-arena.vercel.app/tasks/whale-watching-agent).

## Configuration — [`whales.json`](whales.json)

* `watch` — the addresses to track (validated as 20-byte hex, no duplicates) with an optional label. The default
  list has **8 real, active mainnet addresses**, chosen from the senders/receivers of USDC and WPROS `Transfer`
  logs over ~5,000 recent blocks: the two busiest USDC/WPROS contracts, two busy wallets (EOAs), a Vault,
  a large USDC-receiving proxy, a Morpho market adapter and the LI.FI bridge diamond.
* `tokens` — tokens from the official [token registry](https://docs.pharos.xyz/getting-started/token-registry)
  (USDC, WPROS, WETH, LINK) with a **per-token `threshold`** in token units and an optional static `usd_price`.
  `symbol()` / `decimals()` are read from the contracts at start-up, not trusted from the file.
* `min_usd` — **value-terms threshold**: a move is significant if it reaches the token threshold **or**
  `amount × usd_price ≥ min_usd`. Prices are static, user-supplied numbers (USDC 1.0, WPROS 0.65); no price
  feed is fetched. Tokens without `usd_price` use only their token threshold.
* `native_threshold_pros` — the summary flags a watched address whose native PROS balance changed by at least this much.
* `labels` — known addresses (registry tokens; DEX pairs/routers, bridge, vault names read from the
  pharosscan.xyz address profile on 2026-10-14). Unknown counterparties are labelled at runtime via `get_code`
  (wallet vs contract) and `symbol()` (token contracts).

## How it works

1. **Logs** — `get_logs` with `address = [configured tokens]` and `topics = [Transfer, [watched…]]` (sender) and
   `[Transfer, null, [watched…]]` (receiver): two calls per ≤1000-block window (the server's limit). Events are
   de-duplicated by `(txHash, logIndex)` (a watched→watched transfer matches both queries); ERC-721 transfers
   (4 topics) and `removed` logs are ignored.
2. **Significance** — per-token threshold or value-terms threshold, as above.
3. **Explain each move** (requirement 03) — what moved (token, amount, ~USD), how much, where to/from with a label,
   and **why it matters**: `balanceOf(watched)` is read with `eth_call` at block N-1 and N →
   `NEW POSITION` (held none before), `EXIT` (balance went to ~0), `PASS-THROUGH` (0 before and after, e.g. a router
   or bridge), or the share of the holding moved/added. If the same tx moves another token the opposite way for the
   same address it is annotated **part of a swap: A → B**. Mints/burns (zero address) are named as such.
4. **Summary view** (requirement 04) — per watched address: transfers seen, significant count, in/out/net per token,
   native PROS change over the window (`get_balance` at window start-1 and end), largest move with link.
   Printed to the console; `--markdown` writes the same as a Markdown table file, `--json` the full data.
5. **Continuous mode** — `--follow` re-polls the chain head every `--interval` s (default 30) and reports and
   summarises each new block window.

## How it covers the task requirements

| # | Requirement | Implementation |
|---|-------------|----------------|
| 01 | Tracks ≥ 5 configurable addresses | `watch` list in `whales.json` (8 real mainnet addresses by default), any number allowed |
| 02 | Configurable "significant" threshold per token or in value terms | per-token `threshold` + `min_usd` with static `usd_price`; `native_threshold_pros` for PROS |
| 03 | Each report explains what moved, how much, where to (labelled where known) | `[WHALE MOVE]` block: token, amount, ~USD, counterparty + label, new position / exit / pass-through / % of holding, swap legs, explorer link |
| 04 | Summary view of recent activity across all watched addresses | `=== Summary` section and `--markdown` file ([`sample_summary.md`](sample_summary.md)) |

## Run it

Python 3.10+ (tested with Python 3.12.15).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python whale_watching_agent.py                                    # last 2000 blocks (~35 min) with whales.json
python whale_watching_agent.py --blocks 3000 --json sample_output.json --markdown sample_summary.md
python whale_watching_agent.py --blocks 200 --follow --interval 30   # keep watching new blocks
python whale_watching_agent.py --config my_whales.json
```

Options: `--config` (default `whales.json`), `--blocks` (1–20000, default 2000), `--follow`, `--interval` (≥5 s),
`--json`, `--markdown`, `--endpoint` (default `https://mcp.pharos.xyz/mcp`; only `https://`, or `http://` to localhost).
Cost: 2 `get_logs` calls per 1000 blocks + 2 `eth_call` per significant move + 2 `get_balance` per address;
the client backs off on HTTP 429 (server limit 120 req/min). A 3000-block run took ~25 s.

## Sample output (real run on Pharos mainnet, 2026-10-14)

`python whale_watching_agent.py --blocks 3000 --json sample_output.json --markdown sample_summary.md` —
full console output in [`sample_output.txt`](sample_output.txt) (13 significant moves out of 472 transfers),
Markdown summary in [`sample_summary.md`](sample_summary.md), data in [`sample_output.json`](sample_output.json).
A `--follow` run (3 consecutive polls) is in [`sample_output_follow.txt`](sample_output_follow.txt).

```
Pharos Whale-Watching Agent · pharos-mcp-server 0.1.0 @ https://mcp.pharos.xyz/mcp
  watching 8 addresses, tokens: USDC (>= 500), WPROS (>= 2,000), WETH (>= 0.1), LINK (>= 50), or >= $1,000 where a usd_price is set, native PROS change >= 100

[WHALE MOVE] 2026-10-14 18:39:08 UTC  block #20,244,014
  who     : 0xff70f4a1d11995621854f3692acf286d8acd04b2  [LiFiDiamond bridge (explorer)]
  what    : received 5,187.101368 WPROS (~$3,372)
  from <- : 0x1de7889c440d4fefa0e5f4f83a021f6f41cdf26b  [unlabelled contract]
  why     : >= 2,000 WPROS token threshold; PASS-THROUGH - held no WPROS before or after this block (tokens received and forwarded in the same block)
  tx      : https://www.pharosscan.xyz/tx/0x401ad0be1ed273cfbcaa2b1d34069d189979b029734296902bf75b790b48ef90

[WHALE MOVE] 2026-10-14 18:39:08 UTC  block #20,244,014
  who     : 0xff70f4a1d11995621854f3692acf286d8acd04b2  [LiFiDiamond bridge (explorer)]
  what    : sent 5,187.101368 WPROS (~$3,372)
  to   -> : 0x290d54179960984599f16f77dcda81320301b158  [unlabelled contract]
  why     : >= 2,000 WPROS token threshold; PASS-THROUGH - held no WPROS before or after this block (tokens forwarded right after receipt in the same block)
  tx      : https://www.pharosscan.xyz/tx/0x401ad0be1ed273cfbcaa2b1d34069d189979b029734296902bf75b790b48ef90

[WHALE MOVE] 2026-10-14 18:45:01 UTC  block #20,244,330
  who     : 0x936564245b69f5a293b42cbd752e0ee3bb68b470  [Active swap contract (unlabelled)]
  what    : sent 621.324307 USDC (~$621)
  to   -> : 0x912c9ade24d44d8922f0866d8dcb079f1363f647  [Most active USDC/WPROS contract (unlabelled)]
  why     : >= 500 USDC token threshold; moves 2% of the 38,171.862913 USDC held before (now 37,550.538606)
  note    : part of a swap: 621.324307 USDC -> 1,063.514102 WPROS
  tx      : https://www.pharosscan.xyz/tx/0x39dcac197ad4c9b5aa841d24358c92d746598872441e8d7ed94ad9aa4930ae12

[WHALE MOVE] 2026-10-14 18:45:39 UTC  block #20,244,371
  who     : 0xf3503da2961ff5118adaa2c4ff8d9a6fb28a411f  [ERC1967Proxy, large USDC receiver (explorer)]
  what    : received 1,552.129867 USDC (~$1,552)
  from <- : 0xd7c38e2580f6eb3e547ce77ec5495833a0d9520e  [unlabelled wallet (EOA)]
  why     : >= 500 USDC token threshold; adds <1% to the 13,704,447.528102 USDC held before (now 13,705,999.657969)
  tx      : https://www.pharosscan.xyz/tx/0x95b4f18b6f4d28e9c928fb2378553c7ce8b815d07f67012bfc9fe21f73a83081

...

=== Summary: blocks #20,243,911-#20,246,910 (2026-10-14 18:37:06 UTC -> 2026-10-14 19:32:03 UTC)
    472 transfers touching watched addresses, 13 significant

* 0x912c9ade24d44d8922f0866d8dcb079f1363f647  [Most active USDC/WPROS contract (unlabelled)]
    transfers 406, significant 3, native PROS change +0.0000
    USDC   in      11,411.585834  out       9,379.387782  net +2,032.1981
    WPROS  in      16,016.389855  out      19,492.240057  net -3,475.8502
    largest: received 621.529396 USDC from Active swap contract (unlabelled)  https://www.pharosscan.xyz/tx/0xc1bd0c092162e9205bbc14537844106ff261e3650fe1460b052f7f11d143221b
* 0x936564245b69f5a293b42cbd752e0ee3bb68b470  [Active swap contract (unlabelled)]
    transfers 212, significant 3, native PROS change +0.0000
    USDC   in       5,824.171637  out        9,320.61401  net -3,496.4424
    WPROS  in      15,916.848213  out       9,947.737758  net +5,969.1105
    largest: sent 621.529396 USDC to Most active USDC/WPROS contract (unlabelled)  https://www.pharosscan.xyz/tx/0xc1bd0c092162e9205bbc14537844106ff261e3650fe1460b052f7f11d143221b
* 0x1f3546a29eec2102c8d52e21a45114124014c85c  [High-frequency wallet (EOA)]
    transfers 176, significant 0, native PROS change -0.1698
    USDC   in       2,870.093392  out       2,282.496028  net +587.5974
    WPROS  in              3,900  out              4,900  net -1,000.0000
* 0x768b959191ce5876cabbbc014a08d72615bdde54  [Vault (explorer)]
    transfers 54, significant 4, native PROS change +0.0000
    USDC   in           5,722.09  out           5,722.09  net +0.0000
    largest: received 584.85 USDC from unlabelled contract  https://www.pharosscan.xyz/tx/0x8fbbcff6d8a362279a4f69c50384b632f39a99c835d8bcd50a88400596ae8ed5
* 0xf3503da2961ff5118adaa2c4ff8d9a6fb28a411f  [ERC1967Proxy, large USDC receiver (explorer)]
    transfers 2, significant 1, native PROS change +0.0000
    USDC   in       1,652.129867  out                  0  net +1,652.1299
    largest: received 1,552.129867 USDC from unlabelled wallet (EOA)  https://www.pharosscan.xyz/tx/0x95b4f18b6f4d28e9c928fb2378553c7ce8b815d07f67012bfc9fe21f73a83081
* 0xba3b260596d8412318ee6ef5476291f6c8e3b68a  [MorphoMarketV1AdapterV2 (explorer)]
    transfers 0, significant 0, native PROS change +0.0000
    no token transfers in this window
* 0xff70f4a1d11995621854f3692acf286d8acd04b2  [LiFiDiamond bridge (explorer)]
    transfers 2, significant 2, native PROS change +0.0000
    WPROS  in       5,187.101368  out       5,187.101368  net +0.0000
    largest: received 5,187.101368 WPROS from unlabelled contract  https://www.pharosscan.xyz/tx/0x401ad0be1ed273cfbcaa2b1d34069d189979b029734296902bf75b790b48ef90
* 0x15ddce897d76ac39c188ade4d353711a60395315  [Active wallet (EOA)]
    transfers 8, significant 0, native PROS change -0.0073
    USDC   in         583.763671  out                  0  net +583.7637
    WPROS  in                  0  out         995.957599  net -995.9576
```

## Files

* `whale_watching_agent.py` — the agent (CLI)
* `whales.json` — watch list, thresholds, labels
* `pharos_mcp.py` — minimal Chain MCP client (Streamable HTTP, JSON-RPC, 429 back-off)
* `sample_output.txt`, `sample_output.json`, `sample_summary.md`, `sample_output_follow.txt` — real runs above
* No API keys, no private keys, no transactions sent.

## License

MIT — see [LICENSE](LICENSE).
