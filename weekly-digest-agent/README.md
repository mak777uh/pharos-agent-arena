# Pharos Agent Arena · Weekly Digest Agent (Morpho Blue on Pharos mainnet)

A scheduled agent that writes a **weekly summary of the Morpho Blue lending protocol on Pharos Pacific mainnet**
(chainId 1672, Morpho at
[`0x18573fa18fd17ddfd790b4a5b5b2977aad3b4efb`](https://www.pharosscan.xyz/address/0x18573fa18fd17ddfd790b4a5b5b2977aad3b4efb)):
what changed, key numbers compared with the previous week, and the top 3 notable events — delivered to the console
and as a Markdown + HTML page per period. Read-only.

Built for the Pharos *Season of AI · Agent Arena* task
[Weekly Digest Agent](https://pharos-agent-arena.vercel.app/tasks/weekly-digest-agent).

**Latest published digest (real run):** [`digests/digest_2026-10-12T0000Z.md`](digests/digest_2026-10-12T0000Z.md)
(+ [HTML](digests/digest_2026-10-12T0000Z.html)) — week 2026-10-05 → 2026-10-12 00:00 UTC vs the week before.

## What each digest contains

| Section | How it is computed |
|---|---|
| **Key metrics, this period vs previous** (requirement 02) | Total supplied, total borrowed and utilization across all Morpho markets at the **last block of each period** (`Morpho.market(id)` via `eth_call` pinned to that block); PROS/USD from the Chainlink feed [`0x371ed36a…5466`](https://www.pharosscan.xyz/address/0x371ed36aee5bffbed1a2aa9b7ded7f4eb30f5466) `latestRoundData()` (its `decimals()` is read on chain); number of transactions touching Morpho; unique accounts; count and USD volume of each Morpho event type (Supply, Withdraw, Borrow, Repay, SupplyCollateral, WithdrawCollateral, Liquidate, CreateMarket, FlashLoan) |
| **Top 3 notable events** (requirement 03) | All Morpho events of the period, ranked: liquidations first, then new markets, then by USD size; each with amount, market, block and a Pharosscan tx link |
| **Biggest market moves** | the 3 markets whose supply + borrow changed most between the two period ends |
| **Sources** | the blocks every number was read at and which contracts/functions were used |

USD values: USDC counted as $1.00 (assumption), WPROS at the Chainlink PROS/USD price, collateral tokens through
each market's own Morpho oracle (`price()`, 1e36 scale). If a market cannot be read after 3 tries it is left out
of **both** periods' totals and named in the digest — never guessed.

## How it reads the chain

* **Pharos Chain MCP** `https://mcp.pharos.xyz/mcp` (hosted, read-only, no key): `get_block` (binary search on
  block timestamps maps the period start/end to block numbers), `get_transaction_receipt` (the Morpho events
  themselves, decoded with the Morpho Blue `EventsLib` signatures), `eth_call` (`market()`, `idToMarketParams()`,
  oracle `price()`, token `symbol()`/`decimals()`, Chainlink).
* **Which transactions** touched Morpho in a period comes from the public Pharosscan explorer index
  (`https://api.socialscan.io/pharos-mainnet/v1/explorer`: token transfers to/from Morpho + direct calls), because a
  node can only find them by scanning every block (~0.6 M blocks per week at ~1 s blocks, 1,000 blocks per
  `get_logs` call).
* **Markets**: discovered every run from `CreateMarket` events in the receipts of direct `createMarket()` calls
  (11 found), unioned with [`known_markets.json`](known_markets.json) (12 known on 2026-10-14; WPROS/USDC was created
  through another contract) and any market seen in the period's events; new ids are appended to the file.

## Schedule (requirement 01)

* `--schedule` keeps the agent running and delivers one digest per period. **Weekly by default**, aligned to
  `--anchor` (default `monday 00:00` UTC); `--period daily | <N>d | <N>h` (1 h … 31 d) changes it — non-weekly
  periods align to multiples of the period since the Unix epoch. After each digest it prints when the next one is
  due and sleeps until then (+30 s so the boundary block exists).
* A state file (`--state`, default `state_digest.json`) records delivered periods, so a restart or a second run
  never sends the same period twice.
* Prefer cron? Run it once per period instead — the state file gives the same no-duplicate guarantee:
  `5 0 * * 1  cd /path/weekly-digest-agent && .venv/bin/python weekly_digest_agent.py` (Mondays 00:05 UTC).

## Delivery (requirement 04) — honest note

Implemented channel: **console + files**. Every digest is printed and written to `--out` (default `digests/`) as
`digest_<period end>.md` and a self-contained `digest_<period end>.html` page (all text HTML-escaped; links only
`https://`). The page is "published" by committing it — the sample digest above is viewable on GitHub. Email,
Telegram and Discord delivery are **not** implemented (no bot tokens or accounts were used), so reviewers who
require one of those channels should count this item as a published page only.

## Run it

Python 3.10+ (tested with Python 3.12).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt                  # requests, pycryptodome (keccak for selectors/topics)

python weekly_digest_agent.py                                  # last 7 days vs the 7 days before, once
python weekly_digest_agent.py --end 2026-10-12T00:00 --json data.json
python weekly_digest_agent.py --schedule                       # every Monday 00:00 UTC, forever
python weekly_digest_agent.py --schedule --period daily --out /var/www/digests
python weekly_digest_agent.py --schedule --period 1h --runs 2  # quick demo of the scheduler
```

Inputs are validated (`--period`, `--end` = `YYYY-MM-DDTHH:MM` UTC, `--anchor`, `--runs`, market ids in the JSON);
the MCP endpoint must be `https://` (or `http://` to localhost). A weekly digest takes ~1–3 minutes (receipts of
each Morpho tx + ~60 `eth_call`s + ~50 `get_block`s for the period boundaries).

## Sample output (real runs on Pharos mainnet, 2026-10-14 ~22:00 UTC)

* [`sample_output.txt`](sample_output.txt) — weekly digest for 2026-10-05 → 2026-10-12 00:00 UTC
  (raw data: [`sample_digest_data.json`](sample_digest_data.json)), the duplicate-delivery check, and input
  validation errors. Excerpt:

```
| Total supplied (end of period) | $13,480,189.07 | $13,767,557.74 | -2.1% |
| Total borrowed (end of period) | $12,237,329.30 | $12,557,303.57 | -2.5% |
| Utilization | 90.78% | 91.21% | -0.43 pp |
| PROS/USD (Chainlink) | $0.678833 | $0.812833 | -16.5% |
| Transactions touching Morpho | 13 | 18 | -27.8% |
...
## Top 3 notable events
1. **WithdrawCollateral** — WithdrawCollateral 91,000.0000 wsrUSD in wsrUSD/USDC (≈ $100,325.79) · block 20,006,430 · [tx](https://www.pharosscan.xyz/tx/0x93bcfee1b462808327e4b16b1e76367b531b150bd788d51d62dd0701e54300b2)
2. **Repay** — Repay 89,365.0994 USDC in wsrUSD/USDC (≈ $89,365.10) · block 20,006,349 · [tx](https://www.pharosscan.xyz/tx/0x406bb94bdd740a284f04a4720c53c1569f918186d71cd6460d1c6ccda9ed4ad2)
3. **Withdraw** — Withdraw 52,861.5350 USDC in wsrUSD/USDC (≈ $52,861.54) · block 19,723,132 · [tx](https://www.pharosscan.xyz/tx/0xfe96c79f0ca10171b685f08db9334ef2dd13bc8a07b6c17eed10847c69205cb3)
```

  Morpho on Pharos is quiet: 13 transactions that week, no liquidations. Most of the −2.1 % in supplied USD is the
  WPROS-denominated BTCVC/WPROS market losing value with PROS (−16.5 %), not withdrawals.
* [`schedule_demo/`](schedule_demo/) — the scheduler really running with `--schedule --period 1h --runs 2`:
  [`schedule_log.txt`](schedule_demo/schedule_log.txt) shows it start, deliver the 20:00–21:00 UTC digest, sleep
  until the next hour boundary, wake at 22:00 UTC and deliver the 21:00–22:00 digest; both digests are in that folder.

## Limits

* Only Morpho Blue core is covered (not the Morpho vaults built on it); USDC = $1 is an assumption.
* The transaction list depends on the Pharosscan index; events are then read from the chain itself.
* Not financial advice.

## Security

Read-only: no wallet, private key, API key, bot token or account; nothing is signed or sent on chain.
HTTPS endpoints only.

License: MIT.
