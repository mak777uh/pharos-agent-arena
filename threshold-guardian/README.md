# Pharos Agent Arena · Threshold Guardian (SkillEngine skill, Chain MCP, prepare-only)

A Skill that checks **several on-chain conditions together** on Pharos Pacific mainnet (chainId 1672) and only
then decides whether an action should fire — e.g. *"only rebalance if the price moved less than 3 %, the pool
agrees with Chainlink, gas ≤ 20 gwei, the Morpho USDC market has > 50k liquidity and the treasury holds ≥ 1,000
USDC"*. Every condition is read live through the hosted **Pharos Chain MCP** (`https://mcp.pharos.xyz/mcp`),
pinned to one block. Each run logs a decision trace (value, operator, threshold, PASS/FAIL, source) and ends in
**FIRE** or **HOLD**.

**Prepare-only.** On FIRE the action (an ERC-20 transfer) is built as an **unsigned** EIP-1559 transaction after
full pre-checks (balance, blacklist, `estimate_gas`, PROS for gas, nonce). The skill never asks for, reads or
stores a private key and never broadcasts; signing is left to the user (wallet `eth_sendTransaction` request or
the `cast send … --private-key $PRIVATE_KEY` line for the official
[pharos-skill-engine](https://github.com/PharosNetwork/pharos-skill-engine), which the *user* runs).

Built for the Pharos *Season of AI · Agent Arena* task
[Threshold Guardian](https://pharos-agent-arena.vercel.app/tasks/threshold-guardian).

## Skill format

Same layout as the official Pharos Skill Engine, so it can be dropped into an agent's skills directory
(e.g. `~/.claude/skills/pharos-threshold-guardian/`) next to it:

```
SKILL.md                      agent entry point (frontmatter: name, description, version, requires)
references/conditions.md      condition types, operators, decision modes, overrides, log format
assets/networks.json          official pharos-skill-engine network file (unchanged copy)
assets/guardian.rebalance.json sample config (5 conditions + action)
scripts/guardian.py           the guardian (CLI)
scripts/skill_chain.py        typed chain readers (pool price, Chainlink, Morpho market, balances, gas)
scripts/pay_core.py           prepare-only ERC-20 transfer builder (unsigned EIP-1559)
scripts/pharos_mcp.py         minimal MCP client (Streamable HTTP, JSON-RPC)
samples/                      real runs (below)   tests/  offline tests
```

## Requirements → where they are met

| # | Requirement | How |
|---|---|---|
| 1 | ≥ 3 independent conditions read from chain | sample config has 5: pool price change (archive `eth_call` `slot0()` now vs 3,600 blocks ago), pool-vs-Chainlink deviation, gas price (`get_gas_price`), Morpho Blue market liquidity (`market(bytes32)`), treasury USDC `balanceOf`. 8 condition types in total (`references/conditions.md`). |
| 2 | Configurable, not hard-coded | conditions, thresholds, operators, params, decision mode (`all`/`any`/k-of-n) and the action live in a JSON config; `--set id.field=value` overrides per run. |
| 3 | Clear decision trace every run | console trace + one JSONL record per run (`--log`): value, op, threshold, pass/fail, source (contract, function, block), final decision, prepared tx or hold reason. |
| 4 | Both outcomes | `samples/01_fire.txt` (5/5 PASS → FIRE, tx prepared) and `samples/02_hold_liquidity.txt`, `samples/03_hold_price_trigger.txt` (HOLD, nothing prepared). |

## Run it

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 scripts/guardian.py --config assets/guardian.rebalance.json                 # exit 0 FIRE / 3 HOLD / 2 error
python3 scripts/guardian.py --config assets/guardian.rebalance.json --set vault_liquidity.threshold=150000
python3 scripts/guardian.py --config assets/guardian.rebalance.json --json
python3 -m unittest discover -s tests                                               # offline tests
```

## Real sample output (Pharos mainnet, 2026-10-14 ~22:20 UTC)

FIRE — `samples/01_fire.txt` (excerpt):

```
Threshold Guardian · usdc-rebalance-guardian · Pharos mainnet block #20,256,150 (2026-10-14 22:19:45 UTC)
mode: all (need 5 of 5 conditions)
[PASS] price_move                     0.0322 %  < 3 %
       WPROS/USDC 0.583049 @#20,252,550 -> 0.582861 @#20,256,150
[PASS] oracle_dev                     0.4710 %  < 2 %
       pool 0.582861 vs Chainlink 0.585620
[PASS] gas                        10.0000 gwei <= 20 gwei
[PASS] vault_liquidity        98,563.1600 USDC  > 50,000 USDC
       WPROS/USDC supply 1,098,122.23 borrow 999,559.07
[PASS] treasury_usdc           6,846.8544 USDC >= 1,000 USDC
DECISION: FIRE  (5/5 passed, 5 required)

=== Prepared action: rebalance 250 USDC treasury -> ops wallet (review before signing) ===
  Pay        : 250 USDC  (contract 0xc879c018db60520f4355c26ed1a6d572cdac1815)
  Network    : Pharos mainnet, chainId 1672, nonce 2166, gas limit 71,060
    [ok] simulation OK: estimate_gas 59,217 -> gas limit 71,060 (+20%)
Unsigned - sign it yourself (this skill never signs or broadcasts):
```

HOLD — `samples/02_hold_liquidity.txt` (threshold raised to 150,000 USDC) and
`samples/03_hold_price_trigger.txt` (the task's own example "only if price moved **more** than 3 %", via
`--set price_move.op=gt`):

```
[FAIL] vault_liquidity        98,563.1600 USDC  > 150,000 USDC
DECISION: HOLD  (4/5 passed, 5 required)
action: HOLD - nothing prepared

[FAIL] price_move                     0.0829 %  > 3 %
       WPROS/USDC 0.583049 @#20,252,598 -> 0.582565 @#20,256,198
DECISION: HOLD  (4/5 passed, 5 required)
```

All three runs are in `samples/guardian_log.jsonl`. The addresses in the sample config are real public
accounts used only as examples (treasury `0x84dbd231…072a` holds USDC and PROS, so the pre-checks can run); nothing
was signed or sent.

Proof (pool whose live price drives the decision):
https://www.pharosscan.xyz/address/0x912c9ade24d44d8922f0866d8dcb079f1363f647

## Notes and limits

* "Vault liquidity" = Morpho Blue market supply − borrow (the market's withdrawable USDC); Morpho's own API does
  not index chainId 1672, so this is read directly from the Morpho contract.
* Gas price is read at `latest` (the MCP `get_gas_price` tool has no block argument); on Pharos it has been a flat
  10 gwei.
* A condition that cannot be read is FAIL, never PASS. If conditions pass but the action fails a pre-check (e.g.
  not enough USDC), the decision is downgraded to HOLD with the reason.
* Only `erc20_transfer` and `none` actions are implemented.

License: MIT
