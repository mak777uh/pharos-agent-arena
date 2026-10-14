# Pharos Agent Arena · Multi-Protocol Aggregator (one interface over Avalon, Morpho Blue and ERC-4626 vaults)

A Harbor-style DeFi agent extended across **three protocols on Pharos Pacific mainnet (chainId 1672)** —
**Avalon** (Aave-V3-fork lending pool), **Morpho Blue** (isolated lending markets) and **ERC-4626 vaults**
(SharpByte, Vishwa) — behind **one interface**. It answers queries (rates, positions) across all of them and
handles actions (deposit, withdraw) by **routing each request to the best venue, explaining why**, and preparing
the transactions **for the user's signature only**. If one protocol is unreachable, the others keep working.

Built for the Pharos *Season of AI · Agent Arena* task
[Multi-Protocol Aggregator](https://pharos-agent-arena.vercel.app/tasks/multi-protocol-aggregator).
Architecture write-up: **[`ARCHITECTURE.md`](ARCHITECTURE.md)**.

## Requirement map

| # | Requirement | How |
|---|---|---|
| 01 | ≥ 2 protocols or vaults behind one interface | 3 protocols / 12 USDC venues + 4 WPROS venues through one `Venue` adapter interface (`venues.py`) and one CLI (`aggregator.py rates · positions · deposit · withdraw · ask`). |
| 02 | queries (positions, rates) and actions (deposit / withdraw prepared for signature) | `rates`, `positions` read live state at one pinned block via the hosted **Pharos Chain MCP** `https://mcp.pharos.xyz/mcp`; `deposit` / `withdraw` emit **unsigned** transactions (JSON with `chainId`, `from`, `to`, `data`, `value`; `--prepare DIR` saves them) and simulate them with `eth_call` from the wallet where possible. |
| 03 | routes each request to the best venue and explains why | deposit → venue with the highest **APY expected after this deposit** among eligible venues (size ≥ $10k, cap room, APY known, not `--exclude`d); prints the winner's rate + how it's measured, the runner-up and the $/year difference, capacity, venue facts, and every rejected venue with its reason. withdraw → takes from the **lowest-APY** position first (best earners stay invested), caps each leg by what can leave now, skips legs whose simulation reverts, splits across venues and reports any shortfall. |
| 04 | one protocol unavailable doesn't break the others | each protocol / venue loads in its own `try/except`; failures are listed as `UNAVAILABLE — reason` in a protocol-status line and routing continues over the rest. Shown with the `--simulate-outage` drill (Avalon down for `rates`, vaults down for `deposit`) and with real per-venue failures (a vault whose `redeem` reverts is skipped during a withdrawal). |
| 05 | architecture write-up + demo video | [`ARCHITECTURE.md`](ARCHITECTURE.md) (diagram, components, routing rules, failure isolation, security model). **Demo video: not included in this repo** — `run_samples.sh` is the exact script to record. |

## Real runs (Pharos mainnet, 2026-10-14 ~22:45 UTC)

| Sample | Command | Result |
|---|---|---|
| [`sample_rates_usdc.txt`](sample_rates_usdc.txt) (+ `.json`) | `rates --token USDC` | 12 venues, best first: SharpByte USDC vault 5.52% · Morpho syzUSD/USDC 5.32% · Morpho WPROS/USDC 4.51% · Morpho wsrUSD/USDC 4.43% · SharpByte Pharos Prime 3.96% · … · Avalon 1.95%; status line `Avalon: up · Morpho Blue: up (9) · ERC-4626 vault: up (2)` |
| [`sample_positions.txt`](sample_positions.txt) | `positions --wallet 0xd27c…65c9 --token USDC --token WPROS` | USDC in Avalon (250,628), SharpByte USDC (676,101), SharpByte Pharos Prime (70,489) = 997,219 USDC; WPROS idle 1,999,990 + Vishwa vault 2,000,293; each figure with its contract + function + block |
| [`sample_deposit.txt`](sample_deposit.txt) | `ask 0x84db…072a "deposit 5000 USDC" --exclude syzUSD --prepare prepared` | parsed → deposit; **routed to SharpByte USDC Pharos Ecosystem** (5.50% after the deposit vs. 4.49% at the runner-up Morpho WPROS/USDC ≈ $50.84/year more); wallet holds 6,967 USDC; approve simulated ok; 2 unsigned txs saved in `prepared/` |
| [`sample_withdraw.txt`](sample_withdraw.txt) | `withdraw --wallet 0xd27c…65c9 --token USDC --amount 100000 --prepare prepared` | split: **Avalon** first (lowest APY 1.95%, only ~55k USDC withdrawable now), the rest from **SharpByte USDC**; SharpByte Pharos Prime skipped because its `redeem` simulation reverts; each leg simulated ok |
| [`sample_outage_rates.txt`](sample_outage_rates.txt) | `rates --token USDC --simulate-outage avalon` | Avalon reported `UNAVAILABLE`, Morpho + vault rates still listed |
| [`sample_outage_deposit.txt`](sample_outage_deposit.txt) | `deposit … --amount 5000 --exclude syzUSD --simulate-outage vault` | vaults `UNAVAILABLE` → routed to **Morpho Blue WPROS/USDC** (4.49% after deposit vs. 4.43% at wsrUSD/USDC), with reasons |
| [`sample_ask_unknown.txt`](sample_ask_unknown.txt) | `ask … "swap my USDC for ETH on a DEX"` | refused with the list of supported requests (no guessing) |

Wallets: [`0xd27cb53f…65c9`](https://www.pharosscan.xyz/address/0xd27cb53f5920d484261cc14d32094d3f043265c9) (positions in
all three protocols) and [`0x84dbd231…072a`](https://www.pharosscan.xyz/address/0x84dbd23178ba7fc6a2a396559139aef69d0f072a)
(idle USDC, used for deposits). Figures move every block; `run_samples.sh` regenerates everything.

## Run it

Python 3.10+ (tested with 3.12). A command takes ~30–90 s (the hosted MCP allows 120 req/min; vault APY needs a
7-day share-price lookup).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt            # requests, pycryptodome

python aggregator.py rates --token USDC
python aggregator.py positions --wallet 0xWALLET                        # USDC + WPROS by default
python aggregator.py deposit  --wallet 0xWALLET --token USDC --amount 5000 --prepare prepared/
python aggregator.py withdraw --wallet 0xWALLET --token USDC --amount all
python aggregator.py deposit  --wallet 0xWALLET --token USDC --amount 5000 --venue avalon   # force a venue
python aggregator.py ask 0xWALLET "withdraw 20000 USDC"
python aggregator.py rates --token USDC --simulate-outage morpho        # outage drill
```

Plain-English `ask` understands: *rates / APY / best place for <TOKEN>* · *my positions / holdings* ·
*deposit|supply|put <N> <TOKEN>* · *withdraw|redeem|take out <N|all> <TOKEN>*; anything else is refused. Tokens:
USDC, WPROS, WETH (Pharos token registry). Common options: `--exclude TEXT` (venue name, address or collateral
symbol), `--min-tvl-usd`, `--prepare DIR`, `--json FILE`, `--simulate-outage avalon|morpho|vault`, `--endpoint`.

## Files

| File | Role |
|---|---|
| `aggregator.py` | the interface: queries, router, plain-English parser, output |
| `venues.py` | adapters (Avalon reserve, Morpho Blue market, ERC-4626 vault), discovery with failure isolation, unsigned tx builders, `eth_call` simulation |
| `venues.json` | registry: 12 Morpho Blue market ids + 3 ERC-4626 vaults (Avalon discovered on chain); re-validated on chain every run |
| `pharos_mcp.py` / `pharos_explorer.py` | Chain MCP client (HTTPS only) / public Pharosscan index client (recent gas figures) |
| `ARCHITECTURE.md` | architecture write-up |
| `sample_*.txt`, `sample_rates_usdc.json`, `prepared/` | the real outputs above |

## Limits (honest)

* Routing is by yield (post-deposit APY) and capacity; collateral / LLTV / curator risk are shown, not scored —
  exclude venues you don't accept with `--exclude`.
* Post-deposit APY is a model (pool venues: utilisation drops, borrow rate held; vaults: pro-rata dilution).
* A deposit can't be simulated before its approve is mined (unless an allowance already exists — then the approve
  is skipped and the deposit itself is simulated). USDC counted as $1.00.
* Venue discovery = registry + on-chain Avalon discovery; not every vault on Pharos.
* The outage samples use a **drill flag** that makes an adapter raise; the network was healthy during the runs.
* No demo video in the repo (see requirement 05).

## Safety

Read + prepare only: no private key, no signing, no `send_raw_transaction`, no spending. HTTPS endpoints; wallet
and token addresses validated; amounts must be positive; unknown requests refused.

## Proof

Demo wallet with positions in all three protocols (the one the positions / withdraw samples read):
https://www.pharosscan.xyz/address/0xd27cb53f5920d484261cc14d32094d3f043265c9

License: MIT.
