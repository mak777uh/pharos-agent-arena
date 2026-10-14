# Pharos Agent Arena · Dry-Run Executor (SkillEngine skill, Chain MCP, prepare-only)

A Skill that **simulates a transaction before it is sent** on Pharos Pacific mainnet (chainId 1672) and gives a
go / no-go:

1. **Simulate** through the hosted **Pharos Chain MCP** (`https://mcp.pharos.xyz/mcp`): `eth_call` → success or
   revert with the **decoded revert reason**; `estimate_gas` → gas; fees from `get_gas_price` + base fee →
   expected and worst-case cost in PROS and USD (Chainlink PROS/USD).
2. **Predict the outcome**: `debug_traceCall` (callTracer with logs) on the official Pharos RPC from
   `assets/networks.json` (`https://rpc.pharos.xyz`) returns the events the tx *would* emit; ERC-20 `Transfer`, WPROS
   `Deposit`/`Withdrawal` and native value moves become a **before → after balance table**, with "before" read
   through Chain MCP at the same head block.
3. **Gate**: if the simulation reverts, or the sender cannot pay value + worst-case fee, execution is **BLOCKED
   automatically** (exit 4, nothing prepared). Otherwise **GO**: an **unsigned** EIP-1559 transaction is prepared.

It never signs, never broadcasts and never touches a private key; signing is left to the user (EIP-1193 request,
or the `cast send … --private-key $PRIVATE_KEY` line for the official
[pharos-skill-engine](https://github.com/PharosNetwork/pharos-skill-engine), run by the user).

Built for the Pharos *Season of AI · Agent Arena* task
[Dry-Run Executor](https://pharos-agent-arena.vercel.app/tasks/dry-run-executor).

## Requirements → where they are met

| # | Requirement | How |
|---|---|---|
| 1 | Uses a call / gas estimation to simulate without broadcasting | Chain MCP `eth_call` + `estimate_gas` on every run (+ `debug_traceCall` for the state prediction); no send path exists in the code. |
| 2 | Predicted success/revert incl. revert reason, and estimated gas cost | `result : SUCCESS` / `REVERT - <reason>` (`Error(string)`, `Panic`, custom errors, or "reverted without a reason"); gas estimate, gas limit, expected fee and worst-case fee in PROS, expected fee in USD. |
| 3 | Expected outcome, balances before/after | event list + table account / token / before / change / after (samples 01, 03, 05, 08) — incl. a real router swap with both pool and wallet balances. |
| 4 | Blocks execution automatically if the simulation fails | `[3] Decision: BLOCKED … no transaction was prepared`, exit 4 (samples 02, 04, 06, 07, and 08 where the simulation passes but PROS for gas is short). |

## Skill format

```
SKILL.md                  agent entry point (official pharos-skill-engine frontmatter)
references/dry-run.md     build → simulate → cost → predict → gate, in detail
assets/networks.json      official pharos-skill-engine network file (unchanged copy)
scripts/dryrun.py         the skill (CLI)
scripts/skill_chain.py    simulate(), trace_call(), decode_revert(), prepare_tx(), readers
scripts/pay_core.py, pharos_mcp.py
samples/  tests/
```

## Run it

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 scripts/dryrun.py transfer --from 0x84dbd23178ba7fc6a2a396559139aef69d0f072a --to 0x1f3546a29eec2102c8d52e21a45114124014c85c --token USDC --amount 100
python3 scripts/dryrun.py swap --from 0x1f3546a29eec2102c8d52e21a45114124014c85c --sell USDC --amount 10 --min-out 18
python3 scripts/dryrun.py --json wrap --from 0x84dbd23178ba7fc6a2a396559139aef69d0f072a --amount 1
python3 -m unittest discover -s tests
```

## Real sample output (Pharos mainnet, 2026-10-14 22:43–22:46 UTC)

| sample | action | result | decision |
|---|---|---|---|
| 01 | transfer 100 USDC `0x84db…` → `0x1f35…` | SUCCESS, gas 58,620, ~0.000586 PROS | GO (unsigned tx prepared) |
| 02 | transfer 10,000 USDC (sender has ~6,967) | REVERT `ERC20: transfer amount exceeds balance` | BLOCKED |
| 03 | swap 10 USDC → WPROS via router (sender `0x1f35…`) | SUCCESS, +17.151459 WPROS, gas 162k | GO |
| 04 | same swap with `--min-out 18` | REVERT `Too little received` | BLOCKED |
| 05 | wrap 1 PROS | SUCCESS, `Deposit`, +1 WPROS / −1.000576 PROS | GO |
| 06 | unwrap 1 WPROS (sender has 0) | REVERT, no reason (empty revert data) | BLOCKED |
| 07 | raw `transferFrom` without allowance | REVERT `ERC20: transfer amount exceeds allowance` | BLOCKED |
| 08 | wrap the whole PROS balance minus 0.0001 | SUCCESS, but value + max fee 7.139678 > balance 7.138397 | BLOCKED |
| 09 | sample 01 as `--json` | | |

`samples/03_go_swap_10_usdc.txt` (excerpt):

```
Dry run · swap 10 USDC -> WPROS via router 0xf38d34c8382b9079b0f85309578b43b8479cd875 (pool fee 0.01%, amountOutMinimum 0, deadline +10 min)
[1] Simulation (Chain MCP eth_call + estimate_gas, nothing broadcast)
  result : SUCCESS, return data 0x000000000000000000000000000000000000000000000000ee0638a2cd04f25c
  cost   : expected 0.00162377 PROS (~$0.000948 at Chainlink PROS/USD 0.5840); worst case 0.00389704 PROS
[2] Expected outcome (debug_traceCall on https://rpc.pharos.xyz + balances via Chain MCP)
  event  : Swap               0x912c9ade24d44d8922f0866d8dcb079f1363f647 amount0 -17151458502762623580 amount1 10000000 tick-> -281721
  event  : Transfer           0x52c48d42… 0x912c9ade… -> 0x1f3546a2… value 17151458502762623580
  event  : Transfer           0xc879c018… 0x1f3546a2… -> 0x912c9ade… value 10000000
  account                                      token                before             change                after
  0x1f3546a29eec2102c8d52e21a45114124014c85c   WPROS                   300         +17.151459           317.151459
  0x1f3546a29eec2102c8d52e21a45114124014c85c   USDC           1,867.834083                -10         1,857.834083
  0x1f3546a29eec2102c8d52e21a45114124014c85c   PROS              34.691068          -0.001624            34.689445
  0x912c9ade24d44d8922f0866d8dcb079f1363f647   WPROS        468,775.258077         -17.151459       468,758.106619
  0x912c9ade24d44d8922f0866d8dcb079f1363f647   USDC         519,911.623312                +10       519,921.623312
[3] Decision: GO
```

`samples/04_blocked_swap_min_out.txt`:

```
  result : REVERT - Too little received
[3] Decision: BLOCKED
  blocked: simulation reverted: Too little received
  execution blocked automatically - no transaction was prepared
```

The `--from` addresses are real public accounts used only as simulation senders (anyone can simulate from any
address); none of the prepared transactions was signed or sent.

Proof (the router whose swap was simulated):
https://www.pharosscan.xyz/address/0xf38d34c8382b9079b0f85309578b43b8479cd875

## Notes and limits

* The prediction is for the head block at simulation time; anything mined before the user's tx can change it.
* The Chain MCP has no tracing tool, so the before/after table needs `debug_traceCall` on `https://rpc.pharos.xyz`
  (it works today); if it fails, simulation and cost still run and the prediction is marked unavailable.
* `swap` is wired to the WPROS/USDC pool (fee 100) through router `0xf38d…d875`; use `raw` for anything else.

License: MIT
