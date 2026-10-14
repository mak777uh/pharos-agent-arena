# Docs MCP session transcript (raw)

Endpoint: `https://docs.pharos.xyz/~gitbook/mcp` · server `docs.pharos.xyz/~gitbook/mcp` · protocol 2025-03-26 · started 2026-10-14 22:13:13 UTC · 15 calls

Answers are copied verbatim from the Docs MCP (long `getPage` / search results are cut at 2,500 chars here; transcript.json has them in full).

## Step 1 — Find out what ai.pharos.xyz is and where to start

**Tool:** `askQuestion` · **Query:** `What is ai.pharos.xyz and how do I get started with it as a developer?` · **goal:** `Onboard onto ai.pharos.xyz and build a first read-only agent on Pharos`
 · 17.6 s

```text
I cannot find information in the docs identifying **ai.pharos.xyz** itself. The documented developer entry point for building an AI agent that reads Pharos data is the **Pharos MCP server**: it connects AI clients to Pharos chain data and contract-read tools.

To start a read-only agent:

1. Add the hosted MCP server to an MCP-compatible client’s configuration:

   ```json
   {
     "mcpServers": {
       "pharos": {
         "type": "http",
         "url": "https://mcp.pharos.xyz/mcp"
       }
     }
   }
   ```

   The hosted endpoint requires no API key. Check connectivity with `curl https://mcp.pharos.xyz/health`.
2. Ask the agent for read-only tasks, such as checking the latest block, a PROS balance, or calling a contract’s view function. The server provides tools including `get_block_number`, `get_balance`, `eth_call`, and `get_logs`.
3. Keep the agent read-only by not asking it to broadcast transactions. For a stronger technical restriction, self-host the MCP server: transaction broadcasting is disabled by default there.

The hosted endpoint also exposes a transaction-broadcasting tool, so prompts alone are not a technical access control. See the [Pharos MCP](/tooling-and-infrastructure/mcp) for client setup, available tools, and self-hosting. If you meant a toolkit for an agent to run Pharos CLI commands, see the [Pharos Skill Engine](/tooling-and-infrastructure/pharos-skill-engine-guide).

# Sources

