# Pharos Agent Arena · Read Contract State (Chain MCP)

A small Python agent script that reads **live state of a deployed Pharos contract through the
Pharos Chain MCP server** and prints it in plain language for non-developers.

Built for the Pharos *Season of AI · Agent Arena* task
[Read Contract State](https://pharos-agent-arena.vercel.app/tasks/read-contract-state).

## Which contract

**USDC (Circle-deployed) on Pharos Pacific mainnet** (chainId 1672)
`0xc879c018db60520f4355c26ed1a6d572cdac1815` —
listed in the official [Pharos token registry](https://docs.pharos.xyz/getting-started/token-registry) ·
[explorer](https://www.pharosscan.xyz/address/0xc879c018db60520f4355c26ed1a6d572cdac1815).

It is a Circle FiatToken behind an upgradeable proxy, so it has more interesting state than a plain
ERC-20: roles (owner, pauser, master minter, blacklister, rescuer), a pause switch and proxy slots.
Any other contract can be passed with `--address`; functions it does not implement are skipped.

## How it uses Chain MCP

All chain data comes from the hosted Pharos Chain MCP server `https://mcp.pharos.xyz/mcp`
([PharosNetwork/pharos-mcp](https://github.com/PharosNetwork/pharos-mcp)), spoken over MCP
Streamable HTTP (JSON-RPC 2.0): `initialize` → `notifications/initialized` → `tools/list` →
`tools/call`. The client is `pharos_mcp.py` (no SDK, only `requests`).

| MCP tool | What it is used for |
|---|---|
| `get_chain_info` | network name, chainId check, latest block number (the snapshot block) |
| `get_block` | hash and timestamp of the snapshot block |
| `eth_call` | `name`, `symbol`, `decimals`, `totalSupply`, `currency`, `version`, `paused`, `owner`, `pauser`, `masterMinter`, `blacklister`, `rescuer`, `balanceOf` |
| `get_storage_at` | raw proxy storage slots (ZeppelinOS and EIP-1967 implementation/admin) |
| `get_code` | labels each address as *contract* (with code size) or *wallet (EOA)* |
| `get_balance` | native PROS (gas) balance of each labelled address |

Every call is pinned to the **same block number**, so all values belong to one consistent snapshot.

## How it covers the task requirements

1. **≥3 values from one contract via Chain MCP** — 13 view functions + 4 storage slots of the USDC contract.
2. **Contract address and block number** — printed in the header (plus block hash, time, explorer link).
3. **Human formatting** — decimals applied (`23558408456760` → `23,558,408.456760 USDC`, raw value kept
   alongside), booleans in words, every address labelled by role, by EOA/contract and by the
   official token registry for known contracts (WPROS, USDC, LINK, WETH).
4. **README** — this file.

## Run it

Python 3.10+ (tested with Python 3.12.15).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python read_contract_state.py                                   # USDC at the latest block
python read_contract_state.py --holder 0x52c48d4213107b20bc583832b0d951fb9ca8f0b0   # + a balance
python read_contract_state.py --json                            # machine-readable output
python read_contract_state.py --address 0x52c48d4213107b20bc583832b0d951fb9ca8f0b0  # another contract (WPROS)
```

Options: `--address`, `--holder` (repeatable), `--endpoint` (default `https://mcp.pharos.xyz/mcp`), `--json`.

## Sample output (real run, 2026-10-14)

Produced by `python read_contract_state.py --holder 0x52c48d4213107b20bc583832b0d951fb9ca8f0b0`
(also saved as [`sample_output.txt`](sample_output.txt); a JSON run is in
[`sample_output.json`](sample_output.json), taken at a nearby block):

```
========================================================================
 PHAROS CONTRACT STATE  ·  USDC (USDC)
========================================================================
 Network      : Pharos (chainId 1672)
 Contract     : 0xc879c018db60520f4355c26ed1a6d572cdac1815  [contract, 1,798 bytes of code]
 Explorer     : https://www.pharosscan.xyz/address/0xc879c018db60520f4355c26ed1a6d572cdac1815
 Read at block: #20,223,987  (2026-10-14 12:58:24 UTC)
 Block hash   : 0x3e3af36f77c9e90888e93eb3a840bcf5ef8bca7d9b614d1f583b6816068527a9
 Source       : Pharos Chain MCP — pharos-mcp-server 0.1.0 @ https://mcp.pharos.xyz/mcp
------------------------------------------------------------------------
 WHAT THIS TOKEN IS
  • Name ............. USDC
  • Ticker ........... USDC
  • Decimals ......... 6  (1 USDC = 10^6 base units)
  • Total supply ..... 23,558,408.456760 USDC
                       (raw on-chain value: 23558408456760)
  • Backing currency . USD
  • Contract version . 2
  • Transfers ........ active (not paused)
------------------------------------------------------------------------
 WHO CONTROLS IT
  • Owner (can reassign roles)
      0x9f8ea09770dc38fc61f5a62031b3be0526d54d92  [wallet (EOA)]
  • Pauser (can freeze all transfers)
      0x828409db274cad94ac10db6d54aeca1dbe00e237  [wallet (EOA)]
  • Master minter (manages who may mint)
      0x908ea62d2529f31347d71992954e540a8d169603  [contract, 5,346 bytes of code]
  • Blacklister (can block addresses)
      0xba8cd4958d4105aaa23b00c645573f53fcd59486  [wallet (EOA)]
  • Rescuer (can recover stuck tokens)
      0x0000000000000000000000000000000000000000  [zero address (unset)]
------------------------------------------------------------------------
 UPGRADEABILITY (raw storage slots)
  This address is an upgradeable proxy: the logic lives elsewhere.
  • proxy implementation (zos): 0xc35becb283fec66626220467265f6d3b3f8ceae8  [contract, 23,464 bytes of code]
  • proxy admin (zos): 0x46f958bdeffd33495e058873cdd114c79ec13f1b  [wallet (EOA)]
------------------------------------------------------------------------
 BALANCES
  • Wrapped PROS (WPROS) token contract
      0x52c48d4213107b20bc583832b0d951fb9ca8f0b0: 0.807275 USDC   |   gas balance: 23256442.525086389026254338 PROS
  • Owner
      0x9f8ea09770dc38fc61f5a62031b3be0526d54d92: 0.000000 USDC   |   gas balance: 0.2 PROS
  • Pauser
      0x828409db274cad94ac10db6d54aeca1dbe00e237: 0.000000 USDC   |   gas balance: 0.2 PROS
  • Master minter
      0x908ea62d2529f31347d71992954e540a8d169603: 0.000000 USDC   |   gas balance: 0 PROS
  • Blacklister
      0xba8cd4958d4105aaa23b00c645573f53fcd59486: 0.000000 USDC   |   gas balance: 51.814762963091646094 PROS
  • this contract (USDC)
      0xc879c018db60520f4355c26ed1a6d572cdac1815: 0.000000 USDC   |   gas balance: 0 PROS
========================================================================
```

## Security notes

* Read-only: no private key, no signing, no transactions; the hosted MCP server itself has
  `send_raw_transaction` disabled. No API keys or secrets are used or stored.
* Inputs are validated: addresses must be 20-byte hex; the MCP endpoint must be `https://`
  (plain `http://` is accepted only for `localhost` when self-hosting the server).
* Respects the server rate limit (120 req/min): HTTP 429 is retried after `Retry-After`.

## Files

* `read_contract_state.py` — the agent script.
* `pharos_mcp.py` — minimal Chain MCP client (session handling, SSE parsing, 429 retry, input checks).
* `sample_output.txt`, `sample_output.json` — real outputs.

License: MIT
