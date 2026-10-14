# Pharos Agent Arena · Saga Executor (SkillEngine skill, prepare-only)

A Skill that runs a **multi-step, dependent on-chain workflow as a saga** on Pharos Pacific mainnet state
(chainId 1672):

**approve USDC → swap USDC→WPROS (WPROS/USDC pool, 0.01 %) → approve WPROS → deposit into the Vishwa WPROS
ERC-4626 vault.**

Each step is sent, its **receipt is checked** and its post-condition is verified on chain before the next step
starts. Transient failures are **retried** by a defined policy; a permanent failure triggers **compensation**
(cancel the abandoned nonce → revoke approvals → swap the WPROS back to USDC) so no funds or allowances are
left stranded halfway. Every transition goes to a **JSONL state log**.

Built for the Pharos *Season of AI · Agent Arena* task
[Saga Executor](https://pharos-agent-arena.vercel.app/tasks/saga-executor).

## What is real and what is not (read this first)

| | where | how |
|---|---|---|
| Preflight (chain id, contract code, `vault.asset()`, pool tokens/fee, pool price, Chainlink PROS/USD, balances, pending nonce, fees) | **Pharos mainnet**, read-only | hosted **Pharos Chain MCP** `https://mcp.pharos.xyz/mcp` |
| Step 1 simulation of the mainnet plan | **Pharos mainnet** | Chain MCP `eth_call` + `estimate_gas` |
| The saga run itself: send → receipt → verify → next, retries, compensation | **local anvil fork of Pharos mainnet** (`anvil --fork-url https://rpc.pharos.xyz --no-mining`, upstream from `assets/networks.json`) | the public sender address is **impersonated** on the fork — no key exists anywhere; real contract code + real mainnet state at the fork block; real EVM receipts |
| Mainnet output | **unsigned** EIP-1559 txs (nonces n…n+3), EIP-1193 request + `cast send … --private-key $PRIVATE_KEY` line **for the user** | never signed, never broadcast |
| Receipt check of a tx the user signed on mainnet | **Pharos mainnet** | `verify-receipt`: Chain MCP `get_transaction` + `get_transaction_receipt` |
| Compensation txs | simulated (`eth_call` + `estimate_gas`) on the stranded fork state, prepared unsigned (chainId 1672), then **executed on the fork** to prove the funds come back (`--no-execute-compensation` stops after simulate + prepare) | nothing reaches mainnet |

**Fork transaction hashes in the samples do not exist on pharosscan** — they are receipts from the local fork.
The pharosscan links in the output are real: the fork block, the contracts, and the real mainnet tx used by
`verify-receipt`.

## Requirements → where they are met

| # | Requirement | How |
|---|---|---|
| 1 | ≥ 3 dependent steps, each verified (receipt checked) before the next | 4 steps; each waits for its receipt, requires `status = 1`, then checks logs + state: `Approval` = amount and `allowance()`; USDC paid = amountIn and WPROS received ≥ minOut; approve amount = **what the swap receipt delivered**; ERC-4626 `Deposit` assets/shares and vault `balanceOf` delta. On mainnet: `verify-receipt` must say VERIFIED before step N+1 is prepared. |
| 2 | Retry policy for transient failures (gas spike, dropped tx) | `assets/saga.json → retry` (max 3 attempts, 3 confirm blocks, +25 % fee bump, 100 gwei cap, backoff, re-quote on slippage). Demos: **dropped tx** → resent with the same nonce (sample 02); **base-fee spike** 40 gwei → evicted as underpriced → replaced 20 → 80 gwei (03); **competing swap lands first** → mined with status 0 `Too little received` → re-quote → success (04). |
| 3 | Compensation/rollback path on permanent failure, and a demo | Permanent = simulation revert, receipt status 0 (non-slippage), post-condition mismatch, retries/fee budget exhausted. Compensation in reverse order: `cancel_pending` (supersede the abandoned nonce) → `revoke_wpros_vault` → `approve_wpros_router` + `swap_back` → `revoke_usdc_router`. Demos: deposit reverts `TransferFromReverted()` → unwind, 24.995 USDC back of 25 (05); fee above cap → abandoned nonce cancelled → unwind (06). |
| 4 | State log showing exactly which step the saga is in | `runs/<saga_id>.jsonl` (one flushed line per transition: phase, step 1/4…C4, state, attempt, tx, block) + `saga.py status --log …` → `CURRENT: phase=… state=… · last step …` (sample 07; all six logs in `runs/`). |
| 5 | One fully successful run and one forced-failure run with recovery | Success: sample 01 (25 USDC → 42.96 WPROS → 42.96 vault shares). Forced failures with recovery: 02–04 (retry → completed) and 05–06 (compensated, wallet back to USDC, all allowances 0). |

## Real run (15 Oct 2026, mainnet head ≈ block 20,260,850, fork at block [20,260,896](https://www.pharosscan.xyz/block/20260896))

Sender (public EOA, impersonated on the fork only): [`0x84dbd231…072a`](https://www.pharosscan.xyz/address/0x84dbd23178ba7fc6a2a396559139aef69d0f072a)
— 6,959.75 USDC, 7.14 PROS, pending nonce 2169. Pool spot 1 WPROS = 0.581677 USDC, Chainlink PROS/USD 0.5811.

| sample | inject | path | result |
|---|---|---|---|
| `01_success` | — | 4/4 VERIFIED | 25 USDC → 42.973099 WPROS → 42.964728 shares; gas 0.00287 PROS; unsigned mainnet plan, step 1 simulated on mainnet (gas 71,481) |
| `02_retry_dropped_swap` | `drop@swap` | swap dropped → resent, nonce 2170 | COMPLETED |
| `03_retry_gas_spike_approve` | `spike@approve_usdc` | evicted at maxFee 20 gwei (base 40) → replaced at 80 gwei | COMPLETED |
| `04_retry_frontrun_slippage_swap` | `frontrun@swap` | competing 3,000 USDC buy first in block → `Too little received` (status 0) → new quote 42.554 WPROS → VERIFIED | COMPLETED |
| `05_compensation_revert_deposit` | `revert@deposit` | deposit receipt status 0 `TransferFromReverted()` → revoke vault allowance, approve router, swap back | COMPENSATED: 24.995 USDC recovered (−0.005 USDC = 2× pool fee + impact), 0 WPROS, 0 shares, all allowances 0 |
| `06_compensation_fee_cap_deposit` | `spike-persist@deposit` | base fee 400 gwei, needed 800 > cap 100 → give up → cancel nonce 2172 → unwind | COMPENSATED, same end state |
| `07_status_after_compensation` | | `status --history` of run 05 | `CURRENT: phase=saga state=COMPENSATED` |
| `08_verify_receipt_mainnet_mismatch` | | `verify-receipt` against a real mainnet tx [0x9a2cdbaa…](https://www.pharosscan.xyz/tx/0x9a2cdbaa52286a3c16771d0f5a4af451b0fed815119687a78e285b60b0171139) that is **not** the planned approve | NOT VERIFIED (from/to/selector differ) → saga stays at `approve_usdc` |

`samples/*.json` hold the full reports: every receipt (gasUsed, effective price), balances before/after, the
unsigned mainnet plan (01–04) or the prepared compensation txs (05–06).

## Usage

```bash
pip install -r requirements.txt            # Foundry's anvil must be on PATH (or --anvil /path/to/anvil)
python3 scripts/saga.py run                                    # assets/saga.json: 25 USDC, 50 bps slippage
python3 scripts/saga.py run --amount 100 --sender 0x…           # any public address with USDC
python3 scripts/saga.py run --inject revert@deposit --json-out report.json
python3 scripts/saga.py run --inject spike-persist@deposit --no-execute-compensation
python3 scripts/saga.py status --log runs/<saga_id>.jsonl --history
python3 scripts/saga.py verify-receipt --plan report.json --step approve_usdc --tx 0x…
python3 -m unittest discover -s tests       # offline tests
```

## Skill format

```
SKILL.md               agent entry point (official pharos-skill-engine frontmatter)
references/saga.md     steps, verification, retry table, compensation, fault injection, state log format
assets/networks.json   official pharos-skill-engine network file (unchanged copy)
assets/saga.json       saga definition: sender, amount, slippage, contracts, retry policy
scripts/saga.py        the skill (run / status / verify-receipt)
scripts/skill_chain.py, pay_core.py, pharos_mcp.py   shared Chain MCP client, simulate, unsigned EIP-1559
runs/                  the JSONL state logs of the sample runs
samples/               real outputs
```

## Limits (honest)

- No mainnet transaction was sent; the saga's receipts come from a local fork of mainnet. A reviewer can
  re-run any sample: it forks the then-current mainnet block.
- anvil evicts underpriced txs instead of keeping them queued; the executor treats both the same way
  (fee-bumped replacement at the same nonce), and before compensating it always supersedes the last
  unconfirmed nonce, because a real node may still hold it.
- `revert@deposit` forces the failure by doubling the deposit amount and skipping the pre-send simulation gate
  (which would otherwise have caught it — the log shows `SIM_REVERT` first).
- The mainnet plan's steps 2–4 depend on step 1's state, so only step 1 can be simulated on mainnet today; their
  gas comes from the fork receipts (+30 %). Re-run before signing if the price moved.
- Mainnet `estimate_gas` for the approve (71,481) is higher than the fork's (55,831): Pharos' gas accounting differs
  from anvil's EVM; the mainnet plan uses the mainnet estimate for step 1.

## License

MIT