- [Alchemy RPC](https://docs.pharos.xyz/tooling-and-infrastructure/rpc/alchemy)
- [Get PROS Gas](https://docs.pharos.xyz/getting-started/get-pros-gas)
- [Write Your First dApp](https://docs.pharos.xyz/developer-guide/hardhat/write-your-first-dapp)
- [Pharos MCP](https://docs.pharos.xyz/tooling-and-infrastructure/mcp)
- [Pharos Skill Engine](https://docs.pharos.xyz/tooling-and-infrastructure/pharos-skill-engine-guide)
- [Goldsky](https://docs.pharos.xyz/tooling-and-infrastructure/indexing/goldsky)
- [About Pharos](https://docs.pharos.xyz/introduction/readme)
- [Publish Service Agent](https://docs.pharos.xyz/tooling-and-infrastructure/overview/publish-skill-af)
- [Encyclopedia](https://docs.pharos.xyz/introduction/encyclopedia)
- [Using Binary](https://docs.pharos.xyz/node-and-validator-guide/validator-node-deployment/using-binary-deployment)
- [Node Debugging & Configuration](https://docs.pharos.xyz/node-and-validator-guide/node-debugging-and-configuration)
- [Pharos Network Snapshots](https://docs.pharos.xyz/node-and-validator-guide/pharos-network-sna …[cut]
```

## Step 2 — Locate the AI / agent section of the docs

**Tool:** `searchDocumentation` · **Query:** `ai.pharos.xyz agent center MCP SkillEngine`
 · 0.7 s

```text
Title: Pharos MCP
Link: https://docs.pharos.xyz/tooling-and-infrastructure/mcp
Content: The Pharos MCP server brings Pharos chain data and onchain operations into any client that speaks the Model Context Protocol — Claude Desktop, Cursor, Windsurf, Codex, and custom agent runners.

Instead of writing RPC plumbing, you ask your AI assistant in plain language — "what's the latest Pharos block?", "check the PROS balance of 0x…" — and it calls the right chain method for you.

Hosted endpoint
https://mcp.pharos.xyz/mcp
Transport
Streamable HTTP
Authentication
None — public
Rate limit
120 requests per minute per IP
Chain ID
1672 (0x688)
Native token
PROS
Source
github.com/PharosNetwork/pharos-mcpTitle: Publish Service Agent
Link: https://docs.pharos.xyz/tooling-and-infrastructure/overview/publish-skill-af
Content: A Service Agent currently hosts a single Skill package. If your repo contains multiple Skills, split them into separate packages and submit each one individually.Title: x402
Link: https://docs.pharos.xyz/developer-guide/x402
Content: AI Agents subscribing to content services with auto-renewal: AI agents can represent users in subscribing to content services such as industry updates, financial newsletters, or in-depth analysis reports, automatically completing periodic fee payments and renewal operations through integrated digital wallets.
Pay-per-use AI capability calls: Users or systems pay based on the actual number of uses when calling Large Language Model (LLM) inference, image generation, or other AI APIs.
Payments between agents: Supports peer-to-peer payments between multiple autonomous agents for data sharing, task collaboration, or service provision without human intervention.
Automated financial trading agents: Based on real-time market data and external information sources (e.g., paid data streams), these agents autonomously execute high-frequency trading strategies and dynamically procure necessary data resources to optimize decision-making.Title: Pharos Skill Engine
Link: https://docs.pharos.xyz/tooling-and-infrastructure/pharos-skill-engine-guide
Content: When a developer asks the AI agent to do something on Pharos, the agent's first action is to read SKILL.md. This file does two things:

Declares the prerequisites the agent must satisfy (Foundry installed, private key configured)
Provides a Capability Index — a table that maps every possible user intent to the correct capability and points to detailed instructions in a reference file

Fo …[cut]
```

## Step 3 — Get the Chain MCP endpoint and how to connect a client

**Tool:** `askQuestion` · **Query:** `What is the URL of the hosted Pharos Chain MCP server and how do I connect an MCP client to it?`
 · 7.9 s

```text
The hosted Pharos Chain MCP server is **`https://mcp.pharos.xyz/mcp`**. It uses Streamable HTTP and requires no API key.

For an MCP client that supports remote HTTP servers, add:

```json
{
  "mcpServers": {
    "pharos": {
      "type": "http",
      "url": "https://mcp.pharos.xyz/mcp"
    }
  }
}
```

If your client supports only stdio, use `mcp-remote` as a bridge:

```json
{
  "mcpServers": {
    "pharos": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://mcp.pharos.xyz/mcp"]
    }
  }
}
```

You can check server health with `curl https://mcp.pharos.xyz/health`. The hosted endpoint is limited to 120 requests per minute per IP. See [Pharos MCP](/tooling-and-infrastructure/mcp) for details.

# Sources

- [Pharos MCP](https://docs.pharos.xyz/tooling-and-infrastructure/mcp)
- [Cross Chain Infrastructure](https://docs.pharos.xyz/tooling-and-infrastructure/cross-chain)
- [ZAN RPC](https://docs.pharos.xyz/tooling-and-infrastructure/rpc/zan)
- [x402](https://docs.pharos.xyz/developer-guide/x402)
- [Circle CCTP](https://docs.pharos.xyz/tooling-and-infrastructure/cross-chain/circle-cctp)
- [Pharos Skill Engine](https://docs.pharos.xyz/tooling-and-infrastructure/pharos-skill-engine-guide)
- [Node Management](https://docs.pharos.xyz/node-and-validator-guide/node-management)
- [Write Your First Token](https://docs.pharos.xyz/developer-guide/hardhat/write-your-first-token)
- [Write Your First NFT](https://docs.pharos.xyz/developer-guide/hardhat/write-your-first-nft)
- [Enable AutoPruning in Pharos Node](https://docs.pharos.xyz/enable-pruning-in-pharos-node)
```

## Step 4 — Read the MCP page in full

**Tool:** `getPage` · **Query:** `https://docs.pharos.xyz/tooling-and-infrastructure/mcp`
 · 0.3 s

```text
# Pharos MCP

## Overview

The Pharos MCP server brings Pharos chain data and onchain operations into any client that speaks the [Model Context Protocol](https://modelcontextprotocol.io) — Claude Desktop, Cursor, Windsurf, Codex, and custom agent runners.

Instead of writing RPC plumbing, you ask your AI assistant in plain language — *"what's the latest Pharos block?"*, *"check the PROS balance of 0x…"* — and it calls the right chain method for you.

|                     |                                                                                    |
| ------------------- | ---------------------------------------------------------------------------------- |
| **Hosted endpoint** | `https://mcp.pharos.xyz/mcp`                                                       |
| **Transport**       | Streamable HTTP                                                                    |
| **Authentication**  | None — public                                                                      |
| **Rate limit**      | 120 requests per minute per IP                                                     |
| **Chain ID**        | `1672` (`0x688`)                                                                   |
| **Native token**    | PROS                                                                               |
| **Source**          | [github.com/PharosNetwork/pharos-mcp](https://github.com/PharosNetwork/pharos-mcp) |

## Quick Start

Add the server to your MCP client configuration:

```json
{
  "mcpServers": {
    "pharos": {
      "type": "http",
      "url": "https://mcp.pharos.xyz/mcp"
    }
  }
}
```

No API key is required.

### Clients that only support stdio

Some clients cannot connect to remote MCP servers directly. Bridge to the hosted endpoint with [`mcp-remote`](https://www.npmjs.com/package/mcp-remote):

```json
{
  "mcpServers": {
    "pharos": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://mcp.pharos.xyz/mcp"]
    }
  }
}
```

### Verify the connection

```bash
curl https://mcp.pharos.xyz/health
```

```json
{"status":"ok","chain":"Pharos","chainId":1672,"rpcUrl":"https://rpc.pharos.xyz"}
```

## Available Tools

### Chain and block data

| Tool               | JSON-RPC method                                        | Description                               |
| ------------------ | ------------------------------------------------------ | ----------------------------------------- |
| `get_chain_info`   | `eth_chainId`, `eth …[cut]
```

## Step 5 — Does the hosted Chain MCP need an API key, and what are its rate limits?

**Tool:** `askQuestion` · **Query:** `Does the hosted Pharos Chain MCP require an API key, and what are its rate limits?`
 · 7.1 s

```text
No API key is required for the hosted Pharos MCP endpoint. It allows **120 requests per minute per IP**. Requests over the limit may receive `429 Too Many Requests`; the response includes a `Retry-After` header.

See [Pharos MCP](/tooling-and-infrastructure/mcp) for the endpoint and setup details.

# Sources

- [Pharos MCP](https://docs.pharos.xyz/tooling-and-infrastructure/mcp)
- [Circle CCTP](https://docs.pharos.xyz/tooling-and-infrastructure/cross-chain/circle-cctp)
- [Cross Chain Infrastructure](https://docs.pharos.xyz/tooling-and-infrastructure/cross-chain)
- [JSON-RPC API Methods](https://docs.pharos.xyz/api-and-sdk/json-rpc-methods)
- [ZAN RPC](https://docs.pharos.xyz/tooling-and-infrastructure/rpc/zan)
- [Foundry](https://docs.pharos.xyz/developer-guide/foundry)
- [Hardhat](https://docs.pharos.xyz/developer-guide/hardhat)
- [Pharos Skill Engine](https://docs.pharos.xyz/tooling-and-infrastructure/pharos-skill-engine-guide)
- [Write Your First Token](https://docs.pharos.xyz/developer-guide/hardhat/write-your-first-token)
- [Write Your First NFT](https://docs.pharos.xyz/developer-guide/hardhat/write-your-first-nft)
```

## Step 6 — Can the hosted Chain MCP send transactions?

**Tool:** `askQuestion` · **Query:** `Can I send a signed transaction through the hosted Pharos Chain MCP with send_raw_transaction?`
 · 6.9 s

```text
Yes. The hosted Pharos MCP exposes `send_raw_transaction` to broadcast an already-signed transaction. Sign it locally, then pass the `0x`-prefixed signed transaction bytes; the server never needs your private key. It returns a transaction hash you can check with `get_transaction_receipt`.

See [Pharos MCP](/tooling-and-infrastructure/mcp) for the hosted endpoint and transaction flow.

# Sources

- [Pharos MCP](https://docs.pharos.xyz/tooling-and-infrastructure/mcp)
- [Pharos Skill Engine](https://docs.pharos.xyz/tooling-and-infrastructure/pharos-skill-engine-guide)
- [Cross Chain Infrastructure](https://docs.pharos.xyz/tooling-and-infrastructure/cross-chain)
- [Hardhat](https://docs.pharos.xyz/developer-guide/hardhat)
- [Foundry](https://docs.pharos.xyz/developer-guide/foundry)
- [Circle CCTP](https://docs.pharos.xyz/tooling-and-infrastructure/cross-chain/circle-cctp)
- [Gas Model](https://docs.pharos.xyz/getting-started/gas-model)
- [Wallet](https://docs.pharos.xyz/tooling-and-infrastructure/wallets)
- [Safe MultiSig](https://docs.pharos.xyz/tooling-and-infrastructure/wallets/safe)
- [x402](https://docs.pharos.xyz/developer-guide/x402)
```

## Step 7 — Find the Docs MCP endpoint itself

**Tool:** `askQuestion` · **Query:** `What is the endpoint URL of the Pharos Developer Docs MCP server?`
 · 9.9 s

```text
The Pharos Developer Docs MCP server endpoint is **`https://mcp.pharos.xyz/mcp`**. It uses Streamable HTTP and requires no authentication. See [Pharos MCP](/tooling-and-infrastructure/mcp).

# Sources

- [Pharos MCP](https://docs.pharos.xyz/tooling-and-infrastructure/mcp)
- [x402](https://docs.pharos.xyz/developer-guide/x402)
- [About Pharos](https://docs.pharos.xyz/introduction/readme)
- [Pharos Skill Engine](https://docs.pharos.xyz/tooling-and-infrastructure/pharos-skill-engine-guide)
- [ZAN RPC](https://docs.pharos.xyz/tooling-and-infrastructure/rpc/zan)
- [Anvita Flow](https://docs.pharos.xyz/tooling-and-infrastructure/overview)
- [Rust](https://docs.pharos.xyz/developer-guide/rust)
- [Call EVM From WASM](https://docs.pharos.xyz/developer-guide/interoperability/call-evm-from-wasm)
- [Circle CCTP](https://docs.pharos.xyz/tooling-and-infrastructure/cross-chain/circle-cctp)
- [Alchemy RPC](https://docs.pharos.xyz/tooling-and-infrastructure/rpc/alchemy)
```

## Step 8 — Learn what SkillEngine is and how to use it

**Tool:** `askQuestion` · **Query:** `What is Pharos SkillEngine and how do I install or use a skill from it?`
 · 14.2 s

```text
The **Pharos Skill Engine** is a packaged toolkit of instructions, templates, and Pharos network settings. An AI agent reads its `SKILL.md`, selects the relevant instructions, then runs Foundry `cast` or `forge` commands. It is **not** an SDK, API, or smart-contract framework. See [Pharos Skill Engine](/tooling-and-infrastructure/pharos-skill-engine-guide).

### Install and set up

1. Unzip the Skill Engine package and enter its folder:

   ```bash
   unzip pharos-skill-engine-0.1.0.zip
   cd pharos-skill-engine-0.1.0
   ```
2. Install Foundry if needed, then verify `cast` and `forge` are available:

   ```bash
   curl -L https://foundry.paradigm.xyz | bash
   source ~/.zshenv && foundryup
   cast --version
   forge --version
   ```
3. For transaction operations, export your private key as an environment variable:

   ```bash
   export PRIVATE_KEY=0xYOUR_PRIVATE_KEY
   ```

   Never hardcode or share the key. Pass it explicitly to commands as `--private-key $PRIVATE_KEY`.

### Use a skill

Open the Skill Engine folder in an AI agent that can read its files. For example, start Claude Code there with `claude`, then make a request in natural language, such as “check my wallet balance” or “deploy an ERC20 token.” The agent uses `SKILL.md` to find the capability and its reference instructions.

For an end-to-end example of adding and using a custom vault skill, see the [Piggy Bank Tutorial](/tooling-and-infrastructure/pharos-skill-engine-guide/piggybank-skill-guide).

# Sources

- [Piggy Bank Tutorial](https://docs.pharos.xyz/tooling-and-infrastructure/pharos-skill-engine-guide/piggybank-skill-guide)
- [Pharos Skill Engine](https://docs.pharos.xyz/tooling-and-infrastructure/pharos-skill-engine-guide)
- [Publish Service Agent](https://docs.pharos.xyz/tooling-and-infrastructure/overview/publish-skill-af)
- [Validator Node Deployment](https://docs.pharos.xyz/node-and-validator-guide/validator-node-deployment)
- [Write Your First dApp](https://docs.pharos.xyz/developer-guide/hardhat/write-your-first-dapp)
- [Anvita Flow](https://docs.pharos.xyz/tooling-and-infrastructure/overview)
- [x402](https://docs.pharos.xyz/developer-guide/x402)
- [About Pharos](https://docs.pharos.xyz/introduction/readme)
- [Alchemy RPC](https://docs.pharos.xyz/tooling-and-infrastructure/rpc/alchemy)
- [Pharos MCP](https://docs.pharos.xyz/tooling-and-infrastructure/mcp)
```

## Step 9 — Learn how to produce a share link on ai.pharos.xyz (needed as Arena proof)

**Tool:** `askQuestion` · **Query:** `How do I create a share link for my agent session on ai.pharos.xyz?`
 · 12.6 s

```text
I cannot find information about creating a share link for an agent session in the docs. The [Anvita Flow](/tooling-and-infrastructure/overview) links to the chat interface, but doesn’t describe session sharing.

# Sources

- [Publish Service Agent](https://docs.pharos.xyz/tooling-and-infrastructure/overview/publish-skill-af)
- [Pharos Skill Engine](https://docs.pharos.xyz/tooling-and-infrastructure/pharos-skill-engine-guide)
- [Anvita Flow](https://docs.pharos.xyz/tooling-and-infrastructure/overview)
- [Pharos MCP](https://docs.pharos.xyz/tooling-and-infrastructure/mcp)
- [x402](https://docs.pharos.xyz/developer-guide/x402)
- [Piggy Bank Tutorial](https://docs.pharos.xyz/tooling-and-infrastructure/pharos-skill-engine-guide/piggybank-skill-guide)
- [ZAN RPC](https://docs.pharos.xyz/tooling-and-infrastructure/rpc/zan)
- [Node Management](https://docs.pharos.xyz/node-and-validator-guide/node-management)
- [Using Binary](https://docs.pharos.xyz/node-and-validator-guide/validator-node-deployment/using-binary-deployment)
- [Node Configuration](https://docs.pharos.xyz/node-and-validator-guide/validator-node-deployment/node-configuration)
- [Chainlink CCIP](https://docs.pharos.xyz/tooling-and-infrastructure/cross-chain/chainlink-ccip)
- [Hemera SocialScan](https://docs.pharos.xyz/tooling-and-infrastructure/block-explorer/hemera-socialscan)
- [Node Debugging & Configuration](https://docs.pharos.xyz/node-and-validator-guide/node-debugging-and-configuration)
- [Alchemy RPC](https://docs.pharos.xyz/tooling-and-infrastructure/rpc/alchemy)
- [Solidity](https://docs.pharos.xyz/resources/solidity)
```

## Step 10 — Network parameters for the mainnet

**Tool:** `askQuestion` · **Query:** `What is the chain ID, RPC URL and block explorer of Pharos mainnet?`
 · 6.4 s

```text
Pharos Pacific Mainnet uses:

* **Chain ID:** `1672`
* **RPC URL:** `https://rpc.pharos.xyz`
* **Block explorer:** <https://www.pharosscan.xyz>

See [Pacific Mainnet](/getting-started/network/pacific-mainnet).

# Sources

- [Pacific Mainnet](https://docs.pharos.xyz/getting-started/network/pacific-mainnet)
- [Block Explorer](https://docs.pharos.xyz/tooling-and-infrastructure/block-explorer)
- [Hemera SocialScan](https://docs.pharos.xyz/tooling-and-infrastructure/block-explorer/hemera-socialscan)
- [Node Debugging & Configuration](https://docs.pharos.xyz/node-and-validator-guide/node-debugging-and-configuration)
- [Pharos MCP](https://docs.pharos.xyz/tooling-and-infrastructure/mcp)
- [Atlantic Testnet](https://docs.pharos.xyz/getting-started/network/atlantic-testnet)
- [ZAN RPC](https://docs.pharos.xyz/tooling-and-infrastructure/rpc/zan)
- [Rollback Blocks](https://docs.pharos.xyz/node-and-validator-guide/rollback-blocks)
- [RPC Infrastructure](https://docs.pharos.xyz/tooling-and-infrastructure/rpc)
- [Pharos Skill Engine](https://docs.pharos.xyz/tooling-and-infrastructure/pharos-skill-engine-guide)
```

## Step 11 — Network parameters for the testnet + faucet

**Tool:** `askQuestion` · **Query:** `What is the chain ID and RPC URL of the Pharos Atlantic testnet and where is the faucet?`
 · 10.8 s

```text
* **Chain ID:** `688689`
* **RPC URL:** `https://atlantic.dplabs-internal.com`
* **Faucet:** The [Piggy Bank Tutorial](/tooling-and-infrastructure/pharos-skill-engine-guide/piggybank-skill-guide) is referenced, but its URL isn’t specified. The guide also says testnet tokens can be requested directly from the Pharos team.

See [Atlantic Testnet](/getting-started/network/atlantic-testnet) for the network details.

# Sources

- [Atlantic Testnet](https://docs.pharos.xyz/getting-started/network/atlantic-testnet)
- [Node Management](https://docs.pharos.xyz/node-and-validator-guide/node-management)
- [Piggy Bank Tutorial](https://docs.pharos.xyz/tooling-and-infrastructure/pharos-skill-engine-guide/piggybank-skill-guide)
- [RPC Infrastructure](https://docs.pharos.xyz/tooling-and-infrastructure/rpc)
- [Pharos Skill Engine](https://docs.pharos.xyz/tooling-and-infrastructure/pharos-skill-engine-guide)
- [Cross Chain Infrastructure](https://docs.pharos.xyz/tooling-and-infrastructure/cross-chain)
- [Alchemy RPC](https://docs.pharos.xyz/tooling-and-infrastructure/rpc/alchemy)
- [Pharos MCP](https://docs.pharos.xyz/tooling-and-infrastructure/mcp)
- [Write Your First Token](https://docs.pharos.xyz/developer-guide/hardhat/write-your-first-token)
- [Write Your First NFT](https://docs.pharos.xyz/developer-guide/hardhat/write-your-first-nft)
- [Token Registry](https://docs.pharos.xyz/getting-started/token-registry)
- [Rapid Node Initialization(Testnet)](https://docs.pharos.xyz/node-and-validator-guide/rapid-node-initialization/rapid-node-initialization-testnet)
- [Write Your First Token](https://docs.pharos.xyz/developer-guide/foundry/write-your-first-token)
- [Write Your First NFT](https://docs.pharos.xyz/developer-guide/foundry/write-your-first-nft)
```

## Step 12 — Which tools does the Chain MCP expose (names + arguments)?

**Tool:** `askQuestion` · **Query:** `List the tools exposed by the Pharos Chain MCP server and their arguments.`
 · 22.7 s

```text
The MCP page lists **13 tools**. It doesn’t publish their exact MCP JSON schemas, so the arguments below summarize the corresponding JSON-RPC inputs. Optional block arguments default to `latest`. See [Pharos MCP](/tooling-and-infrastructure/mcp) and [JSON-RPC API Methods](/api-and-sdk/json-rpc-methods).

**Chain and block data**

* `get_chain_info` — no arguments.
* `get_block_number` — no arguments.
* `get_block` — `block` (tag or number); optionally `fullTransactions` (boolean).
* `get_gas_price` — no arguments.

**Accounts and transactions**

* `get_balance` — `address`; optionally `block`.
* `get_transaction_count` — `address`; optionally `block`.
* `get_transaction` — `hash`.
* `get_transaction_receipt` — `hash`.

**Contracts**

* `eth_call` — `transaction` object (`from`, `to`, `gas`, `gasPrice`, `value`, `data`); optionally `block`.
* `estimate_gas` — `transaction` object; optionally `block`.
* `get_code` — `address`; optionally `block`.
* `get_storage_at` — `address`, `position` (storage slot), optionally `block`.
* `get_logs` — `filter` object with optional `fromBlock`, `toBlock`, `address`, and `topics`. The MCP docs cap each query at 1,000 blocks.

**Broadcasting**

* `send_raw_transaction` — signed transaction data (`0x`-prefixed bytes). Broadcasting is disabled by default for self-hosted deployments.

# Sources

- [Pharos MCP](https://docs.pharos.xyz/tooling-and-infrastructure/mcp)
- [Cross Chain Infrastructure](https://docs.pharos.xyz/tooling-and-infrastructure/cross-chain)
- [Pharos Skill Engine](https://docs.pharos.xyz/tooling-and-infrastructure/pharos-skill-engine-guide)
- [x402](https://docs.pharos.xyz/developer-guide/x402)
- [About Pharos](https://docs.pharos.xyz/introduction/readme)
- [Enable AutoPruning in Pharos Node](https://docs.pharos.xyz/enable-pruning-in-pharos-node)
- [Solidity](https://docs.pharos.xyz/resources/solidity)
- [Fordefi](https://docs.pharos.xyz/tooling-and-infrastructure/wallets/fordefi)
- [Using Binary](https://docs.pharos.xyz/node-and-validator-guide/validator-node-deployment/using-binary-deployment)
- [Hemera SocialScan](https://docs.pharos.xyz/tooling-and-infrastructure/block-explorer/hemera-socialscan)
- [JSON-RPC API Methods](https://docs.pharos.xyz/api-and-sdk/json-rpc-methods)
- [Block Explorer](https://docs.pharos.xyz/tooling-and-infrastructure/block-explorer)
- [Alchemy RPC](https://docs.pharos.xyz/tooling-and-infrastructure/rpc/alchemy)
- [Node Debugging & Configuration](https://docs.pharos.xyz/node-and-v …[cut]
```

## Step 13 — Find an official token / contract address to read

**Tool:** `askQuestion` · **Query:** `What is the USDC contract address on Pharos mainnet?`
 · 10.3 s

```text
The USDC contract address on Pharos Pacific Mainnet is **`0xc879c018db60520f4355c26ed1a6d572cdac1815`**. See the [Token Registry](/getting-started/token-registry).

# Sources

- [Canonical Contracts](https://docs.pharos.xyz/getting-started/canonical-contracts)
- [Write Your First Uniswap Contract](https://docs.pharos.xyz/developer-guide/hardhat/write-your-first-uniswap-contract)
- [Write Your First Uniswap Contract](https://docs.pharos.xyz/developer-guide/foundry/write-your-first-uniswap-contract)
- [Pacific Mainnet](https://docs.pharos.xyz/getting-started/network/pacific-mainnet)
- [Token Registry](https://docs.pharos.xyz/getting-started/token-registry)
- [About Pharos](https://docs.pharos.xyz/introduction/readme)
- [Rust](https://docs.pharos.xyz/developer-guide/rust)
- [Hemera SocialScan](https://docs.pharos.xyz/tooling-and-infrastructure/block-explorer/hemera-socialscan)
- [LayerZero](https://docs.pharos.xyz/tooling-and-infrastructure/cross-chain/layerzero)
- [Circle CCTP](https://docs.pharos.xyz/tooling-and-infrastructure/cross-chain/circle-cctp)
```

## Step 14 — Get a block-range limit before calling get_logs

**Tool:** `askQuestion` · **Query:** `What is the maximum block range for get_logs on the Pharos Chain MCP or RPC?`
 · 10.3 s

```text
The limit depends on the interface:

* **Pharos Chain MCP `get_logs`: 1,000 blocks per call.** Split larger ranges into 1,000-block windows. See [Pharos MCP](/tooling-and-infrastructure/mcp).
* **JSON-RPC `eth_getLogs`: 100 blocks when rate limiting is enabled; 10,000 blocks as the fallback cap.** See [JSON-RPC API Methods](/api-and-sdk/json-rpc-methods).

# Sources

- [Pharos MCP](https://docs.pharos.xyz/tooling-and-infrastructure/mcp)
- [Rollback Blocks](https://docs.pharos.xyz/node-and-validator-guide/rollback-blocks)
- [JSON-RPC API Methods](https://docs.pharos.xyz/api-and-sdk/json-rpc-methods)
- [Node Debugging & Configuration](https://docs.pharos.xyz/node-and-validator-guide/node-debugging-and-configuration)
- [Block Explorer](https://docs.pharos.xyz/tooling-and-infrastructure/block-explorer)
- [Why Pharos](https://docs.pharos.xyz/introduction/why-pharos)
- [About Pharos](https://docs.pharos.xyz/introduction/readme)
- [Node Configuration](https://docs.pharos.xyz/node-and-validator-guide/validator-node-deployment/node-configuration)
- [Cross Chain Infrastructure](https://docs.pharos.xyz/tooling-and-infrastructure/cross-chain)
- [Get PROS Gas](https://docs.pharos.xyz/getting-started/get-pros-gas)
```

## Step 15 — Confirm whether ai.pharos.xyz itself has docs (login, workspace, agents)

**Tool:** `searchDocumentation` · **Query:** `ai.pharos.xyz sign in workspace share`
 · 0.7 s

```text
Title: About Pharos
Link: https://docs.pharos.xyz/introduction/readme
Content: Pharos has designed a Modular & Full-stack Parallel L1 Blockchain Network, structured into three primary layers: L1-Base, L1-Core, and L1-Extension.

L1-Base: Delivers industry-leading data availability and hardware acceleration.
L1-Core: A high-performance, globally distributed blockchain network powered by decentralized nodes. These nodes provide high-throughput and sub-second finality, enabling real-time Web3 user experiences.
L1-Extension: Built on top of L1-Core, this layer supports network expansion across three dimensions:
Leverages heterogeneous computation to create custom Special Processing Networks (SPNs), capable of running blockchain networks, sidechains, or non-blockchain applications like HFT, ZKML, and AI models.
Enables Native Restaking to support rapid SPN creation, shared security, staking rewards, and slashing mechanisms. SPNs utilize the excess computing power of validators, providing flexibility in resource use while incentivizing upgrades.
Facilitates Cross-SPNs Interoperation, allowing infrastructure, middleware, and application-focused SPNs to communicate and collaborate effortlessly, fostering a highly modular and composable ecosystem.Title: x402
Link: https://docs.pharos.xyz/developer-guide/x402
Content: Scheme Selection and Payment Payload Generation: The client parses the payment requirements returned by the server and selects a specific payment scheme based on its own supported payment capabilities. Subsequently, the client constructs a PaymentPayload containing the necessary metadata according to the protocol specifications of that scheme.
Secondary Request Submission: The client re-initiates the resource request and attaches the signed PaymentPayload in the PAYMENT-SIGNATURE HTTP header.Title: Pharos Skill Engine
Link: https://docs.pharos.xyz/tooling-and-infrastructure/pharos-skill-engine-guide
Content: The Pharos Skill Engine is a packaged developer toolkit that lets an AI agent perform any on-chain operation on the Pharos — check balances, send transactions, deploy and verify Solidity contracts, run batch airdrops, and generate Web3 interaction scripts — entirely through CLI commands.

It is not an SDK, not an API, and not a smart contract framework. It is a self-contained knowledge and tooling package that a developer installs once and then hands to an AI agent (like Claude). The agent reads the package, understands what Pharos operations are a …[cut]
```
