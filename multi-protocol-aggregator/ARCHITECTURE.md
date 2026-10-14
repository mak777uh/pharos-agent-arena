# Architecture — Pharos multi-protocol aggregator

```
                ┌──────────────────────────────────────────────────────────────┐
  user  ──────▶ │ aggregator.py  (one interface)                               │
  CLI / "ask"   │  rates · positions · deposit · withdraw · ask "<plain text>" │
                │  parse_ask(): regex intents, refuses anything else            │
                └───────────────┬──────────────────────────────┬───────────────┘
                                │ queries                       │ actions
                                ▼                               ▼
                ┌──────────────────────────────┐  ┌───────────────────────────────────┐
                │ Aggregator                    │  │ Router                            │
                │  venues(token) → discover()   │  │  deposit: best post-deposit APY   │
                │  status(): up / UNAVAILABLE   │  │   among eligible venues           │
                │  positions(wallet)            │  │  withdraw: lowest-APY holding     │
                └───────────────┬──────────────┘  │   first, simulated, split if      │
                                │                  │   needed                          │
                                ▼                  └───────────────┬───────────────────┘
        ┌─────────────────────────────────────────────────────────┴─────────────────────┐
        │ venues.py — one adapter interface (Venue)                                      │
        │  load() · position() · apy_after() · fees() · build_deposit() · build_withdraw()│
        │  ┌────────────────┐   ┌─────────────────────┐   ┌──────────────────────────┐   │
        │  │ AvalonReserve  │   │ MorphoMarket (×N)   │   │ Erc4626Vault (×N)        │   │
        │  │ Aave-V3 fork   │   │ Morpho Blue         │   │ SharpByte, Vishwa, --vault│  │
        │  └────────────────┘   └─────────────────────┘   └──────────────────────────┘   │
        │  discover(): each protocol / venue loaded inside its own try/except            │
        │  → (venues, unavailable[])                                                      │
        └───────────────┬──────────────────────────────────────────────┬────────────────┘
                        ▼                                              ▼
        ┌──────────────────────────────────┐          ┌────────────────────────────────┐
        │ Chain (one pinned block)          │          │ PharosExplorer (optional)      │
        │  Pharos Chain MCP, HTTPS          │          │  public Pharosscan index:       │
        │  eth_call · get_block · get_gas_  │          │  recent gasUsed per method      │
        │  price · estimate_gas · eth_call  │          └────────────────────────────────┘
        │  "from" = wallet for simulation   │
        └──────────────────────────────────┘
                        │
                        ▼
              unsigned tx JSON (chainId 1672, from, to, data, value 0)
              → the user signs and sends in their own wallet
```

## Components

* **`aggregator.py`** — the single interface. Sub-commands `rates`, `positions`, `deposit`, `withdraw`, and `ask`
  (plain English → one of those; unknown requests are refused with the list of supported ones, never guessed).
* **`Aggregator`** — caches the venue list per token, aggregates positions across protocols, reports protocol
  status (`up (n venues)` / `UNAVAILABLE — reason`) with every answer.
* **Router** (`route_deposit`, `route_withdraw`):
  * deposit → filters venues (APY known, size ≥ `--min-tvl-usd`, cap room ≥ amount, not `--exclude`d), ranks by the
    **APY expected after this deposit** (`apy_after`: pool venues lose utilisation; vaults dilute), prints *why*
    (winner's rate and basis, the runner-up and the $/year difference, capacity, venue facts) and the rejected
    venues with reasons; checks wallet balance and existing allowance (skips the approve and simulates the deposit
    itself when the allowance already covers it).
  * withdraw → reads every position, caps each by what can leave now (pool cash), takes from the **lowest-APY**
    venue first so the best earners stay invested, simulates each leg with `eth_call` from the wallet, skips legs
    that would revert, splits across venues when one isn't enough and reports any shortfall.
* **`venues.py`** — protocol adapters behind one interface; adding a protocol = one class + a line in `discover()`.
  * `AvalonReserve`: pool found on chain (`POOL()` of the Avalon USDC aToken) → `getReservesList()`,
    `getReserveData()` (rate, aToken, config bits: supply cap, active/frozen/paused, reserve factor); deposit =
    `approve` + `supply(asset, amount, onBehalfOf, 0)`, withdraw = `withdraw(asset, amount|MAX, to)`.
  * `MorphoMarket`: `idToMarketParams`, `market`, `position`, IRM `borrowRateView`; deposit = `approve` +
    `supply(marketParams, assets, 0, onBehalf, "")`, withdraw = `withdraw(marketParams, assets|0, 0|shares, onBehalf, receiver)`.
  * `Erc4626Vault`: `totalAssets`, `convertToAssets` now vs. 7 days ago, `previewDeposit`/`previewRedeem` for fees,
    `performanceFee`/`managementFee` when present; deposit = `approve` + `deposit(assets, receiver)`, withdraw =
    `redeem(shares, receiver, owner)`.
* **`Chain`** — one MCP session, every read pinned to the same block, token metadata cache, PROS/USD from Chainlink,
  `simulate()` = `eth_call` with `from = wallet` (no state change).

## Failure isolation (requirement 04)

`discover()` loads Avalon, each Morpho market and each vault inside its own `try/except` (MCP errors, reverts,
malformed data, `VenueUnavailable`). A failing protocol lands in `unavailable[]` with its error and is shown as
`UNAVAILABLE` in the status line; routing simply works over what is left. Position reads and withdrawal
simulations are isolated the same way. `--simulate-outage avalon|morpho|vault` is a **drill** that makes that
protocol's adapter raise as if its contracts were unreachable — used in the samples to show the behaviour; it does
not touch the network.

## Security model

No keys, no signing, no `send_raw_transaction`; transactions are emitted as unsigned JSON. HTTPS-only endpoints;
addresses validated; amounts must be positive decimals; the plain-English parser accepts only known intents and
known tokens.
