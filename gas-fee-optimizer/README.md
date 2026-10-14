# Pharos Agent Arena · Gas Fee Optimizer (Chain MCP)

Monitors the Pharos mainnet gas price and **times non-urgent transactions** for the lowest cost before a deadline
you set. All chain data comes from the hosted **Pharos Chain MCP** (`https://mcp.pharos.xyz/mcp`, Pharos Pacific
mainnet, chainId 1672): `get_gas_price`, `get_block`, `estimate_gas`, `get_transaction(_receipt)`, `eth_call`.

**Prepare-only by default.** The tool never holds or asks for a private key. When it decides to submit, it prints the
unsigned EIP-1559 transaction with the chosen fee fields for you to sign. Only if you pass a transaction **you
signed yourself** (`--raw-tx FILE --broadcast`) does it send it, via the Chain MCP tool `send_raw_transaction`
(note: the hosted `mcp.pharos.xyz` has that tool switched off, so broadcasting needs a self-hosted
[pharos-mcp](https://github.com/PharosNetwork/pharos-mcp) given with `--endpoint`). The broadcast path was **not
exercised** for this submission (no funds were spent); every sample below is a dry run or a read-only report.

Built for the Pharos *Season of AI · Agent Arena* task
[Gas Fee Optimizer](https://pharos-agent-arena.vercel.app/tasks/gas-fee-optimizer).

## Commands

| Command | What it does |
|---|---|
| `watch --samples N --interval S` | polls `get_gas_price` (eth_gasPrice + suggested tip) and the latest block's `baseFeePerGas`, stores every sample in `gas_history.json` (the file from our runs is [`sample_gas_history.json`](sample_gas_history.json)), prints a **history table + min/median/max + sparkline** |
| `history --hours H --points P` | backfill: `baseFeePerGas` of P blocks spread over the last H hours (block rate measured from the chain) |
| `hold --to … [--value PROS] [--data 0x…] [--from …] --deadline 15m [--target-gwei X \| --percentile P]` | records the price at **request time**, estimates gas (`estimate_gas` +20 %), then polls; **submits as soon as baseFee+tip ≤ target**, otherwise **at the last poll that still fits before the deadline** |
| `report --tx HASH (--request-block N \| --request-gwei X)` | **actual gas paid** (receipt `effectiveGasPrice` × `gasUsed`) vs the price at request time, in gwei, PROS and USD (Chainlink PROS/USD feed `0x371Ed36a…5466`, `latestAnswer()`) |

`hold` details:
* "favourable" = `baseFee + tip ≤ --target-gwei`, or ≤ the `--percentile` (default p25) of all recorded samples.
* **Deadline guarantee** (requirement 04): before sleeping it checks `time left ≤ interval + safety (5 s)`; if so it
  submits now, even if gas is high, so the submission always happens before the deadline. `--deadline` accepts
  `90s` / `15m` / `2h` or an ISO time with a zone (10 s … 7 days ahead).
* Fee fields: `maxPriorityFeePerGas` = network suggestion from `get_gas_price` (or `--tip-gwei`),
  `maxFeePerGas` = 2 × baseFee + tip (headroom if the base fee doubles before inclusion; you only pay baseFee + tip).
* On submit it prints the expected fee now vs at request time (saved / extra / same), computed on the gas limit,
  i.e. an upper bound — the fee actually charged is `gasUsed × effectiveGasPrice`. After a real broadcast it
  waits for the receipt and runs `report` automatically.

## Run it

Python 3.10+ (tested with Python 3.12).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python gas_fee_optimizer.py watch --samples 8 --interval 10
python gas_fee_optimizer.py history --hours 168 --points 30
python gas_fee_optimizer.py hold --to 0x1f3546a29eec2102c8d52e21a45114124014c85c --value 0.01 --gas-limit 21000 --deadline 10m
python gas_fee_optimizer.py hold --to <token> --from <sender> --data 0xa9059cbb… --target-gwei 9 --deadline 60s --interval 15
python gas_fee_optimizer.py report --tx <hash> --request-block <block at request time>
```

Global options: `--endpoint` (only `https://`, or `http://` to localhost), `--history-file` (default
`gas_history.json`, `''` = memory only). Inputs are validated (addresses, hex data, hashes, deadline range,
intervals, percentiles); `--broadcast` without `--raw-tx` is refused.

## What the network looked like (real data, 2026-10-14)

Pharos mainnet gas was **flat at 10 gwei** (baseFee 10 gwei, suggested tip 0) in every sample: 8 live samples
over ~75 s ([`sample_watch.txt`](sample_watch.txt)) and 30 blocks spread over the last 7 days
([`sample_history.txt`](sample_history.txt)). The docs describe the base fee as dynamic
([Gas Model](https://docs.pharos.xyz/getting-started/gas-model)), so the optimizer still matters when it moves;
today its main saving is the **tip**: many wallets pay 11–25 gwei legacy gas prices while 10 gwei is enough.

## Sample output (real runs on Pharos mainnet)

**Recent history** — `watch --samples 8 --interval 10` ([`sample_watch.txt`](sample_watch.txt)):

```
  time (UTC)                 block       baseFee        tip  eth_gasPrice   block gas used
  2026-10-14 20:37:38   20,250,467       10 gwei     0 gwei       10 gwei         0/1,000M
  ...
  2026-10-14 20:38:51   20,250,527       10 gwei     0 gwei       10 gwei         0/1,000M
  8 samples · min 10 gwei · median 10 gwei · max 10 gwei · trend ▁▁▁▁▁▁▁▁
  gas price has been FLAT at 10 gwei over this window (no variation observed).
```

**Hold until the deadline** — target 9 gwei is never reached, so it submits at the deadline
([`sample_hold_deadline.txt`](sample_hold_deadline.txt)):

```
[2026-10-14 20:39:37 UTC] REQUEST to=0xc879c018db60520f4355c26ed1a6d572cdac1815 value=0 PROS data=0xa9059cbb00000000… gasLimit=70,344 (estimate_gas +20%)
    deadline 2026-10-14 20:40:36 UTC (60 s) · favourable when baseFee+tip <= target 9 gwei · poll every 15 s
[2026-10-14 20:39:37 UTC] HOLD block #20,250,567 baseFee+tip 10 gwei > 9 gwei · 59 s to deadline
[2026-10-14 20:39:52 UTC] HOLD block #20,250,580 baseFee+tip 10 gwei > 9 gwei · 43 s to deadline
[2026-10-14 20:40:08 UTC] HOLD block #20,250,596 baseFee+tip 10 gwei > 9 gwei · 28 s to deadline
[2026-10-14 20:40:24 UTC] SUBMIT (DEADLINE: 12 s left, submitting even though 10 gwei > 9 gwei) at block #20,250,614: baseFee 10 gwei + tip 0 gwei
    request-time price 10 gwei at block #20,250,567 (2026-10-14 20:39:37 UTC)
    expected fee now  0.00070344 PROS (~$0.000411)  vs at request time 0.00070344 PROS (~$0.000411)  -> same price
    DRY RUN (prepare-only): nothing was signed or sent. Sign this with your own wallet now:
    {"chainId": 1672, "type": "0x2", "to": "0xc879c018db60520f4355c26ed1a6d572cdac1815", "value": "0x0", "data": "0xa9059cbb…0f4240", "gas": "0x112c8", "maxFeePerGas": "0x4a817c800", "maxPriorityFeePerGas": "0x0", "from": "0xf3503da2961ff5118adaa2c4ff8d9a6fb28a411f"}
```

(The held transaction is a 1 USDC `transfer` *prepared* from a large USDC holder's address only so that
`estimate_gas` can simulate it; nothing is signed or sent.)

**Favourable immediately** — p25 of the recorded history is 10 gwei, current is 10 gwei → submits at once
([`sample_hold_favourable.txt`](sample_hold_favourable.txt), which also shows the input-validation errors).

**Actual paid vs request time** — `report` on a real mined third-party transaction from the same minutes
(not ours), against the request block of the hold run above ([`sample_report.txt`](sample_report.txt)):

```
Gas report for https://www.pharosscan.xyz/tx/0xe66a35d77e41c80c9ec978b90752b3cfa5cbc0be6a994d3fbecf9f6254339e47
    mined in block #20,250,617 · status success · gasUsed 124,909
    tx fee fields: gasPrice 15 gwei (legacy)
    actual price paid  15 gwei (baseFee at inclusion 10 gwei + tip 5 gwei)
    baseFee at request block #20,250,567: 10 gwei
    actual fee 0.00187364 PROS (~$0.001093)  vs at request-time price 0.00124909 PROS (~$0.000729)  -> paid MORE by 0.00062454 PROS (~$0.000364) (+50.0% per gas)
    note: the tip of 5 gwei was above the network's suggested tip (0 gwei) — the optimizer would have used the suggested tip.
```

Fee check: the docs' [Gas Model](https://docs.pharos.xyz/getting-started/gas-model) table says fees are
"charged by gas_limit at inclusion time", so we verified on chain: for this tx and for
[0x5178bf46…](https://www.pharosscan.xyz/tx/0x5178bf468cd320f5645bb62f0ce3d811b2266fe0011f2be3f21a3428d243bc00)
the sender's balance change (`get_balance` at block N-1 vs N) equals **gasUsed × effectiveGasPrice**
(1,873,635 gwei and 472,500 gwei), not gasLimit × price — so `report` uses gasUsed.

## How it covers the task requirements

| # | Requirement | Implementation |
|---|-------------|----------------|
| 01 | Tracks gas price over time and shows recent history | `watch` (persistent `gas_history.json`, table + stats + sparkline), `history` backfill over hours/days |
| 02 | Accepts a tx with a deadline, holds until favourable or deadline | `hold --to/--value/--data --deadline` with `--target-gwei` or history percentile |
| 03 | Reports actual gas paid vs price at request time | `report` (receipt `effectiveGasPrice` × `gasUsed` vs request block/gwei, PROS + USD); runs automatically after a broadcast; dry runs print expected-now vs request-time fee |
| 04 | Never misses the deadline; submits at deadline even if gas is high | deadline check before every sleep (`interval + safety`); shown in `sample_hold_deadline.txt` |

## Limits / honesty notes

* Gas was flat during all runs, so a "gas dropped → submitted early with savings" case could not be observed live;
  the decision code path is the same comparison shown in the favourable run.
* No transaction was signed or broadcast (no spending); `--broadcast` relies on a Chain MCP endpoint with
  `send_raw_transaction` enabled and a transaction you signed. Because the fee fields are inside the signature,
  waiting saves money only when the base fee is lower at broadcast time than the signed `maxFeePerGas`.

## Security

No private key, wallet or API key. HTTPS endpoints only. License: MIT.
