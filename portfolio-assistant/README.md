# Pharos Agent Arena · Portfolio Assistant (natural-language questions → answers from Pharos mainnet)

Ask plain-English questions about any wallet on **Pharos Pacific mainnet (chainId 1672)** — *"what am I earning?"*,
*"where is my USDC?"*, *"what's my biggest position?"* — and get answers computed from a single on-chain snapshot,
with **the contract, function and block behind every figure**. It reads wallet balances plus positions in
**Morpho Blue, Avalon (Aave-V3-style lending) and ERC-4626 vaults** (Morpho Vault V2 vaults such as SharpByte /
Vishwa), says plainly what it cannot read or price, and never builds, signs or sends a transaction.

Built for the Pharos *Season of AI · Agent Arena* task
[Portfolio Assistant](https://pharos-agent-arena.vercel.app/tasks/portfolio-assistant).

## What it reads (requirement 01)

One snapshot: every `eth_call` is pinned to the same block (the latest block when the run starts), via the hosted
**Pharos Chain MCP** `https://mcp.pharos.xyz/mcp` (read-only, no key).

| Source | Contracts / calls | Gives |
|---|---|---|
| **Wallet** | `get_balance` (native PROS); `balanceOf(wallet)` for the [Pharos token registry](https://docs.pharos.xyz/getting-started/token-registry) mainnet tokens (WPROS `0x52c4…b0b0`, USDC `0xc879…1815`, LINK `0x51e2…BE29`, WETH `0x1f4b…56E9`) **and** every other ERC-20 the wallet ever received (token list from the public Pharosscan explorer index) | token balances |
| **Morpho Blue** [`0x18573fa1…4efb`](https://www.pharosscan.xyz/address/0x18573fa18fd17ddfd790b4a5b5b2977aad3b4efb) | `position(id, wallet)`, `market(id)`, `idToMarketParams(id)` for all 12 known markets ([`known_markets.json`](known_markets.json) + fresh `CreateMarket` discovery); IRM `borrowRateView()`; market oracle `price()` | supplied / borrowed (Morpho share math with virtual shares) / collateral; borrow APY = e^(rate·year) − 1, supply APY = borrow APY × utilization × (1 − fee); health factor = collateral × price × LLTV / debt |
| **Avalon** lending pool [`0xd9b9e4f8…0cba`](https://www.pharosscan.xyz/address/0xd9b9e4f8257bff9b1415d74c1ef88d8057f60cba) (found via `POOL()` of the "Avalon USDC" aToken [`0xe2ec2f39…87d`](https://www.pharosscan.xyz/address/0xe2ec2f3933ad2f83de9a9dda844558354384e87d)) | `getReservesList()`, `getReserveData(asset)` (Aave V3 layout: aToken, variable-debt token, `currentLiquidityRate`, `currentVariableBorrowRate`), `balanceOf(wallet)` on aToken / debt token, `getUserAccountData(wallet)` (HF when there is Avalon debt), pool oracle `getAssetPrice()` | supplied / borrowed per reserve, supply / borrow APY (from the ray APR) |
| **ERC-4626 vaults** | any wallet token answering `asset()` + `convertToAssets()`: `balanceOf(wallet)`, `convertToAssets(shares)`, and `convertToAssets(1 share)` now and at the block 7 days earlier (binary search on `get_block` timestamps) | value in the underlying token; yield = 7-day share-price growth, annualised |

Prices: USDC = $1.00 (stated assumption), WPROS / PROS = Chainlink PROS/USD
[`0x371ed36a…5466`](https://www.pharosscan.xyz/address/0x371ed36aee5bffbed1a2aa9b7ded7f4eb30f5466) `latestRoundData()`
(`decimals()` read on chain), Avalon reserves = Avalon's own oracle, Morpho collateral tokens = that market's Morpho
oracle × loan-token price. A token with none of these has **no price** and is left out of USD totals — and the
answer says so.

## Questions it answers (requirement 02)

A small rule-based intent parser (regular expressions, no LLM, no guessing) maps a question to one of 9 types:

| Type | Example phrasings | Answer |
|---|---|---|
| `earning` | what am I earning? · what's my yield / APY? | every yield position with APY and ≈ $/year, minus borrow interest → net |
| `where_token` | where is my USDC? · where are my WPROS? | every place that token sits (wallet, vaults, lending, collateral, debt) |
| `how_much_token` | how much WPROS do I have? | same as above |
| `biggest` | what's my biggest position? · top holdings | top 3 by USD with % of priced assets |
| `networth` | what's my net worth? · total portfolio value | assets − debts, by protocol |
| `debt` | how much do I owe? · my loans | debts with borrow APY and health factor |
| `health` | am I at risk of liquidation? · health factor | HF per Morpho loan (+ Avalon HF), state, % price drop to liquidation |
| `wallet` | what's in my wallet? · balances | idle tokens with USD value or "no price" |
| `protocols` | which protocols am I in? | protocols with positions + what was checked + what is not covered |

## Sources on every figure (requirement 03)

Each answer ends with `Sources:` — one line per figure used: `<contract> <function> @ block <N>` (e.g.
`0x18573fa1… position(0xb3b8…e1ba, wallet) @ block 20,255,054`) plus the price source. `--json` writes the full
snapshot (every position with its sources, market ids, prices) next to the answers.

## When it doesn't know (requirement 04)

* questions it has no rule for → *"I don't know how to answer that from chain data, so I won't guess"* + the list of
  questions it can answer;
* predictions / advice (*"will"*, *"next year"*, *"should I"*, …) → refused: it reports only what is on chain now;
* tokens without a trusted price → shown as **no price**, excluded from totals, listed under
  *"What I could NOT read or value"*;
* a vault whose `convertToAssets()` fails, an Avalon pool that cannot be read, explorer downtime → named in the
  same list; protocols it does not read (e.g. DEX LP tokens such as UNI-V2 are shown as plain tokens, not
  decomposed) are stated in the `protocols` answer.

## Read-only (requirement 05)

Only `eth_call`, `get_balance`, `get_block`, `get_transaction_receipt` and `get_block_number` are used. No private
key, no wallet connection, no transaction building; the code never calls `send_raw_transaction` or `estimate_gas`.

## Run it

Python 3.10+ (tested with Python 3.12).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt                   # requests, pycryptodome (keccak for selectors)

python portfolio_assistant.py 0xd27cb53f5920d484261cc14d32094d3f043265c9 "what am I earning?" "where is my USDC?"
python portfolio_assistant.py 0xWALLET                    # interactive: type questions, empty line to quit
python portfolio_assistant.py 0xWALLET "how much do I owe?" --json answers.json
```

The address is validated (`0x` + 40 hex), questions must be 1–300 characters, market ids in `--markets` must be
`0x` + 64 hex; the MCP endpoint must be `https://` (or `http://` to localhost). Building the snapshot takes about
1–2 minutes (≈ 100–200 read calls; the hosted MCP allows 120 per minute); all questions are then answered from it.

## Sample output (real runs on Pharos mainnet, 2026-10-14 ~22:00 UTC)

[`sample_output.txt`](sample_output.txt) (+ [`sample_answers.json`](sample_answers.json)) — wallet
[`0xd27cb53f…65c9`](https://www.pharosscan.xyz/address/0xd27cb53f5920d484261cc14d32094d3f043265c9) at block
20,255,054, positions in **Morpho Blue, Avalon and 3 ERC-4626 vaults**, 10 questions (8 types + one prediction
and one off-topic question, both declined):

```
Q: what am I earning?
[earning] You are earning about $46,976.87 per year (≈ $128.70/day) at current rates, and paying about $49,473.87 per year in borrow interest → net ≈ −$2,497.00/year.
• 2,000,292.9454 WPROS ($1,171,410.69) — deposited in Vault Vishwa WPROS Vault · vishwaWPROS, 7-day share-price APY 0.16% → ≈ $1,861.91/year
• 676,100.6017 USDC ($676,100.60) — deposited in Vault SharpByte USDC Pharos Ecosystem · sbUSDC-ph-eco, 7-day share-price APY 5.53% → ≈ $37,403.15/year
• 250,627.9732 USDC ($250,627.97) — supplied (earning) in Avalon · USDC, supply APY 1.95% → ≈ $4,899.72/year
• 70,488.7785 USDC ($70,488.78) — deposited in Vault SharpByte Pharos Prime · sbPharosPrime, 7-day share-price APY 3.99% → ≈ $2,812.09/year
• cost: 999,558.8500 USDC ($999,558.85) — BORROWED (owed) in Morpho Blue · WPROS/USDC, borrow APY 4.95% → ≈ −$49,473.87/year

Q: am I at risk of liquidation?
[health] Liquidation risk (health factor < 1.0 = can be liquidated):
• Morpho WPROS/USDC: health factor 2.594 (healthy); liquidation if the WPROS price falls 61.4% (LLTV 38.5%)
Sources:
  - 0x18573fa18fd17ddfd790b4a5b5b2977aad3b4efb position(0xb3b8…e1ba, wallet) @ block 20,255,054
  - 0x18573fa18fd17ddfd790b4a5b5b2977aad3b4efb market(0xb3b8…e1ba) @ block 20,255,054
  - 0xd5e02889c13230458506cc842347c4e62f8cdf3a borrowRateView() @ block 20,255,054
  - 0xf4331ff581c31d897da6a3cd552dc39bd44983a5 price() @ block 20,255,054
  ...
Q: what will PROS be worth next year?
[out_of_scope] I only report what is on chain right now; I can't predict prices or future yields and I don't give buy/sell advice, so I won't guess.
```

The Avalon aToken in the wallet is recognised as the Avalon supply position (not counted twice). The same
borrower shows HF 2.587 in our separate liquidation-watchdog run ~1.5 h earlier — consistent.

[`sample_output_defi_wallet.txt`](sample_output_defi_wallet.txt) (+ [`sample_answers_defi_wallet.json`](sample_answers_defi_wallet.json)) —
an active DeFi wallet [`0x1f735797…528c`](https://www.pharosscan.xyz/address/0x1f735797c030e7780f803706c5ed8e2b269a528c):
2 Morpho loans, 3 vaults, and four tokens it **cannot price** (VWTVS, CFVP, BTCVP, a UNI-V2 LP token) — each shown as
"no price" and listed as not valued; plus an invalid-address error.

## Limits / honesty notes

* Vault values are what each vault's own `convertToAssets()` reports; some vaults (e.g. Vishwa WPROS
  `0x6ce7a0c9…`) are unverified contracts on Pharosscan, so their numbers are self-reported.
* Vault APY is backward-looking (last 7 days); lending APYs are the current rates and move every block.
* Not covered: DEX LP positions (UNI-V2 / V3), other lenders, staking. Morpho Vault V2s are covered as ERC-4626
  vaults when the wallet holds their shares.
* The parser understands the phrasings in the table, not arbitrary English; anything else is declined, not guessed.
* Not financial advice.

## Security

Read-only; no wallet, private key, API key or account. HTTPS endpoints only.

License: MIT.
