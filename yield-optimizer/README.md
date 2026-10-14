# Pharos Agent Arena · Yield Optimizer (compare live yields → "move" with net gain, or "stay put" with the reason)

An agent that compares **every yield source for a token on Pharos Pacific mainnet (chainId 1672)** — the Avalon
lending reserve, each Morpho Blue market that lends the token, and ERC-4626 vaults — using live on-chain data,
nets out **gas, entry/exit fees and a minimum holding period**, and then says either **MOVE / DEPOSIT** (with the
expected net gain) or **STAY PUT** (with the reason). If asked, it **prepares** the rebalance as unsigned
transactions for the user to sign. It never holds a key, never signs, never broadcasts.

Built for the Pharos *Season of AI · Agent Arena* task
[Yield Optimizer](https://pharos-agent-arena.vercel.app/tasks/yield-optimizer).

## Requirement map

| # | Requirement | How |
|---|---|---|
| 01 | ≥ 3 yield sources, live on-chain data | USDC run compares **12 venues across 3 protocols** at one pinned block (Avalon USDC reserve, 9 Morpho Blue USDC markets, SharpByte USDC Pharos Ecosystem + SharpByte Pharos Prime vaults); WPROS run compares 4 (Vishwa vault + 3 Morpho markets). All reads via the hosted **Pharos Chain MCP** `https://mcp.pharos.xyz/mcp`. |
| 02 | gas, entry/exit fees, minimum holding period | **gas** for withdraw + approve + deposit: `estimate_gas` from the wallet where the call can be simulated, else the median `gasUsed` of recent real txs with the same method on that contract (public Pharosscan index), else a labelled default; priced with `get_gas_price` × Chainlink PROS/USD. **fees**: ERC-4626 entry = 1 − `previewDeposit`/`convertToShares`, exit = 1 − `previewRedeem`/`convertToAssets` (with the real amount); Avalon/Morpho have no deposit/withdraw fee (reserve factor / market fee are already inside the rate and shown). **holding period**: `--hold-days` (default 30) — the extra yield is only counted over that period and costs must be earned back within it; plus `--min-age-days` (default 7) — a position topped up more recently than that is not moved (anti-churn; last deposit found from the wallet's token transfers and linked). |
| 03 | clear recommendation with expected net gain, or "stay put" + reason | per position: `⇒ MOVE: move X to V: a% after your deposit vs b% now → +$gain over N days, costs $c, net $n, break-even in t`; or `⇒ STAY PUT: <every failed rule, with numbers>` |
| 04 | if executing: prepare for user signature only | `--prepare DIR` writes the withdraw → approve → deposit transactions as **unsigned** JSON (`chainId`, `from`, `to`, `data`, `value`), simulates the ones that can be simulated (`eth_call` from the wallet) and says which can't. No nonce/gas fields are filled; the user's wallet does that. |

## How it decides

For each position the wallet has in a venue (or an idle `--amount` in the wallet):

1. **Exit check** — the withdrawal is built and simulated with `eth_call` from the wallet at the pinned block. Pool
   venues are also checked against cash on hand (Avalon: USDC held by the aToken; Morpho: supply − borrow); if only
   part can leave now, a **partial move** of 99% of the available cash is evaluated.
2. **Every other eligible venue** (APY known, size ≥ `--min-tvl-usd` $10k, deposit cap room, not `--exclude`d):
   * **post-deposit APY**, not the headline one — pool venues: supply APY = borrow APY × borrowed/(supplied +
     your deposit) × (1 − fee) (borrow rate held constant, so slightly optimistic); vaults: the 7-day realised
     share-price APY diluted pro rata by your deposit (conservative);
   * gain over the holding period = amount × (post-deposit APY − current APY) × hold-days/365;
   * costs = gas (3 txs) + entry/exit fees.
3. **MOVE** only if, for the best candidate: APY gap ≥ `--min-apy-diff` (0.25 pts), net gain over the holding
   period ≥ `--min-gain-usd` ($5), break-even ≤ holding period, the exit simulation passes, and the current
   position is older than `--min-age-days`. Otherwise **STAY PUT**, listing every rule that failed.

Rates per venue: Avalon = `getReserveData().currentLiquidityRate` (APR → APY); Morpho Blue = IRM
`borrowRateView()` → borrow APY × utilisation × (1 − fee); ERC-4626 = `convertToAssets(1 share)` now vs. 7 days
ago (binary search on block timestamps), annualised — already net of the vault's performance/management fee,
which is read and shown when it is WAD-scaled.

## Real runs (Pharos mainnet, 2026-10-14 ~22:30 UTC)

Demo wallet [`0xd27cb53f…65c9`](https://www.pharosscan.xyz/address/0xd27cb53f5920d484261cc14d32094d3f043265c9)
(has USDC in Avalon and two SharpByte vaults, WPROS in the Vishwa vault):

* [`sample_wallet_usdc.txt`](sample_wallet_usdc.txt) (block #20,256,702, `--prepare prepared`, JSON:
  [`sample_wallet_usdc.json`](sample_wallet_usdc.json)):
  * **Avalon USDC** 250,628 USDC at 1.95% — only 55,016 USDC is withdrawable (the reserve is 89% lent out) →
    **MOVE 54,465.77 USDC to SharpByte USDC Pharos Ecosystem** (5.52% now, **5.38% after the deposit**) →
    +$153.50 over 30 days, gas $0.0028 (483k gas at 10 gwei), fees 0 bps, break-even < 1 minute. Prepared:
    [`prepared/move_1_USDC_0x15c2_aff1.json`](prepared/move_1_USDC_0x15c2_aff1.json) — Avalon `withdraw` (simulated
    ok), USDC `approve` (simulated ok), vault `deposit` (not simulable before the approve is mined).
  * **SharpByte USDC** 676,101 USDC at 5.52% → **STAY PUT**: the best alternative would pay only 4.44% once 676k
    lands in it (gap −1.08 pts; −$602 over 30 days).
  * **SharpByte Pharos Prime** 70,489 USDC at 3.98% → **STAY PUT**: a better venue exists (+$79/30 d), but
    `redeem` reverts right now ("ERC20: transfer amount exceeds balance" — the vault holds 0 idle USDC).
* [`sample_short_hold.txt`](sample_short_hold.txt) — same wallet, `--hold-days 3 --min-gain-usd 25`: the Avalon
  move now earns only $15.35 within the holding period → **STAY PUT** (holding-period rule in action).
* [`sample_wpros.txt`](sample_wpros.txt) — WPROS, 4 venues: Vishwa vault 0.16% vs. best Morpho WPROS market
  0.09% after deposit → **STAY PUT** (and the Vishwa redeem simulation reverts).
* [`sample_idle_usdc.txt`](sample_idle_usdc.txt) — 25,000 idle USDC, no wallet, `--exclude syzUSD` →
  **DEPOSIT** into SharpByte USDC Pharos Ecosystem (5.45% after deposit, +$112.02 over 30 days).

`run_samples.sh` regenerates all four. Figures change every block.

## Run it

Python 3.10+ (tested with 3.12). Each run takes 1–2 minutes (hosted MCP limit 120 req/min).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt            # requests, pycryptodome (keccak for selectors)

python yield_optimizer.py --token USDC --wallet 0xWALLET
python yield_optimizer.py --token USDC --wallet 0xWALLET --prepare prepared/ --json result.json
python yield_optimizer.py --token USDC --amount 25000 --exclude syzUSD --hold-days 60
python yield_optimizer.py --token WPROS --wallet 0xWALLET --min-apy-diff 0.5 --min-gain-usd 20
```

Options: `--token` (USDC, WPROS, WETH or an address), `--wallet`, `--amount`, `--hold-days`, `--min-age-days`,
`--min-gain-usd`, `--min-apy-diff`, `--min-tvl-usd`, `--exclude TEXT` (repeatable: venue name, address or a
collateral symbol), `--vault 0x…` (extra ERC-4626 vault; validated on chain with `asset()`), `--prepare DIR`,
`--json FILE`, `--endpoint`.

## Files

| File | Role |
|---|---|
| `yield_optimizer.py` | CLI: compare → cost → decide → (prepare) |
| `venues.py` | venue adapters (Avalon reserve, Morpho Blue market, ERC-4626 vault): rate, size, exit liquidity, deposit room, fees, unsigned tx builders, `eth_call` simulation |
| `venues.json` | registry: 12 Morpho Blue market ids + 3 ERC-4626 vaults (Avalon is discovered on chain); every entry is re-validated on chain |
| `pharos_mcp.py` / `pharos_explorer.py` | Chain MCP client (HTTPS only) / public Pharosscan index client (recent gas, last deposit) |
| `sample_*.txt`, `sample_wallet_usdc.json`, `prepared/` | real outputs above |

## Limits (honest)

* Yield only: the ranking does not price collateral, LLTV, oracle or curator risk — those facts are printed per
  venue, and `--exclude` removes venues you don't accept (e.g. the idle-USDC sample excludes the syzUSD market).
* Post-deposit APY is a model (see above); vault APY is the past 7 days and may not persist.
* USDC is counted as $1.00; gas is cheap on Pharos (≈ $0.003 for three txs), so the holding-period gain and the
  post-deposit rate drive most decisions.
* The deposit step can't be simulated before the approve is mined; its gas comes from recent real deposits.
* Venue discovery is a registry + on-chain Avalon discovery, not a full scan of every vault on Pharos; add vaults
  with `--vault`.

## Safety

Read-only + prepare-only: no private key, no signing, no `send_raw_transaction`, no spending. HTTPS endpoints only;
wallet / token / vault addresses validated; numeric options range-checked.

## Proof

* Live position the MOVE recommendation is about (the wallet's Avalon USDC deposit, found by the agent):
  https://www.pharosscan.xyz/tx/0x53a76ba436464b29be42c7d6c82b05c72d5fe2f9178fdfddd81543818c5973a8
* Demo wallet: https://www.pharosscan.xyz/address/0xd27cb53f5920d484261cc14d32094d3f043265c9

License: MIT.
