# Pharos Agent Arena · Liquidation Watchdog (Morpho Blue on Pharos, Chain MCP)

Monitors **real lending positions on Pharos mainnet** and alerts *before* they reach liquidation, with a concrete
"repay X or add Y collateral" action in every alert. Lending protocol: **Morpho Blue** deployed on Pharos Pacific
mainnet (chainId 1672) at
[`0x18573fa18fd17ddfd790b4a5b5b2977aad3b4efb`](https://www.pharosscan.xyz/address/0x18573fa18fd17ddfd790b4a5b5b2977aad3b4efb)
(verified contract named "Morpho" on pharosscan; also returned by `morpho()` of the
[MorphoMarketV1AdapterV2](https://www.pharosscan.xyz/address/0xba3b260596d8412318ee6ef5476291f6c8e3b68a) of a USDC vault).
All chain data comes from the hosted **Pharos Chain MCP** (`https://mcp.pharos.xyz/mcp`), read-only.

Built for the Pharos *Season of AI · Agent Arena* task
[Liquidation Watchdog](https://pharos-agent-arena.vercel.app/tasks/liquidation-watchdog).

## What it reads (requirement 01)

Per configured `(market id, borrower)`, every value pinned to the same block via `eth_call`:

| Call | Gives |
|---|---|
| `Morpho.idToMarketParams(id)` | loan token, collateral token, oracle, IRM, LLTV |
| `Morpho.market(id)` | total borrow assets/shares, `lastUpdate` |
| `Morpho.position(id, borrower)` | borrow shares, **collateral** |
| `IRM.borrowRateView(params, market)` | per-second borrow rate → interest accrued since `lastUpdate` (Morpho `wTaylorCompounded`) and APR |
| `Oracle.price()` | collateral price in loan-token units, scaled 1e36 (Morpho oracle convention) |
| `symbol()` / `decimals()` | token labels and units |

Then: **debt** = borrowShares × totalBorrowAssets′ / totalBorrowShares (rounded up, with Morpho's virtual shares),
collateral value = collateral × price / 1e36, **health factor** = collateral value × LLTV / debt (Morpho liquidates
below 1.0), liquidation price of the collateral and the % drop that would trigger it.

## Alerts (requirements 02–04)

* **Thresholds** — `--warn` (default 1.3) and `--critical` (default 1.1); levels `OK`, `WARNING`, `CRITICAL`,
  `LIQUIDATABLE` (< 1.0), `NO_DEBT`. Validated: `1.0 < critical < warn < target-hf`.
* **Channel** — console lines prefixed with a **UTC ISO timestamp** `[YYYY-MM-DDTHH:MM:SSZ]`, and every
  WARNING/CRITICAL/LIQUIDATABLE reading is appended as a JSON line to `--alert-log` (default `alerts.jsonl`)
  so another tool can pick it up. (Telegram/Discord/email are not implemented.)
* **Action** — each alert says `ACTION: repay <X> <loan token> OR add <Y> <collateral token> collateral to bring HF
  back to <--target-hf>` (default 1.5), computed exactly:
  repay = debt − collateralValue × LLTV / target; add = (target × debt / LLTV − collateralValue) / price.
  Healthy positions show the same numbers as information only.
* `--follow` re-polls every `--interval` seconds (≥ 10) and after the first poll prints only alerting positions or
  level changes; `--polls N` stops after N polls.
* `--price-shock P` is a clearly-labelled **what-if**: every oracle price is lowered by P % (positions themselves
  are still real) to show how alerts escalate.

## Configuration — [`positions.json`](positions.json)

8 real borrow positions in 6 Morpho markets on Pharos mainnet, found on 2026-10-14 by reading the receipts of
the transactions sent to the Morpho contract (pharosscan explorer list) and then `position()` for each
(market, address) pair; positions with debt were kept. Single position from the CLI:
`--market <bytes32 id> --borrower <0x…>`. Market ids must be `0x` + 64 hex, addresses are validated.

## Run it

Python 3.10+ (tested with Python 3.12).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt            # requests, pycryptodome (keccak for selectors)

python liquidation_watchdog.py                                   # one check, defaults warn 1.3 / critical 1.1
python liquidation_watchdog.py --follow --interval 60            # keep watching
python liquidation_watchdog.py --warn 1.5 --critical 1.2 --target-hf 2.0
python liquidation_watchdog.py --market 0xb3b8a31c8a1652cf31eb872c7778716edc39471ed82bcad99b3f5c83a449e1ba \
                               --borrower 0xd27cb53f5920d484261cc14d32094d3f043265c9
python liquidation_watchdog.py --price-shock 30                  # what-if: all oracle prices −30 %
```

Other options: `--config`, `--alert-log ''` (disable file), `--json FILE` (last poll's readings), `--endpoint`
(only `https://`, or `http://` to localhost). Cost: ~6 `eth_call`s per position per poll (+ token/param
look-ups once); a poll of 8 positions takes ~20 s.

## Sample output (real runs on Pharos mainnet, 2026-10-14 ~20:30 UTC)

[`sample_output.txt`](sample_output.txt) (+ [`sample_output.json`](sample_output.json), alerts in
[`sample_alerts.jsonl`](sample_alerts.jsonl)) — one check with default thresholds: **4 CRITICAL, 4 OK**.
The four critical ones are real positions in the wsrUSD/USDC and syzUSD/USDC markets (LLTV 91.5 %) with HF
1.007–1.021, i.e. 0.7–2.1 % away from liquidation (most likely leveraged yield-stablecoin loops — our inference, not verified):

```
=== poll 1 · Pharos mainnet block #20,250,159 (2026-10-14 20:32:13 UTC) · Morpho 0x18573fa18fd17ddfd790b4a5b5b2977aad3b4efb
[2026-10-14T20:32:20Z] OK           WPROS→USDC borrower  WPROS/USDC market 0xb3b8a31c…  borrower 0xd27cb53f5920d484261cc14d32094d3f043265c9
    HF 2.5867  (warn < 1.3, critical < 1.1, liquidation < 1.0; LLTV 38.5%)
    collateral 11,499,995.0065 WPROS ≈ 6,715,901.31 USDC   debt 999,571.3992 USDC   borrow APR 4.83%
    WPROS price 0.583992 USDC; liquidation at 0.225764 USDC (61.3% lower)
    healthy: no action needed (HF already ≥ target 1.5)
[2026-10-14T20:32:23Z] CRITICAL     wsrUSD→USDC large  wsrUSD/USDC market 0xbea21c37…  borrower 0x086eb9c2b14dd657ae77d07df9115a2f946ce327
    HF 1.0214  (warn < 1.3, critical < 1.1, liquidation < 1.0; LLTV 91.5%)
    collateral 5,558,838.5131 wsrUSD ≈ 6,131,369.42 USDC   debt 5,492,878.3826 USDC   borrow APR 4.78%
    wsrUSD price 1.102995 USDC; liquidation at 1.079928 USDC (2.1% lower)
    ACTION: repay 1,752,743.04 USDC OR add 2,605,043.5603 wsrUSD collateral to bring HF back to 1.5
    borrower https://www.pharosscan.xyz/address/0x086eb9c2b14dd657ae77d07df9115a2f946ce327  ·  Morpho https://www.pharosscan.xyz/address/0x18573fa18fd17ddfd790b4a5b5b2977aad3b4efb
...
--- summary: CRITICAL 4, OK 4
```

(Debt grows every block with interest, so re-runs show slightly larger debt. The WPROS price 0.583992 USDC equals the Chainlink PROS/USD feed value read at the same time.)

* [`sample_output_follow.txt`](sample_output_follow.txt) — `--follow --interval 10 --polls 3`: three polls on
  consecutive blocks; polls 2–3 repeat only the CRITICAL positions.
* [`sample_output_whatif.txt`](sample_output_whatif.txt) — `--price-shock 30 --warn 1.8 --critical 1.4 --target-hf 2.5`:
  custom thresholds and a −30 % price what-if → 5 LIQUIDATABLE, 1 CRITICAL, 2 OK, each with its action; plus the
  input-validation errors (bad market id, inverted thresholds).

## How it covers the task requirements

| # | Requirement | Implementation |
|---|-------------|----------------|
| 01 | Reads collateral, debt and health factor from chain | `position()`, `market()`, `idToMarketParams()`, IRM `borrowRateView()`, oracle `price()` via Chain MCP `eth_call`; HF computed with Morpho's formulas |
| 02 | Configurable warning thresholds (e.g. 1.3 / 1.1) | `--warn 1.3 --critical 1.1` (defaults), `--target-hf`, validated ordering |
| 03 | Alerts through at least one channel (console with timestamps) | timestamped console alerts + JSON-lines alert log |
| 04 | Concrete action in each alert | `ACTION: repay X USDC OR add Y <collateral> collateral to bring HF back to 1.5` |

## Limits / honesty notes

* Only Morpho Blue is supported (the lending protocol found live on Pharos mainnet). Morpho's public API does not
  support chainId 1672, so HF values could not be cross-checked against an official UI; they follow the Morpho
  Blue contract math (`MorphoBalancesLib`, `SharesMathLib` virtual shares, oracle 1e36 scale).
* The IRM is called for the accrued-interest view; if a market's IRM were the zero address, no interest is added.
* Positions are not auto-discovered by the tool; the bundled list was discovered once as described above.

## Security

Read-only: no wallet, private key or API key; never signs or sends transactions. HTTPS endpoints only.

License: MIT.
