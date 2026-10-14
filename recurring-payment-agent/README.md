# Pharos Agent Arena · Recurring Payment Agent (Chain MCP, prepare-only)

Manages repeating on-chain payments — rent, subscriptions, payroll-style transfers — on a **weekly or monthly**
schedule on Pharos Pacific mainnet (chainId 1672). Each `run` works out which periods are due, checks the chain
for a payment that already happened, and otherwise prepares **one unsigned EIP-1559 transaction** per period for
the payer to sign. Every period is tracked as `scheduled` / `pending` / `paid` / `failed` / `insufficient_funds`.
All chain reads go through the hosted **Pharos Chain MCP** (`https://mcp.pharos.xyz/mcp`): `get_logs`
(USDC `Transfer` events), `get_block` (timestamps → blocks), `get_transaction_count`, `get_transaction(_receipt)`,
`eth_call` (`balanceOf`, `allowance`, `isBlacklisted`, `symbol`, `decimals`), `estimate_gas`, `get_balance`,
`get_gas_price`.

**Prepare-only.** No private key, no signing, no broadcasting. The agent writes unsigned transactions
(`prepared/<schedule>_<period>_attempt<N>.json`: EIP-1193 `eth_sendTransaction` request, unsigned EIP-1559
serialization, signing hash). Signing and sending is left to the user's wallet; the agent then sees the payment on
chain (or via `confirm --tx`) and marks it paid.

