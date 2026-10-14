# Flow: what calls what, in what order

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant S as Skill (scripts/flow.py)
    participant M as Pharos Chain MCP<br/>mcp.pharos.xyz/mcp
    participant C as Pharos mainnet<br/>(pool, Chainlink, USDC)
    U->>S: "Is now a good time to swap 500 USDC to WPROS?"
    S->>S: parse_request -> {500, USDC -> WPROS}
    S->>M: get_chain_info (must be chainId 1672)
    S->>M: get_block_number, get_block (head + timestamp)
    S->>M: eth_call token0/token1/decimals/symbol (pool matches the request?)
    S->>M: eth_call slot0, liquidity, fee, tickSpacing @head
    S->>M: eth_call Chainlink latestRoundData, decimals @head
    S->>M: get_gas_price
    opt --wallet
        S->>M: eth_call balanceOf(wallet) @head
    end
    M->>C: JSON-RPC (eth_call / eth_getBlockByNumber / ...)
    C-->>M: live state
    M-->>S: results
    S->>S: quote(): in-range Uniswap-V3 math on the live slot0/liquidity
    S->>M: eth_call tickBitmap(word) @head (does the swap cross an initialized tick?)
    S->>S: rules -> GO / WAIT / NO-GO
    S-->>U: verdict + quote + every rule + the list of MCP calls
```

Plain text: **Skill** (parse) → **Chain MCP** (≈ 16–18 read calls, all pinned to one head block except
`get_gas_price`) → **Skill** (quote + tick-range check, which itself needs one more Chain MCP `tickBitmap`
read) → answer. The verdict cannot be produced without the live data: impact, deviation, oracle age and the
balance rule are all computed from values fetched in the same run.

## Quote

Exact-input, inside the current tick range (`L` = `liquidity()`, `√P` = `sqrtPriceX96 / 2^96`, fee from `fee()`):

* sell token0 (WPROS): `√P' = L·√P / (L + Δx·√P)`, out = `L·(√P − √P')`
* sell token1 (USDC):  `√P' = √P + Δy / L`, out = `L·(1/√P − 1/√P')`

with `Δ = amount·(1 − fee)`. The predicted tick `floor(log_1.0001(√P'^2))` is checked against the pool's
`tickBitmap`; if an initialized tick lies in between (or the move spans more than 8 bitmap words) the in-range
formula does not hold → NO-GO "split the order".

## Rules

`assets/rules.json` (`assets/rules.strict.json` = same with 0.1 % deviation):

| rule | severity | default |
|---|---|---|
| range — no initialized tick crossed | NO-GO | – |
| impact — \|execution price − spot\| / spot | NO-GO | ≤ 0.5 % |
| balance — wallet holds the input amount (only with `--wallet`) | NO-GO | – |
| oracle_dev — \|pool spot − Chainlink PROS/USD\| / Chainlink | WAIT | ≤ 1 % |
| oracle_fresh — head timestamp − `updatedAt` | WAIT | ≤ 3900 s (heartbeat 3600 s + 5 min) |
| gas — `get_gas_price` | WAIT | ≤ 20 gwei |

## Backtest

`scripts/backtest.py` takes real `Swap` events of the pool, reads the pool state at block N−1 through Chain MCP,
runs the same `quote()` on the real input and compares with the real output. Swaps whose liquidity changed
(tick crossed) or that were not the first in their block are skipped.
