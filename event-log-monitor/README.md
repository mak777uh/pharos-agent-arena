# Pharos Agent Arena · Event/Log Monitor (Chain MCP)

A continuously running monitor that watches a Pharos contract for one event through the
**Pharos Chain MCP server's `get_logs` tool** (filtered by contract address **and** event-signature
topic) and reports each **new** occurrence once, decoded into readable fields.

Built for the Pharos *Season of AI · Agent Arena* task
[Event/Log Monitor](https://pharos-agent-arena.vercel.app/tasks/event-log-monitor).

## What it watches

Default: `Transfer(address,address,uint256)` events of **USDC (Circle-deployed) on Pharos Pacific
mainnet** (chainId 1672), `0xc879c018db60520f4355c26ed1a6d572cdac1815`
([token registry](https://docs.pharos.xyz/getting-started/token-registry) ·
[explorer](https://www.pharosscan.xyz/address/0xc879c018db60520f4355c26ed1a6d572cdac1815)) —
an active contract, so live events show up within seconds.
`--address` watches any other ERC-20; `--event Approval` switches to `Approval(address,address,uint256)`.

## How it works

* **Chain MCP** — talks to `https://mcp.pharos.xyz/mcp`
  ([PharosNetwork/pharos-mcp](https://github.com/PharosNetwork/pharos-mcp)) over MCP Streamable HTTP
  (`initialize` → `notifications/initialized` → `tools/list` → `tools/call`), client in `pharos_mcp.py`.
  Tools used: `get_block_number` (chain head), `get_logs` (the event query),
  `eth_call` (`symbol()` / `decimals()` once at start for formatting).
* **Filter** — every `get_logs` call passes `address=<contract>` and `topics=[topic0]`, where
  `topic0 = keccak256("Transfer(address,address,uint256)") = 0xddf252ad…b3ef`. Returned logs are
  re-checked against both before being reported.
* **Continuous, block by block** — a cursor walks `[next_block, head − confirmations]`, in chunks of at
  most 1000 blocks (the server's `get_logs` limit), then sleeps `--interval` seconds (default 3) and polls again.
  By default it starts at the current head, so only events that happen after start-up are reported.
* **No duplicates** — every log is keyed by `(transactionHash, logIndex)`; a key that was already
  reported is skipped, so re-scanned or overlapping ranges never print an event twice. Logs flagged
  `removed` are ignored. With `--state-file` the cursor and the recent keys are saved after every
  chunk, so a restarted monitor resumes instead of re-reporting.
* **Decoding** — `from` / `to` from topics 1–2, `amount` from `data` with the token's decimals applied
  (raw value kept too), plus tx hash, log index, block number, block time and an explorer link.
  Transfers from/to the zero address are tagged `mint` / `burn`.
* Transient MCP/network errors are printed to stderr and the poll is retried; HTTP 429 honours
  `Retry-After`. Ctrl+C / SIGTERM stop it cleanly.

## How it covers the task requirements

1. **Chain MCP log queries filtered by contract address and event signature** — `get_logs` with
   `address` + `topics=[topic0]`.
2. **Runs continuously, only new events, no duplicates** — polling loop with a block cursor and a
   `(txHash, logIndex)` seen-set (optionally persisted). Proven below: a restart that rewound the cursor 80
   blocks over already-reported blocks printed none of them again.
3. **Decoded readable fields** — from, to, amount (decimals applied), tx hash, block (+ time, link).
4. **Caught live events** — real Pharos mainnet USDC transfers below, e.g. tx
   [`0x3e1168df…1a2a`](https://www.pharosscan.xyz/tx/0x3e1168df397e9da275e75329093106eee1ea938549be3d18a53b44f949ee1a2a)
   in block 20,223,935 (133.32 USDC).

## Run it

Python 3.10+ (tested with Python 3.12.15).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python event_log_monitor.py                                   # follow new USDC Transfer events forever
python event_log_monitor.py --from-block -200                 # first replay the last 200 blocks, then follow
python event_log_monitor.py --state-file state.json           # resume after restart without repeats
python event_log_monitor.py --jsonl events.jsonl              # also append decoded events as JSON lines
python event_log_monitor.py --event Approval --address 0x52c48d4213107b20bc583832b0d951fb9ca8f0b0
python event_log_monitor.py --duration 60                     # stop after 60 s (demo); --max-events N also works
```

All options: `--address`, `--event {Approval,Transfer}`, `--from-block`, `--interval`, `--confirmations`,
`--state-file`, `--jsonl`, `--max-events`, `--duration`, `--endpoint` (default `https://mcp.pharos.xyz/mcp`).

## Sample output (real runs on Pharos mainnet, 2026-10-14)

### 1. Live run — `python event_log_monitor.py --duration 60 --state-file st.json`
(saved as [`sample_output.txt`](sample_output.txt))

```
Pharos event monitor · server pharos-mcp-server 0.1.0 @ https://mcp.pharos.xyz/mcp
  contract : 0xc879c018db60520f4355c26ed1a6d572cdac1815 (USDC, 6 decimals)  https://www.pharosscan.xyz/address/0xc879c018db60520f4355c26ed1a6d572cdac1815
  event    : Transfer(address,address,uint256)  topic0 0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef
  head     : #20,223,876   watching from block #20,223,877   (Ctrl+C to stop)

[NEW Transfer [transfer]] block #20,223,881  2026-10-14 12:56:42 UTC
    from   : 0x912c9ade24d44d8922f0866d8dcb079f1363f647
    to     : 0xa5ca5fbe34e444f366b373170541ec6902b0f75c
    amount : 50.143830 USDC  (raw 50143830)
    tx     : 0xd0717be7d655fce62cb697af2994bf75aa003865312f07485200c17da350fd14  (log #1)
    link   : https://www.pharosscan.xyz/tx/0xd0717be7d655fce62cb697af2994bf75aa003865312f07485200c17da350fd14
[NEW Transfer [transfer]] block #20,223,881  2026-10-14 12:56:42 UTC
    from   : 0xa5ca5fbe34e444f366b373170541ec6902b0f75c
    to     : 0x903cf528c0c54ecb99991a69e0e095589917a0ce
    amount : 0.075216 USDC  (raw 75216)
    tx     : 0xd0717be7d655fce62cb697af2994bf75aa003865312f07485200c17da350fd14  (log #4)
    link   : https://www.pharosscan.xyz/tx/0xd0717be7d655fce62cb697af2994bf75aa003865312f07485200c17da350fd14
[NEW Transfer [transfer]] block #20,223,881  2026-10-14 12:56:42 UTC
    from   : 0xa5ca5fbe34e444f366b373170541ec6902b0f75c
    to     : 0x9ea92f9949e768b2985036409d5d6b815d2ee239
    amount : 50.068614 USDC  (raw 50068614)
    tx     : 0xd0717be7d655fce62cb697af2994bf75aa003865312f07485200c17da350fd14  (log #6)
    link   : https://www.pharosscan.xyz/tx/0xd0717be7d655fce62cb697af2994bf75aa003865312f07485200c17da350fd14
[NEW Transfer [transfer]] block #20,223,922  2026-10-14 12:57:22 UTC
    from   : 0x912c9ade24d44d8922f0866d8dcb079f1363f647
    to     : 0xa5ca5fbe34e444f366b373170541ec6902b0f75c
    amount : 50.135662 USDC  (raw 50135662)
    tx     : 0x007840a972049475c8a2d0f97077d90525426011fe1ddcb7d9ca4bf52ffe6fca  (log #1)
    link   : https://www.pharosscan.xyz/tx/0x007840a972049475c8a2d0f97077d90525426011fe1ddcb7d9ca4bf52ffe6fca
[NEW Transfer [transfer]] block #20,223,922  2026-10-14 12:57:22 UTC
    from   : 0xa5ca5fbe34e444f366b373170541ec6902b0f75c
    to     : 0x903cf528c0c54ecb99991a69e0e095589917a0ce
    amount : 0.075204 USDC  (raw 75204)
    tx     : 0x007840a972049475c8a2d0f97077d90525426011fe1ddcb7d9ca4bf52ffe6fca  (log #4)
    link   : https://www.pharosscan.xyz/tx/0x007840a972049475c8a2d0f97077d90525426011fe1ddcb7d9ca4bf52ffe6fca
[NEW Transfer [transfer]] block #20,223,922  2026-10-14 12:57:22 UTC
    from   : 0xa5ca5fbe34e444f366b373170541ec6902b0f75c
    to     : 0x9ea92f9949e768b2985036409d5d6b815d2ee239
    amount : 50.060458 USDC  (raw 50060458)
    tx     : 0x007840a972049475c8a2d0f97077d90525426011fe1ddcb7d9ca4bf52ffe6fca  (log #6)
    link   : https://www.pharosscan.xyz/tx/0x007840a972049475c8a2d0f97077d90525426011fe1ddcb7d9ca4bf52ffe6fca
[NEW Transfer [transfer]] block #20,223,935  2026-10-14 12:57:36 UTC
    from   : 0x3a0feb761c0daf7548341e9e44f6c7cd8081f4d1
    to     : 0x768b959191ce5876cabbbc014a08d72615bdde54
    amount : 133.320000 USDC  (raw 133320000)
    tx     : 0x3e1168df397e9da275e75329093106eee1ea938549be3d18a53b44f949ee1a2a  (log #0)
    link   : https://www.pharosscan.xyz/tx/0x3e1168df397e9da275e75329093106eee1ea938549be3d18a53b44f949ee1a2a

Stopped. 7 new event(s) reported over 19 poll(s); cursor at block #20,223,940.
```

### 2. Duplicate check after restart
The state file from run 1 (cursor at block 20,223,940, 7 reported keys) was edited to move the cursor
back 80 blocks to 20,223,860, then `python event_log_monitor.py --duration 20 --state-file st2.json` was run.
Blocks 20,223,860–20,223,940 were scanned again, but the 7 events of run 1 (blocks 20,223,881 /
20,223,922 / 20,223,935) were **not** printed again; only events that had not been reported before
(blocks 20,223,861 and 20,223,868, before run 1 started, and new blocks after it) appear.
(Saved as [`sample_output_restart_dedup.txt`](sample_output_restart_dedup.txt).)

```
Pharos event monitor · server pharos-mcp-server 0.1.0 @ https://mcp.pharos.xyz/mcp
  contract : 0xc879c018db60520f4355c26ed1a6d572cdac1815 (USDC, 6 decimals)  https://www.pharosscan.xyz/address/0xc879c018db60520f4355c26ed1a6d572cdac1815
  event    : Transfer(address,address,uint256)  topic0 0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef
  head     : #20,223,952   watching from block #20,223,860   (Ctrl+C to stop)

[NEW Transfer [transfer]] block #20,223,861  2026-10-14 12:56:24 UTC
    from   : 0x4146d192da6428c9e1c243d2a953c625b5765623
    to     : 0xa5ca5fbe34e444f366b373170541ec6902b0f75c
    amount : 50.132483 USDC  (raw 50132483)
    tx     : 0x180ae2ce560ed65f5b3bf463e138f706f6a81e1da230b370f7e087df8edb7cc0  (log #1)
    link   : https://www.pharosscan.xyz/tx/0x180ae2ce560ed65f5b3bf463e138f706f6a81e1da230b370f7e087df8edb7cc0
[NEW Transfer [transfer]] block #20,223,861  2026-10-14 12:56:24 UTC
    from   : 0xa5ca5fbe34e444f366b373170541ec6902b0f75c
    to     : 0x903cf528c0c54ecb99991a69e0e095589917a0ce
    amount : 0.075198 USDC  (raw 75198)
    tx     : 0x180ae2ce560ed65f5b3bf463e138f706f6a81e1da230b370f7e087df8edb7cc0  (log #4)
    link   : https://www.pharosscan.xyz/tx/0x180ae2ce560ed65f5b3bf463e138f706f6a81e1da230b370f7e087df8edb7cc0
[NEW Transfer [transfer]] block #20,223,861  2026-10-14 12:56:24 UTC
    from   : 0xa5ca5fbe34e444f366b373170541ec6902b0f75c
    to     : 0x9ea92f9949e768b2985036409d5d6b815d2ee239
    amount : 50.057285 USDC  (raw 50057285)
    tx     : 0x180ae2ce560ed65f5b3bf463e138f706f6a81e1da230b370f7e087df8edb7cc0  (log #5)
    link   : https://www.pharosscan.xyz/tx/0x180ae2ce560ed65f5b3bf463e138f706f6a81e1da230b370f7e087df8edb7cc0
[NEW Transfer [transfer]] block #20,223,868  2026-10-14 12:56:31 UTC
    from   : 0x4146d192da6428c9e1c243d2a953c625b5765623
    to     : 0xa5ca5fbe34e444f366b373170541ec6902b0f75c
    amount : 50.110021 USDC  (raw 50110021)
    tx     : 0x0164d85439fc79fe272dabfdee6009929ebb4f67f837ded8b2c8ab0cab9da1f5  (log #1)
    link   : https://www.pharosscan.xyz/tx/0x0164d85439fc79fe272dabfdee6009929ebb4f67f837ded8b2c8ab0cab9da1f5
[NEW Transfer [transfer]] block #20,223,868  2026-10-14 12:56:31 UTC
    from   : 0xa5ca5fbe34e444f366b373170541ec6902b0f75c
    to     : 0x903cf528c0c54ecb99991a69e0e095589917a0ce
    amount : 0.075165 USDC  (raw 75165)
    tx     : 0x0164d85439fc79fe272dabfdee6009929ebb4f67f837ded8b2c8ab0cab9da1f5  (log #4)
    link   : https://www.pharosscan.xyz/tx/0x0164d85439fc79fe272dabfdee6009929ebb4f67f837ded8b2c8ab0cab9da1f5
[NEW Transfer [transfer]] block #20,223,868  2026-10-14 12:56:31 UTC
    from   : 0xa5ca5fbe34e444f366b373170541ec6902b0f75c
    to     : 0x9ea92f9949e768b2985036409d5d6b815d2ee239
    amount : 50.034856 USDC  (raw 50034856)
    tx     : 0x0164d85439fc79fe272dabfdee6009929ebb4f67f837ded8b2c8ab0cab9da1f5  (log #5)
    link   : https://www.pharosscan.xyz/tx/0x0164d85439fc79fe272dabfdee6009929ebb4f67f837ded8b2c8ab0cab9da1f5
[NEW Transfer [transfer]] block #20,223,954  2026-10-14 12:57:52 UTC
    from   : 0x552818be421fa69f7233652c1f02a2e3af438f89
    to     : 0x32d839c194428732b41f92c6e10f361ed8157707
    amount : 0.102837 USDC  (raw 102837)
    tx     : 0x12b5794f41050ed86c5d0feee6062b49364959432bfce1a33803a74abb4fa746  (log #1)
    link   : https://www.pharosscan.xyz/tx/0x12b5794f41050ed86c5d0feee6062b49364959432bfce1a33803a74abb4fa746
[NEW Transfer [transfer]] block #20,223,954  2026-10-14 12:57:52 UTC
    from   : 0x552818be421fa69f7233652c1f02a2e3af438f89
    to     : 0xa43765a47b9918e4381fc8ccb03b70fb17d19489
    amount : 0.000022 USDC  (raw 22)
    tx     : 0x12b5794f41050ed86c5d0feee6062b49364959432bfce1a33803a74abb4fa746  (log #4)
    link   : https://www.pharosscan.xyz/tx/0x12b5794f41050ed86c5d0feee6062b49364959432bfce1a33803a74abb4fa746
[NEW Transfer [transfer]] block #20,223,963  2026-10-14 12:58:02 UTC
    from   : 0x768b959191ce5876cabbbc014a08d72615bdde54
    to     : 0x3a0feb761c0daf7548341e9e44f6c7cd8081f4d1
    amount : 133.320000 USDC  (raw 133320000)
    tx     : 0xfbd6de4361f07a8c0d470ac2544a47d2becdf762ee1b821a9b9802d16dc2830b  (log #0)
    link   : https://www.pharosscan.xyz/tx/0xfbd6de4361f07a8c0d470ac2544a47d2becdf762ee1b821a9b9802d16dc2830b
[NEW Transfer [transfer]] block #20,223,968  2026-10-14 12:58:07 UTC
    from   : 0x3a0feb761c0daf7548341e9e44f6c7cd8081f4d1
    to     : 0xcd7371e3e5956d3e9a2a7bf0ced44d0ed172d396
    amount : 600.000000 USDC  (raw 600000000)
    tx     : 0xbc2046aea764207f89a2a411429de3ee02a95c3db3782bab8c242a6a199be522  (log #0)
    link   : https://www.pharosscan.xyz/tx/0xbc2046aea764207f89a2a411429de3ee02a95c3db3782bab8c242a6a199be522

Stopped. 10 new event(s) reported over 7 poll(s); cursor at block #20,223,974.
```

## Security notes

* Read-only: no private key, no signing, no transactions; no API keys or secrets.
* Inputs are validated: the address must be 20-byte hex; the MCP endpoint must be `https://`
  (plain `http://` only for `localhost` when self-hosting the MCP server).
* The state file is written atomically (temp file + rename) and holds only a block number and
  `txHash:logIndex` keys.

## Files

* `event_log_monitor.py` — the monitor.
* `pharos_mcp.py` — minimal Chain MCP client (session handling, SSE parsing, 429 retry, input checks).
* `sample_output.txt`, `sample_output_restart_dedup.txt` — real outputs.

License: MIT
