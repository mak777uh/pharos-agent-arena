# Policy reference

`assets/policy.json`:

```json
{
  "name": "treasury-agent-v1",
  "agent": "0x…signer",
  "rules": {
    "spend_limits": [{"token": "USDC", "per_tx": "500", "per_day": "1000", "max_approval": "200"},
                     {"token": "PROS", "per_tx": "1", "per_day": "2"}],
    "allowlist": {"recipients": {"0x…": "label"},
                  "contracts": {"0x…": {"label": "USDC", "functions": ["transfer(address,uint256)", "approve(address,uint256)"]}}},
    "time_windows": [{"id": "contract_calls_business_hours", "applies_to": {"type": "contract_call"},
                      "days": ["mon","tue","wed","thu","fri"], "start": "08:00", "end": "20:00"}]
  }
}
```

The policy is validated on load (addresses, days, times, decimal limits); a broken policy → exit 2, nothing runs.

## Requests

```json
{"id": "r1", "type": "transfer", "token": "USDC|PROS|WPROS|LINK|WETH", "to": "0x…", "amount": "300"}
{"id": "r6", "type": "contract_call", "to": "0x…contract", "data": "0x…calldata", "value_pros": "0"}
```

For a `contract_call` to a registry token with `transfer`/`approve` calldata, the amount and the counterparty are
decoded from the calldata (decimals read on chain), so a raw call cannot bypass the spend limit or the recipient
allowlist.

## Evaluation order (first DENY decides)

| step | rule id(s) | denies when |
|---|---|---|
| 0 | `request.validation` | unknown type/token, non-numeric or ≤ 0 amount, bad hex, invalid address or EIP-55 checksum |
| 1 | `allowlist.recipients` | transfer recipient (or decoded ERC-20 counterparty) not allowlisted |
| 1 | `allowlist.contracts` | contract not allowlisted, or function selector not in its `functions` |
| 2 | `time_windows.<id>` | the first window whose `applies_to` matches (type and/or token) does not contain the current UTC day/time (start and end inclusive) |
| 3 | `spend_limits.<TOKEN>` | no limit configured for a token the action moves (default deny) |
| 3 | `spend_limits.<TOKEN>.per_tx` | amount > `per_tx` |
| 3 | `spend_limits.<TOKEN>.per_day` | executed amount in the last 24 h (from the audit log) + amount > `per_day` |
| 3 | `spend_limits.<TOKEN>.max_approval` | `approve` amount > `max_approval`, or unlimited (≥ 2^255) |

"Executed" means the executor prepared the unsigned transaction. Prepared-but-unsigned transactions count toward
the daily limit (conservative: the gatekeeper cannot know whether the user signed).

## Executor

Called only for ALLOW. Transfers go through `pay_core.Chain.prepare` (chain id, token code/decimals, blacklist,
balance, `estimate_gas`, PROS for gas, pending nonce); contract calls through `skill_chain.prepare_tx`
(code check, `eth_call` + `estimate_gas`, fees, nonce). Within one session the nonces are sequential (pending nonce,
+1 for each further prepared tx) — sign them in order. A failed pre-check is logged as `execution_error`.

## Audit log

JSONL, one entry per decision: time, policy, agent, request, decision, decided_by, reason, full rule trail, spends,
executed, prepared unsigned tx (if any), `prev` (hash of the previous entry) and `hash` =
sha256(canonical JSON of the entry without `hash`). `audit` prints the table and verifies the chain.
