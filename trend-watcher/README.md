# Pharos Agent Arena · Trend Watcher (SkillEngine skill with memory, Chain MCP)

A Skill with **memory** for Pharos Pacific mainnet (chainId 1672). Each run reads one on-chain value through the
hosted **Pharos Chain MCP** (`https://mcp.pharos.xyz/mcp`) — the WPROS/USDC pool price, Chainlink PROS/USD, a
Morpho Blue market's utilization or liquidity, an ERC-20 balance — and stores it in **SQLite** keyed by
(series, block). It acts **only once a trend is confirmed over several observations** ("price down 3 checks in a
row", "utilization above 90 % for 3 checks"), never on a single noisy reading. Restarts, re-runs and overlapping
backfills can neither lose nor duplicate history, and a confirmed trend triggers exactly once.

The action is an alert (JSONL) and optionally an **unsigned** ERC-20 transfer (pre-checked via Chain MCP). Nothing is
ever signed or broadcast; signing is left to the user (`cast send … --private-key $PRIVATE_KEY` with the official
[pharos-skill-engine](https://github.com/PharosNetwork/pharos-skill-engine), or any wallet).

Built for the Pharos *Season of AI · Agent Arena* task
[Trend Watcher](https://pharos-agent-arena.vercel.app/tasks/trend-watcher) (Hard).

## Requirements → where they are met

| # | Requirement | How |
|---|---|---|
| 1 | State stored between runs (persistent, not in-memory) | SQLite file (`--db`, WAL): `observations` (series, block, block time, value, source, mode) and `triggers`. Every `observe`/`watch`/`backfill` run is a separate process using the same file. |
| 2 | Trend rule needs ≥ 3 observations and is configurable | JSON config: `consecutive` (`moves` ≥ 2 → ≥ 3 obs, direction, `min_step_pct`, `max_step_pct`, `min_total_pct`, `max_gap_blocks`) or `sustained` (`op`, `threshold`, `observations` ≥ 3). Fewer is rejected at load (unit test). |
| 3 | Restarts without losing or duplicating history | primary key (series, block) + `INSERT OR IGNORE`, one transaction per observation; triggers keyed (series, rule, block). `samples/04`: backfill killed with SIGKILL after 12 s, re-run → 74 rows = 74 distinct blocks, contiguous every 600 blocks; `samples/01` and `03`: re-runs/overlaps add 0 duplicates; already-recorded triggers are not re-fired. |
| 4 | Shows stored history and the exact moment the trend was met | `history` prints every stored observation with move %, streak counter and `<== TRIGGER` at the observation (block + UTC time) that completed the trend; `events` lists the recorded triggers; live runs print `TREND CONFIRMED [LIVE] at block …`. |
| 5 | A single spike does not trigger | `samples/01`: the real one-block −1.42 % drop at block 20,228,606 (a 4,356 USDC swap) gives streak 1 and resets — no trigger, while `--compare-single 0.5` marks where a one-reading rule would have fired. `max_step_pct` also treats any bigger one-step jump as an outlier. |

## Skill format

```
SKILL.md                     agent entry point (official pharos-skill-engine frontmatter)
references/trend.md          config, sources, rules, triggers, storage, source sanity check
assets/networks.json         official pharos-skill-engine network file (unchanged copy)
assets/wpros_down.json       pool price: 3 consecutive declines (each 0.02–2 %, total >= 0.15 %), alert
assets/spike_demo.json       same rule on a per-block series around a real price spike
assets/morpho_util_high.json Morpho WPROS/USDC utilization > 90 % for 3 observations -> prepare 100 USDC top-up
scripts/trend.py             the skill (observe / watch / backfill / history / events)
scripts/skill_chain.py, pay_core.py, pharos_mcp.py
samples/                     real runs, incl. the SQLite state files and alerts.jsonl     tests/
```

## Run it

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 scripts/trend.py observe --config assets/wpros_down.json          # cron: */10 * * * *
python3 scripts/trend.py watch   --config assets/morpho_util_high.json --interval 60 --count 5
python3 scripts/trend.py backfill --config assets/wpros_down.json --from-block 20215200 --to-block 20259000 --every 600
python3 scripts/trend.py history --config assets/wpros_down.json --compare-single 0.5
python3 -m unittest discover -s tests
```

## Real sample output (Pharos mainnet, 2026-10-14)

**Single spike → no trigger** (`samples/01_single_spike_no_trigger.txt`, every block 20,228,598–616):

```
 20,228,605 2026-10-14 14:14:17        0.585025   +0.0000      0  backfill
 20,228,606 2026-10-14 14:14:18        0.576739   -1.4163      1  backfill  [re-read confirmed the 1.42% jump]   (a single-reading rule |move| >= 0.5% would fire here)
 20,228,607 2026-10-14 14:14:19        0.576739   +0.0000      0  backfill  [re-read: first answer 0.644557 rejected, 0.576739 confirmed]
 20,228,608 2026-10-14 14:14:20        0.578822   +0.3611      0  backfill
 20,228,609 2026-10-14 14:14:21        0.580912   +0.3611      0  backfill
recorded triggers: 0
```

**History with confirmed trends** (`samples/07_history_pool.txt`, 74 backfilled observations every 600 blocks
≈ 10 min from 12:00 to 23:12 UTC + 3 live ones):

```
 20,221,200 2026-10-14 12:10:45        0.602334   +0.2011      0  backfill
 20,221,800 2026-10-14 12:21:08        0.597898   -0.7365      1  backfill    (a single-reading rule |move| >= 0.5% would fire here)
 20,222,400 2026-10-14 12:31:10        0.592420   -0.9162      2  backfill    (a single-reading rule |move| >= 0.5% would fire here)
 20,223,000 2026-10-14 12:40:41        0.591144   -0.2153      3  backfill    <== TRIGGER
 ...
recorded triggers: 3
  block 20,223,000 (2026-10-14 12:40:41 UTC) historical: 3 consecutive down moves 0.602334 -> 0.591144 (-1.8577%) from block 20221200 to 20223000
  block 20,229,000 (2026-10-14 14:20:31 UTC) historical: 3 consecutive down moves 0.592326 -> 0.581004 (-1.9115%) from block 20227200 to 20229000
  block 20,248,200 (2026-10-14 19:55:11 UTC) historical: 3 consecutive down moves 0.587569 -> 0.583071 (-0.7656%) from block 20246400 to 20248200
```

**Kill and restart** (`samples/04_kill_and_restart.txt`):

```
stored before: rows=48 distinct_blocks=48 first=20215200 last=20243400
$ timeout -s KILL 12 python3 scripts/trend.py backfill ... --from-block 20243400 --to-block 20259000 --every 600
exit code: 137 (137 = killed by SIGKILL)
stored after the kill: rows=53 distinct_blocks=53 first=20215200 last=20246400
$ python3 scripts/trend.py backfill ... --from-block 20243400 --to-block 20259000 --every 600
backfill 20,243,400..20,259,000 every 600: 21 new observations (6 already stored)
*** TREND CONFIRMED [historical (recorded, no action)] at block 20,248,200 (2026-10-14 19:55:11 UTC): 3 consecutive down moves ...
stored after the restart: rows=74 distinct_blocks=74 first=20215200 last=20259000
```

**Live trigger with action** (`samples/05_live_sustained_trigger.txt`, 4 live observations 25 s apart):

```
[2026-10-14 23:15:48 UTC] block 20,259,190 value 91.024391 move n/a streak 1/3 stored; history 1 obs
[2026-10-14 23:16:15 UTC] block 20,259,211 value 91.024391 move n/a streak 2/3 stored; history 2 obs
[2026-10-14 23:16:41 UTC] block 20,259,237 value 91.024391 move n/a streak 3/3 stored; history 3 obs
*** TREND CONFIRMED [LIVE] at block 20,259,237 (2026-10-14 23:16:38 UTC): value gt 90 for 3 consecutive observations (blocks 20259190..20259237)
    action: alert written to samples/alerts.jsonl
=== Prepared action: top-up 100 USDC treasury -> ops (review before signing) ===
  ...
[2026-10-14 23:17:11 UTC] block 20,259,261 value 91.024391 move n/a streak 4 (>= 3, already triggered) stored; history 4 obs
```

(The Morpho market's utilization is constant between interactions — Morpho updates its totals only when someone
touches the market.) `samples/06_live_pool_watch.txt` shows 3 live pool observations appended to the 74-row
history (no trend → no action).

The state files `samples/trend_state.db`, `samples/spike_state.db` and `samples/alerts.jsonl` are the real ones from
these runs. The prepared transfer (real public addresses as examples) was never signed or sent.

Proof (the real swap behind the single-spike demo):
https://www.pharosscan.xyz/tx/0xe8300396bcc84e07b57c4022392af81c3ddc62d48cc025baebe82c284df24c31

## Notes and limits

* **Source glitches.** In these runs the hosted endpoint returned 5 inconsistent archive `slot0` answers (0.644557
  or 0.384643 instead of ~0.58; `cast call … -b <block>` on https://rpc.pharos.xyz gives the normal value). The
  watcher re-reads any jump larger than `recheck_pct` and keeps the value two reads agree on; the rejected answer is
  noted in the row (see samples/01 and 07). Real moves survive the re-read.
* Live triggers act; triggers found while backfilling are recorded as historical only.
* Cadence is whatever schedules `observe` (cron, a scheduler, or `watch`); the gap guard (`max_gap_blocks`) stops
  a trend from forming across a long outage.

License: MIT
