# Pharos Agent Arena · Arbitrage Scanner (SkillEngine skill, Chain MCP, prepare-only)

A Skill that compares the **WPROS/USDC price across every Uniswap-V3-style pool on Pharos mainnet**
(chainId 1672; fee tiers 0.01 %, 0.05 %, 0.3 %, 1 %, discovered on chain from the factory) **plus the Chainlink
PROS/USD feed** as a reference — all read at the **same block** — nets out **swap fees, slippage and gas**, and
**prepares a transaction only when the round trip is still profitable after costs** and clears a configurable
threshold. Otherwise it logs why it skipped. Every scan is logged.

It never signs and never broadcasts. A prepared opportunity is ONE atomic unsigned `router.exactInput`
(USDC → pool A → WPROS → pool B → USDC) whose `amountOutMinimum` makes it revert instead of losing on the swap
legs; its execution is **simulated on a local anvil fork** of mainnet (wallet impersonated, no key).

Built for the Pharos *Season of AI · Agent Arena* task
[Arbitrage Scanner](https://pharos-agent-arena.vercel.app/tasks/arbitrage-scanner).

## Requirements → where they are met

| # | Requirement | How |
|---|---|---|
| 1 | Reads prices from ≥ 2 sources in the same scan | 4 pools (`slot0` + `liquidity`, and the `tickBitmap`/`ticks` a quote crosses) + Chainlink `latestRoundData`, every `eth_call` pinned to one block number, through the hosted **Pharos Chain MCP**. Each pool's state is cross-checked against the official RPC (`assets/networks.json`) at the same block, because the hosted MCP has been seen returning a stale archive `slot0` (see Limits). |
| 2 | Net profit after swap fees, slippage estimate and gas | `scripts/v3math.py`: exact Uniswap-V3 swap simulation with tick traversal (SwapMath/SqrtPriceMath/TickBitmap port). Per route and size: gross (mid-price spread), fees actually charged per step, slippage = price impact of both legs, gas = units × (base fee + tip) × Chainlink PROS/USD, **net**; best size by grid + golden-section search, capped by the wallet's USDC. |
| 3 | Acts (prepares the tx) only above a configurable threshold, otherwise logs why | `assets/arb.json → thresholds` ($1.00 and 5 bps) or `--threshold-usd/--threshold-bps`. PREPARE → unsigned approve (if needed) + atomic `exactInput`; SKIP → every failed condition named ("gross spread 21.63 bps <= swap-fee floor 31.00 bps", "best net $0.0003 < threshold $1.0", "slippage + gas eat it at every size"). |
| 4 | Logs every scan with gross spread, costs, net result and decision | console table (all routes) + one JSONL line per scan in `runs/scans.jsonl` / `runs/backscan.jsonl` (`grossBps, grossUSD, feesUSD, slippageUSD, gasUSD, netUSD, netBps, decision, reason`); `--json-out` = full record. |
| 5 | Works safely (testnet or simulated execution), no real funds | Mainnet is only read. `--simulate` executes the prepared approve + exactInput on an anvil fork pinned to the scan block; `fork-scenario` creates a dislocation on a fork with a whale trade and runs the whole loop there. Predicted vs realized profit matched to the wei in both samples. |

## Real runs (15 Oct 2026)

| sample | what | result |
|---|---|---|
| `01_live_scans_mainnet` | 3 live scans, blocks 20,263,148 / 20,263,266 / 20,263,421, default threshold | SKIP ×3: best route buy 0.30 % → sell 0.01 %, gross 30.15 / 26.69 / 21.63 bps < fee floor 31 bps; net −$0.0027 / −$0.0062 / −$0.0112 |
| `02_backscan_mainnet_archive` | archive scans every 2,000 blocks, blocks 20,250,500–20,262,500 | 6 blocks scanned, all SKIP (gross 4.56–31.08 bps, best net −$0.0018); 2 stale Chain MCP `slot0` reads caught and corrected by re-read; block 20,260,500 failed with a read error (DivisionByZero) and is reported as such, not hidden |
| `04_block_20262738_before_real_arb_tx` | scan of block 20,262,738 with threshold 0 + `--simulate` | best route **buy 0.30 % → sell 0.01 %**, gross 36.24 bps, optimal size 42.09 USDC: fees $0.1305, slippage $0.0113, gas $0.0012 → **net +$0.0096** → PREPARE (threshold 0); fork execution: **+0.010804 USDC realized = predicted**, gas 199,708. In the very next block a real bot executed exactly this route: [0x18ec1f2f…](https://www.pharosscan.xyz/tx/0x18ec1f2fefc3d9a5c37090b517b81eb846628d780e5c12d42d812eb33782650f) (26.32 USDC in on the 0.3 % pool, WPROS sold on the 0.01 % pool, block 20,262,739). With the default $1 threshold this is a SKIP — correctly, it is worth one cent. |
| `05_mcp_archive_consistency_check` | `scripts/mcp_consistency.py`: 15 Chain MCP reads vs the official RPC at block 20,255,000 | 0/15 differed this time (see Limits) |
| `03_fork_scenario_whale_prepare_simulate` | fork at block 20,262,798; 1,500 USDC whale buy on the 0.3 % pool (real USDC holder impersonated on the fork) | before: SKIP (24.72 bps < 31). After: 0.3 % pool +1.3 % → buy 0.01 % → sell 0.3 %, 637.48 USDC, **net +$2.48 (38.9 bps) → PREPARE**; unsigned approve + exactInput printed; fork execution **+2.483154 USDC = predicted**, gas 199,611. The unsigned txs there only show the format (the opportunity exists on the fork only). |

The thin 0.05 % pool
always shows a large gross spread (its price sits ~1 % off) but has almost no in-range liquidity: $10 through it
loses ~$9 to slippage — a good example of why spot spreads alone are misleading.

Proof links: pools [0.01 %](https://www.pharosscan.xyz/address/0x912c9ade24d44d8922f0866d8dcb079f1363f647) ·
[0.05 %](https://www.pharosscan.xyz/address/0x9e1ec11d25f65ae5e7f9aaaa6f73b41eb4df651d) ·
[0.3 %](https://www.pharosscan.xyz/address/0x4146d192da6428c9e1c243d2a953c625b5765623) ·
[1 %](https://www.pharosscan.xyz/address/0xfc5f8974a7e94504f8d3ac20fbdc67dadff75049) · real arbitrage tx on the
route the scanner found: [0x18ec1f2f…](https://www.pharosscan.xyz/tx/0x18ec1f2fefc3d9a5c37090b517b81eb846628d780e5c12d42d812eb33782650f).

## Usage

```bash
pip install -r requirements.txt               # anvil (Foundry) only needed for --simulate / fork-scenario
python3 scripts/arb.py scan                                      # one live scan
python3 scripts/arb.py scan --count 10 --interval 30             # watch
python3 scripts/arb.py scan --threshold-usd 2 --threshold-bps 10
python3 scripts/arb.py scan --block 20262738 --threshold-usd 0 --threshold-bps 0 --simulate
python3 scripts/arb.py backscan --from 20250000 --to 20262000 --step 1500
python3 scripts/arb.py fork-scenario --whale-usdc 1500 --pool-fee 3000
python3 -m unittest discover -s tests          # offline tests (V3 math vs closed form, tick crossing, cost identity)
```

## Skill format

```
SKILL.md                 agent entry point (official pharos-skill-engine frontmatter)
references/scanner.md    sources, math, branch-and-bound over pairs, decision, logging, safety
assets/networks.json     official pharos-skill-engine network file (unchanged copy)
assets/arb.json          tokens, factory, router, fee tiers, oracle, wallet, size grid, thresholds, buffer, gas units
scripts/arb.py           the skill (scan / backscan / fork-scenario)
scripts/mcp_consistency.py  evidence helper: Chain MCP vs official RPC for one archive read
scripts/v3math.py        exact V3 exact-input simulation with tick traversal
scripts/skill_chain.py, pay_core.py, pharos_mcp.py   shared Chain MCP client, unsigned EIP-1559
runs/                    JSONL scan logs of the sample runs
samples/                 real outputs (.txt) and full records (.json)
```

## Limits (honest)

- One token pair (WPROS/USDC) on one DEX family (the pools of factory `0x2c90…552b`); Pharos has no second
  WPROS/USDC venue with liquidity that I found. Chainlink is a reference, not a tradable leg.
- **Hosted Chain MCP archive reads are occasionally stale/inconsistent**: during development an `eth_call` at block
  20,262,739 returned the 0.01 % pool at 0.585741 USDC while the official RPC (and the pool's own Swap events) said
  0.591180, and a manual re-read returned tick −280,726 (0.6439) once before three correct answers; an earlier
  backscan also hit a Chain-MCP-vs-RPC disagreement for the 0.3 % pool at block 20,253,000. It is intermittent —
  `scripts/mcp_consistency.py` (sample 05) got 15/15 consistent answers when re-run — so no sample captures it.
  The scanner therefore reads every pool a second time through the official RPC at the same block; on a mismatch
  it re-reads both, uses an answer confirmed by two reads, and otherwise **excludes the pool from that scan**
  (noted in the output). The Chainlink deviation guard (5 %) is an extra backstop, and the fork simulation stops a
  stale case too (that development run reverted with "Too little received" in simulation, nothing prepared).
- Gas uses `gas_units_fallback` 200,000 (2-hop exactInput measured 198,569–199,708 gas on forks); the fork
  simulation reports the exact figure. Gas is ~$0.001 here, so fees and slippage decide everything.
- `getSqrtRatioAtTick` is computed with Decimal instead of Uniswap's bit table: ≤ 18 wei of WPROS difference
  after a tick crossing, identical in range (checked against the router on a fork).
- Opportunities on mainnet right now are cents, not dollars (fee floor 31 bps between the two deep pools); real
  bots take them within a block (see the tx above). The skill reports that honestly instead of acting.

## License

MIT