Built for the Pharos *Season of AI · Agent Arena* task
[Recurring Payment Agent](https://pharos-agent-arena.vercel.app/tasks/recurring-payment-agent).

## Model

* **Schedule** (`add`): `--payer`, `--to`, `--token` (USDC, WPROS, LINK, WETH or an ERC-20 address; on-chain
  `symbol`/`decimals`), `--amount`, `--every weekly|monthly`, `--start` (first due time, ISO with a time zone).
  Monthly keeps the day of month and clamps to the month end (31 Jan → 28 Feb → 31 Mar). Two modes:
  * **push** (default): the payer signs `transfer(to, amount)`;
  * **pull** (`--signer OPERATOR`): an operator signs `transferFrom(payer, to, amount)` using the payer's
    `allowance` (payroll / subscription-operator pattern) — allowance and payer balance are checked via `eth_call`.
  Native PROS schedules are refused (no `Transfer` event to reconcile against; use WPROS).
* **Period** = one due date; it holds a list of **attempts**. The calldata of every prepared tx carries the
  reference `PHAROS-REF:<schedule>:<period>` after the ERC-20 arguments, so a payment can be traced on chain.
* **`run`** (cron it, e.g. hourly) for every period with `due ≤ now + lead` (`--lead-hours`, default 1):
  1. `paid` → skip.
  2. `pending` → look for a matching transfer since the attempt was prepared (→ `paid`; the attempt is `mined`
     if it was this very tx, otherwise `void – do not sign`); else if the signer's nonce is now higher than the
     attempt's nonce and no matching transfer exists, the unsigned tx can never be mined → attempt `failed`
     (dropped); else it **stays pending and nothing new is prepared**.
  3. `scheduled` / `failed` / `insufficient_funds` → first search the chain for a `Transfer(payer → to, exact
     amount)` from `due − lead` on (paid manually or by someone else → `paid`, nothing prepared); otherwise
     prepare a new attempt with full validation (balance, allowance, blacklist, `estimate_gas`, PROS for gas,
     pending nonce). Not enough tokens/allowance/gas → `insufficient_funds`, **no tx**, retried next run.
* **No double payment**: (a) a period is settled by an on-chain transfer, whoever sent it; (b) a transfer hash
  settles at most one period (`used_txs`); (c) never two live attempts for one period — a new attempt is only made
  after the previous one provably cannot be mined (nonce consumed elsewhere, or reverted); (d) `confirm` refuses a
  tx already used, a tx that does not transfer the scheduled amount from payer to recipient, and a reverted tx
  (which marks the period `failed` instead).
* **`confirm --id --period --tx`**: settle a period with the hash of the tx you signed (checks the receipt status
  and the `Transfer` log). **`history`**: every period, its attempts, tx links, open items and the **next scheduled
  payment**. **`list`**: one line per schedule with the next unpaid due date.
* State is one JSON file (`--state`, default `recurring_state.json`) written atomically (temp file + rename).
  Payment search is bounded by `--scan-blocks` (default 20,000 ≈ 5.8 h at ~1.04 s/block) to respect the MCP
  `get_logs` 1,000-block limit and the 120 req/min rate limit.

## Run it

Python 3.10+ (tested with 3.12).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python recurring_agent.py add --id rent --payer 0xYou --to 0xLandlord --token USDC --amount 1000 --every monthly --start 2026-11-01T09:00Z
python recurring_agent.py run                    # e.g. from cron: 0 * * * *  cd /path && python recurring_agent.py run
python recurring_agent.py history
python recurring_agent.py confirm --id rent --period 2026-11-01 --tx 0x<hash of the tx you signed>
python -m unittest discover -s tests -v          # offline tests (schedule math, state machine, tx encoding)
```

Global options: `--state FILE`, `--endpoint` (`https://` only, or `http://` to localhost). Inputs are validated:
addresses + EIP-55 checksum, amount > 0 and within token decimals, time zone required, `--every` only
weekly/monthly, schedule id `[A-Za-z0-9_-]{1,32}`, tx hash format, `--lead-hours` 0–168.

## Sample output (real runs on Pharos mainnet, 2026-10-14 21:31–21:41 UTC)

Transcripts: [`samples/01`–`07`](samples/), final state [`samples/recurring_state.json`](samples/recurring_state.json),
prepared unsigned transactions in [`samples/prepared/`](samples/prepared/).

The four demo schedules use **real public accounts** (we control none of them; nothing was signed):

| id | mode | payer → recipient | amount | why |
|---|---|---|---|---|
| `payroll-600` | pull, operator [`0x25fdb4a1…b058`](https://www.pharosscan.xyz/address/0x25fdb4a165661f07cb52ac55d00d8d191126b058) | `0x84ddbccb…112f` → `0x3e79f7ff…7737` | 600 USDC weekly | mirrors a **real repeating payment**: that operator pulls exactly 600 USDC via `transferFrom` every ~2 h 20 min |
| `rent` | push | [`0x84DBd231…F072a`](https://www.pharosscan.xyz/address/0x84dbd23178ba7fc6a2a396559139aef69d0f072a) → `0x1F3546a2…c85C` | 1000 USDC monthly | payer has 6,845 USDC |
| `subscription` | push | [`0x1F3546a2…c85C`](https://www.pharosscan.xyz/address/0x1f3546a29eec2102c8d52e21a45114124014c85c) → `0x4FFD9c7E…0002` | 20 USDC weekly | very active payer: its nonce moves on its own |
| `payroll-bob` | push | `0x1F3546a2…c85C` → `0x4FFD9c7E…0002` | 2500 USDC weekly | more than the payer holds |

**Run 1** ([`02`](samples/02_run_first.txt)) — paid detected on chain, two txs prepared, one insufficient:

```
[2026-10-14 21:34 UTC] run: 4 schedule(s), block 20,253,685, lead 1.0 h
[2026-10-14 21:34 UTC] payroll-600 2026-10-14: PAID by https://www.pharosscan.xyz/tx/0xe12e4b5452d0bd36900458f26148e3810051792d407e3c49075275edd2db09e5 (block 20,251,314, already paid on chain before preparing)
[2026-10-14 21:34 UTC] rent 2026-10-14: due (due 2026-10-14 21:00 UTC), no payment on chain yet -> preparing
[2026-10-14 21:34 UTC] rent 2026-10-14: PREPARED attempt #1 - 1,000 USDC -> 0x1F3546a29EeC2102c8d52E21a45114124014c85C, nonce 2165, gas 70,962, signing hash 0x490c79caedd27a6b... -> samples/prepared/rent_2026-10-14_attempt1.json (unsigned; sign it in your wallet)
[2026-10-14 21:35 UTC] subscription 2026-10-14: due (due 2026-10-14 21:00 UTC), no payment on chain yet -> preparing
[2026-10-14 21:35 UTC] subscription 2026-10-14: PREPARED attempt #1 - 20 USDC -> 0x4FFD9c7E8641947662a43aF298B734ca93530002, nonce 38487, gas 71,143, signing hash 0x36aa29fbb9539d0e... -> samples/prepared/subscription_2026-10-14_attempt1.json (unsigned; sign it in your wallet)
[2026-10-14 21:35 UTC] payroll-bob 2026-10-14: due (due 2026-10-14 21:00 UTC), no payment on chain yet -> preparing
[2026-10-14 21:35 UTC] payroll-bob 2026-10-14: INSUFFICIENT FUNDS - insufficient USDC: payer has 1,926.073338, needs 2500; nothing prepared, will retry next run
```

**Run 2** ([`03`](samples/03_run_second.txt)) — pending attempts are **not** prepared again:

```
[2026-10-14 21:36 UTC] rent 2026-10-14: attempt #1 still PENDING (nonce 2165 unused) - not preparing another tx; sign samples/prepared/rent_2026-10-14_attempt1.json
[2026-10-14 21:36 UTC] subscription 2026-10-14: attempt #1 still PENDING (nonce 38487 unused) - not preparing another tx; sign samples/prepared/subscription_2026-10-14_attempt1.json
```

**Run 3** ([`06`](samples/06_run_third.txt)) — the subscription payer sent 5 other transactions meanwhile, so the
unsigned attempt #1 (nonce 38487) can never be mined → `failed`, and exactly one new attempt is prepared:

```
[2026-10-14 21:41 UTC] subscription 2026-10-14: attempt #1 FAILED (dropped: signer nonce is now 38492 > 38487 and no matching transfer exists, so this unsigned tx can never be mined)
[2026-10-14 21:41 UTC] subscription 2026-10-14: due (due 2026-10-14 21:00 UTC), no payment on chain yet -> preparing
[2026-10-14 21:41 UTC] subscription 2026-10-14: PREPARED attempt #2 - 20 USDC -> 0x4FFD9c7E8641947662a43aF298B734ca93530002, nonce 38492, gas 71,143, signing hash 0x60e584bbbee1798a... -> samples/prepared/subscription_2026-10-14_attempt2.json (unsigned; sign it in your wallet)
[2026-10-14 21:41 UTC] payroll-bob 2026-10-14: INSUFFICIENT FUNDS - insufficient USDC: payer has 1,633.392269, needs 2500; nothing prepared, will retry next run
```

**History and next payments** (`history`, [`07`](samples/07_history.txt)):

```
== payroll-600: 600 USDC weekly from 0x84ddbccba32bee8f4e775d737bec2d3c10ea112f to 0x3e79f7ffa10d23dac3ad16af98994f2214f69737 - pull (operator 0x25fdb4a165661f07cb52ac55d00d8d191126b058 signs transferFrom)
   period      due                   status              detail
   2026-10-14  2026-10-14 20:30 UTC  paid                https://www.pharosscan.xyz/tx/0xe12e4b5452d0bd36900458f26148e3810051792d407e3c49075275edd2db09e5
   next scheduled payment: 2026-10-21 20:30 UTC (600 USDC)
== rent: 1000 USDC monthly from 0x84dbd23178ba7fc6a2a396559139aef69d0f072a to 0x1f3546a29eec2102c8d52e21a45114124014c85c - push (payer signs transfer)
   period      due                   status              detail
   2026-10-14  2026-10-14 21:00 UTC  pending             attempt #1: nonce 2165, samples/prepared/rent_2026-10-14_attempt1.json
   next scheduled payment: 2026-11-14 21:00 UTC (1000 USDC)
   open (not yet paid): 2026-10-14
== subscription: 20 USDC weekly from 0x1f3546a29eec2102c8d52e21a45114124014c85c to 0x4ffd9c7e8641947662a43af298b734ca93530002 - push (payer signs transfer)
   period      due                   status              detail
   2026-10-14  2026-10-14 21:00 UTC  pending             attempt #2: nonce 38492, samples/prepared/subscription_2026-10-14_attempt2.json
                                       #1 failed         dropped: signer nonce is now 38492 > 38487 and no matching transfer exists, so this unsigned tx can never be mined
   next scheduled payment: 2026-10-21 21:00 UTC (20 USDC)
   open (not yet paid): 2026-10-14
== payroll-bob: 2500 USDC weekly from 0x1f3546a29eec2102c8d52e21a45114124014c85c to 0x4ffd9c7e8641947662a43af298b734ca93530002 - push (payer signs transfer)
   period      due                   status              detail
   2026-10-14  2026-10-14 21:00 UTC  insufficient_funds  attempt #3: insufficient USDC: payer has 1,633.392269, needs 2500
   ...
   next scheduled payment: 2026-10-21 21:00 UTC (2500 USDC)
```

**confirm** ([`04`](samples/04_confirm.txt), [`05`](samples/05_confirm_paid_by_tx.txt)): the 600 USDC tx cannot
settle a second schedule (`REJECTED: … already settles another period`), confirming an already paid period is a
no-op, an unrelated tx (`0x3e1168df…`) is rejected because it does not transfer 1000 USDC from the rent payer to the
landlord, a malformed hash is refused. In a fresh state, `payroll-600` first hits `insufficient_funds` in pull mode
(the payer's balance is 0 between pulls — real), then `confirm --tx 0xe12e4b54…` settles it as paid.

The prepared unsigned tx for the subscription, attempt #2
([`samples/prepared/subscription_2026-10-14_attempt2.json`](samples/prepared/subscription_2026-10-14_attempt2.json)):

```
{"method": "eth_sendTransaction", "params": [{"from": "0x1F3546a29EeC2102c8d52E21a45114124014c85C", "to": "0xC879C018dB60520F4355C26eD1a6D572cdAC1815", "value": "0x0", "data": "0xa9059cbb0000000000000000000000004ffd9c7e8641947662a43af298b734ca935300020000000000000000000000000000000000000000000000000000000001312d00504841524f532d5245463a737562736372697074696f6e3a323032362d31302d3134", "gas": "0x115e7", "maxFeePerGas": "0x4a817c800", "maxPriorityFeePerGas": "0x0", "nonce": "0x965c", "chainId": "0x688", "type": "0x2"}]}
unsigned: 0x02f89082068882965c808504a817c800830115e794c879c018db60520f4355c26ed1a6d572cdac181580b866a9059cbb…c0
signing hash: 0x60e584bbbee1798aeaa1238471f0161223efa8afb23cf13a90ff1ce8caeec9a0   reference: subscription:2026-10-14
```

## How it covers the task requirements

| # | Requirement | Implementation |
|---|-------------|----------------|
| 01 | Schedule (weekly or monthly) with amount, token and recipient | `add --every weekly\|monthly --amount --token --to [--signer]`, month-end clamping; `tests/test_schedule.py` |
| 02 | Prepares each payment on time; tracks paid, pending and failed | `run` (cron) prepares periods due within `--lead-hours`; statuses `scheduled/pending/paid/failed/insufficient_funds` per period + per attempt (`mined/void/failed`); runs 02/03/06 |
| 03 | Handles insufficient balance or a failed tx without double-paying | `insufficient_funds` → no tx, retried; failed (dropped nonce / reverted) → exactly one new attempt; on-chain payment search before every prepare; one live attempt per period; tx hash settles one period; `tests/test_state_machine.py` |
| 04 | Payment history and the next scheduled payment | `history` (periods, attempts, explorer links, open items, next scheduled payment) and `list`; sample 07 |

## Honesty notes / limits

* The agent never signs; "on time" means the unsigned tx is ready by the due time — the payment happens when the
  user signs it. We did not sign or broadcast anything, so the `paid` statuses in the samples come from real
  third-party transfers that match the schedule (`payroll-600` mirrors an existing payroll-style pull).
* The reverted-receipt branch of `confirm` and the `mined`/`void` attempt states are covered by the offline
  state-machine tests (stub chain), not by a live run: we could not create a reverted or mined tx of our own
  without spending funds. The pull-mode allowance check exists in code (`eth_call allowance`) but in the live
  demo the payer's balance check (0 USDC between pulls) stopped it first.
* Payment matching is by exact amount and payer → recipient; a transfer of a different amount is not treated as
  payment (partial payments are not netted). Search depth is bounded by `--scan-blocks`.
* Nonce/gas/fees are as of preparation; if the signer sends other txs first, the agent marks the attempt failed
  and prepares a fresh one on the next run.

## Security

No private key, wallet, seed or API key. HTTPS-only Chain MCP endpoint, validated inputs, atomic state writes.
License: MIT.
