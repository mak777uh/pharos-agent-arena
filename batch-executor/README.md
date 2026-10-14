# Pharos Agent Arena · Batch Executor (SkillEngine skill, Multicall3, prepare-only)

A Skill that takes **several independent actions** on Pharos Pacific mainnet (chainId 1672) — PROS payouts,
permissionless keeper calls (Morpho Blue `accrueInterest`), other `msg.sender`-independent contract calls — and
bundles them into **one prepared transaction** to Multicall3 (`aggregate3Value`, `0xcA11bde0…CA11`) instead of
firing them one by one. Every action is simulated alone and inside the batch through the hosted **Pharos Chain MCP**
(`https://mcp.pharos.xyz/mcp`); the report says which action fails and why, what happened to the rest, and the gas
and transaction saving compared with running them individually.

**Prepare-only.** The batch is an **unsigned** EIP-1559 transaction (EIP-1193 request + the
`cast send … --private-key $PRIVATE_KEY` line for the official
[pharos-skill-engine](https://github.com/PharosNetwork/pharos-skill-engine), run by the user). No key is read, nothing
is signed or broadcast.

Built for the Pharos *Season of AI · Agent Arena* task
[Batch Executor](https://pharos-agent-arena.vercel.app/tasks/batch-executor).

## Requirements → where they are met

| # | Requirement | How |
|---|---|---|
| 1 | List of ≥ 3 independent actions | JSON actions file (3–100 actions; `send_pros`, `morpho_accrue`, `call`), validated; samples use 4–6 actions. |
| 2 | Executes them as a batch (multicall / single prepared tx) | one unsigned tx to Multicall3 `aggregate3Value((address,bool,uint256,bytes)[])` with the summed value, simulated with `eth_call` + `estimate_gas`. |
| 3 | Reports gas saving / step reduction | Σ individual `estimate_gas` vs batch `estimate_gas`, fee in PROS and USD, txs and signatures saved. Real: keeper batch 298,644 → 216,731 gas (−27.4 %), 5 txs → 1. Honest negative case too: 4 plain PROS sends 84,000 → 88,060 gas (+4.8 %), still 4 txs → 1. |
| 4 | Partial failure handled clearly | per-action table "alone" vs "in batch (allowFailure=true)" with decoded reasons; `--on-failure drop` (default) removes the failed action, re-simulates and prepares the rest; `--on-failure abort` prepares nothing. Final batch uses `allowFailure=false` (all-or-nothing on chain, no PROS stranded in Multicall3). |

## Skill format

```
SKILL.md                    agent entry point (official pharos-skill-engine frontmatter)
references/batching.md      action types, pipeline, partial-failure policy, when batching saves gas
assets/networks.json        official pharos-skill-engine network file (unchanged copy)
assets/keeper.json          4 Morpho accrueInterest + 1 PROS tip      assets/keeper_with_bad.json  + 1 unknown market
assets/payouts.json         4 PROS tips                                assets/payouts_with_bad.json + PROS to the USDC contract
assets/erc20_refused.json   contains an ERC-20 transfer (refused by design)
scripts/batch.py            the skill (CLI)
scripts/skill_chain.py, pay_core.py, pharos_mcp.py
samples/  tests/
```

## Run it

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 scripts/batch.py --actions assets/keeper.json
python3 scripts/batch.py --actions assets/keeper_with_bad.json                     # drop the failing action
python3 scripts/batch.py --actions assets/keeper_with_bad.json --on-failure abort  # prepare nothing
python3 -m unittest discover -s tests
```

## Real sample output (Pharos mainnet block #20,258,029, 2026-10-14 22:53 UTC)

`samples/02_keeper_partial_failure_drop.txt`:

```
 # id                   action                   to                                           alone (eth_call)             in batch (allowFailure=true)
 1 accrue-ethvp-wpros   accrueInterest 0xab78d4ff… 0x18573fa18fd17ddfd790b4a5b5b2977aad3b4efb   ok, 72,901 gas               ok
 2 accrue-ethvp-usdc    accrueInterest 0x81c8e4d8… 0x18573fa18fd17ddfd790b4a5b5b2977aad3b4efb   ok, 65,886 gas               ok
 3 accrue-unknown       accrueInterest 0x00000000… 0x18573fa18fd17ddfd790b4a5b5b2977aad3b4efb   REVERT: market not created   FAILED: market not created
 4 accrue-apc3m-usdc    accrueInterest 0xba0d2638… 0x18573fa18fd17ddfd790b4a5b5b2977aad3b4efb   ok, 72,886 gas               ok
 5 accrue-vrpcw-usdc    accrueInterest 0x082e865d… 0x18573fa18fd17ddfd790b4a5b5b2977aad3b4efb   ok, 65,971 gas               ok
 6 tip-alice            send 0.01 PROS           0x1f3546a29eec2102c8d52e21a45114124014c85c   ok, 21,000 gas               ok

partial failure: action 'accrue-unknown' fails (market not created)
  -> dropped from the batch; the remaining 5 actions were re-simulated together and go ahead: accrue-ethvp-wpros, accrue-ethvp-usdc, accrue-apc3m-usdc, accrue-vrpcw-usdc, tip-alice
OUTCOME: PARTIAL - 1 unsigned transaction bundles 5 actions (total value 0.01 PROS, allowFailure=false: all-or-nothing on chain)

Saving vs one transaction per action:
  transactions : 5 -> 1   (signatures/nonces saved: 4)
  gas          : 298,644 -> 216,731   (saved 81,913 = 27.4%)
  fee @ 10 gwei: 0.00298644 PROS -> 0.00216731 PROS (saved ~$0.000478 at PROS/USD 0.5840)

Prepared UNSIGNED tx to Multicall3: nonce 2168, gas limit 260,077, value 0.01 PROS, signing hash 0x47de0e733da942cf26b039c636827cfd80a6039984507e9e8a6b68703ddedbc9
```

Other runs:

| sample | input | outcome |
|---|---|---|
| 01 | `keeper.json` (5 actions) | ALL — 298,644 → 216,731 gas (−27.4 %), 5 txs → 1 |
| 03 | `keeper_with_bad.json --on-failure abort` | ABORTED — "1 action(s) fail in the batch; nothing prepared, the other 5 actions were not executed either", exit 4 |
| 04 | `payouts.json` (4 PROS tips) | ALL — 84,000 → 88,060 gas (**+4.8 %**, reported as extra cost), 4 txs → 1 |
| 05 | `payouts_with_bad.json` (+ PROS to the USDC contract) | PARTIAL — `tip-usdc-contract` reverts (no reason), dropped, 4 tips bundled |
| 06 | `erc20_refused.json` | error, exit 2 — ERC-20 transfers through Multicall3 refused |
| 07 | sample 02 as `--json` | |

Market ids are real Morpho Blue markets on Pharos; recipients and the sender are real public accounts used as
examples. Nothing was signed or sent.

Proof (Multicall3 contract the batch targets):
https://www.pharosscan.xyz/address/0xca11bde05977b3631167028862be2a173976ca11

## Notes and limits

* Inside the batch `msg.sender` is Multicall3, so only actions whose effect does not depend on the caller can be
  batched this way (PROS sends, permissionless keeper calls). ERC-20 transfers need a distributor contract (the
  official engine's airdrop capability) — refused here rather than done unsafely.
* Gas: batching saves the 21,000 base per extra tx; it does not help plain PROS transfers (see sample 04).
* Simulation is at the head block; the final all-or-nothing batch reverts as a whole if something changes before
  it is mined.

License: MIT
