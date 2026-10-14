# Pharos Agent Arena · Policy Gatekeeper (SkillEngine skill, Chain MCP, prepare-only)

A Skill that sits **in front of an agent's actions** on Pharos Pacific mainnet (chainId 1672) and checks each
requested action against a policy file before letting it through:

* **spend limits** — per transaction, per rolling 24 h, max approval (unlimited approvals always denied), per token;
* **allowlists** — recipients for transfers; contracts *and function selectors* for contract calls (ERC-20
  `transfer`/`approve` calldata is decoded so a raw call cannot bypass the limits or the recipient list);
* **time windows** — UTC days/hours, scoped to an action type or token.

Every request returns **ALLOW** or **DENY** with the id of the rule that decided it. Only allowed requests reach
the executor, which prepares an **unsigned** EIP-1559 transaction after on-chain pre-checks through the hosted
**Pharos Chain MCP** (`https://mcp.pharos.xyz/mcp`). Denied requests are never passed to the executor. Every
decision is appended to a **hash-chained JSONL audit log**.

**Prepare-only.** No private key is read or stored, nothing is signed or broadcast. Signing is left to the user
(EIP-1193 request or the `cast send … --private-key $PRIVATE_KEY` line for the official
[pharos-skill-engine](https://github.com/PharosNetwork/pharos-skill-engine), run by the user).

Built for the Pharos *Season of AI · Agent Arena* task
[Policy Gatekeeper](https://pharos-agent-arena.vercel.app/tasks/policy-gatekeeper).

## Requirements → where they are met

| # | Requirement | How |
|---|---|---|
| 1 | Policy file with ≥ 3 rule types: spend limit, allowlist, time window | `assets/policy.json` — `spend_limits` (per_tx / per_day / max_approval), `allowlist` (recipients + contracts/functions), `time_windows`; validated on load. |
| 2 | Allow/deny with the specific rule | every decision prints and logs `decided_by` (e.g. `spend_limits.USDC.per_day`, `allowlist.contracts`, `time_windows.contract_calls_business_hours`) plus the reason and the whole rule trail. |
| 3 | Denied actions never executed; ≥ 1 deny per rule type | the executor is called only on ALLOW (`executor called 3 times` for 3 ALLOW / 6 DENY in `samples/01`; `denied-but-executed 0` in `samples/04`; unit test asserts it). Real denies: spend (`per_day` r3, `per_tx` r4, no-limit r9), allowlist (recipient r5, contract r7), time window (r6, r11), validation (r10). |
| 4 | Audit log of all decisions | `samples/audit_log.jsonl` (11 entries), `gatekeeper.py audit` prints it and verifies the sha256 hash chain (tampering is detected — unit test). |

## Skill format

```
SKILL.md                    agent entry point (official pharos-skill-engine frontmatter)
references/policy.md        policy schema, request format, evaluation order, executor, audit log
assets/networks.json        official pharos-skill-engine network file (unchanged copy)
assets/policy.json          sample policy        assets/demo_requests.json   9 demo requests
scripts/gatekeeper.py       gatekeeper + executor + audit log (CLI)
scripts/pay_core.py, skill_chain.py, pharos_mcp.py   prepare-only tx builders + MCP client
samples/  tests/
```

## Run it

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 scripts/gatekeeper.py check --policy assets/policy.json --requests assets/demo_requests.json --log audit_log.jsonl
python3 scripts/gatekeeper.py check --policy assets/policy.json --request '{"type":"transfer","token":"USDC","to":"0x1f3546a29eec2102c8d52e21a45114124014c85c","amount":"25"}'
python3 scripts/gatekeeper.py audit --log audit_log.jsonl
python3 -m unittest discover -s tests          # offline: every rule type incl. allowed approval in hours, max_approval, unlimited
```

## Real sample output (Pharos mainnet, Wed 2026-10-14 22:36–22:38 UTC)

`samples/01_demo_requests.txt` (shortened; the full file has the rule trail for every request):

```
[ALLOW] r1-pay-alice: transfer 300 USDC -> 0x1f3546a29eec2102c8d52e21a45114124014c85c
        executor: prepared UNSIGNED tx (nonce 2167, signing hash 0xefa881d87deb8ff8…) - not signed, not sent
[ALLOW] r2-pay-bob: transfer 400 USDC -> 0x4ffd9c7e8641947662a43af298b734ca93530002
        executor: prepared UNSIGNED tx (nonce 2168, signing hash 0x34cf8e4bcf70338e…) - not signed, not sent
[DENY] r3-pay-alice: transfer 400 USDC -> 0x1f3546a29eec2102c8d52e21a45114124014c85c
        decided by: spend_limits.USDC.per_day - 700 USDC already executed in the last 24 h + 400 > daily limit 1000 USDC
        executor: NOT called (denied actions are never executed)
[DENY] r4-big-pay: transfer 600 USDC -> 0x4ffd…0002
        decided by: spend_limits.USDC.per_tx - 600 USDC > per-transaction limit 500 USDC
[DENY] r5-unknown: transfer 50 USDC -> 0x50fbb8651e332696ba14b04e17175f960ec18ab2
        decided by: allowlist.recipients - recipient 0x50fbb865… is not on the recipient allowlist
[DENY] r6-approve: contract_call 0xc879c018… data 0x095ea7b3… value 0 PROS
        decided by: time_windows.contract_calls_business_hours - wed 22:36:21 UTC is outside window mon,tue,wed,thu,fri 08:00-20:00 UTC
[DENY] r7-wpros-call: contract_call 0x52c48d42… data 0x2e1a7d4d… value 0 PROS
        decided by: allowlist.contracts - contract 0x52c48d4213107b20bc583832b0d951fb9ca8f0b0 is not on the contract allowlist
[ALLOW] r8-pros-bob: transfer 0.5 PROS -> 0x4ffd9c7e8641947662a43af298b734ca93530002
        executor: prepared UNSIGNED tx (nonce 2169, signing hash 0x5c81fbf257ed6a41…) - not signed, not sent
[DENY] r9-wpros: transfer 10 WPROS -> 0x1f35…c85c
        decided by: spend_limits.WPROS - no spend limit configured for WPROS -> denied by default
9 requests: 3 allowed, 6 denied; executor called 3 times. Audit log: samples/audit_log.jsonl
```

`samples/02_checksum_typo.txt` — one wrong-case character in the recipient → `request.validation` (EIP-55
checksum) DENY. `samples/03_unlimited_approve.txt` — an unlimited USDC approval sent at 22:38 UTC is stopped
by the time window first (the `max_approval` / unlimited denies are shown in the unit tests, because they are only
reached inside business hours). `samples/04_audit.txt`:

```
11 decisions: 3 ALLOW, 8 DENY; executed 3; denied-but-executed 0. hash chain intact.
```

The addresses are real public Pharos mainnet accounts used as examples (signer `0x84dbd231…072a` holds USDC and
PROS so the pre-checks run for real); the three prepared transactions were never signed or sent.

Proof (agent/signer account whose actions are gated; no tx was sent by design):
https://www.pharosscan.xyz/address/0x84dbd23178ba7fc6a2a396559139aef69d0f072a

## Notes and limits

* The daily limit counts prepared transactions (the gatekeeper cannot see whether the user signed) — conservative.
  It does not price tokens in USD; limits are per token.
* Time windows use the machine's UTC clock at decision time; there is no `--now` override on the CLI (tests inject
  the time).
* Within one run, prepared transactions get sequential nonces (pending nonce + n); sign them in order.

License: MIT
