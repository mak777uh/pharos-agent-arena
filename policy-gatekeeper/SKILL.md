---
name: pharos-policy-gatekeeper
description: >
  Policy gate in front of an agent's on-chain actions on Pharos mainnet (chainId 1672). Use BEFORE any
  transfer or contract call an agent wants to make: it checks the request against a policy file (spend limits
  per transaction and per rolling 24 h, recipient and contract/function allowlists, UTC time windows) and
  returns ALLOW or DENY with the exact rule that decided. Only allowed requests are prepared — as UNSIGNED
  transactions, after Chain MCP pre-checks — and every decision goes into a hash-chained audit log.
  Never signs or broadcasts.
version: 0.1.0
requires:
  anyBins:
  - python3
---

# Pharos Policy Gatekeeper

Same layout as the official [`pharos-skill-engine`](https://github.com/PharosNetwork/pharos-skill-engine). Put it
in front of the engine's write capabilities: an action the gatekeeper denies must not be handed to `cast send`.

## Prerequisites

1. Python 3.10+, `pip install -r requirements.txt`.
2. No private key. The gatekeeper only prepares unsigned transactions for the policy's `agent` address.

## Capability Index

| User / agent need | Command | Details |
|---|---|---|
| Check one action | `python3 scripts/gatekeeper.py check --policy assets/policy.json --request '<json>'` | → `references/policy.md` |
| Check a queue of actions in order | `... check --policy … --requests <file.json>` | → `references/policy.md#requests` |
| Show / verify the audit log | `python3 scripts/gatekeeper.py audit --log audit_log.jsonl` | → `references/policy.md#audit-log` |

Exit codes for `check`: 0 all allowed, 5 at least one deny, 2 bad policy/input.

## Agent Guidelines

1. Every action the agent plans (payment, approval, contract call) is sent to `check` first. No exceptions.
2. On **DENY**: stop. Report `decided by` and the reason to the user verbatim. Do not rephrase the request to
   get around the rule, do not split a payment to stay under a limit unless the user explicitly asks.
3. On **ALLOW**: show the prepared unsigned transaction and the signing command; the user signs it.
4. Never edit the audit log. `audit` re-computes the hash chain and reports tampering.

## Security Reminders

- Default deny: unknown request types, unknown tokens, tokens without a spend limit, unlimited approvals,
  malformed or checksum-failing addresses are all denied.
- `https://` endpoints only. No keys are read or stored.
