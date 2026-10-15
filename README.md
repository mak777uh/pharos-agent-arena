# Pharos Agent Arena — builds

Solutions for tasks on the [Pharos Agent Arena](https://pharos-agent-arena.vercel.app/) (Pharos Season of AI). They use the public Pharos Chain MCP server (`https://mcp.pharos.xyz/mcp`, read-only) on Pharos mainnet (chainId 1672); the doc tools also use the Pharos Developer Docs MCP (`https://docs.pharos.xyz/~gitbook/mcp`). None of them signs or sends transactions; the executors that need receipts (Saga Executor) or simulated execution (Arbitrage Scanner) run those steps on a local anvil fork of mainnet with the public address impersonated, never on mainnet.

| Task | Folder |
|---|---|
| Read Contract State (Easy) | [`read-contract-state/`](read-contract-state/) |
| Event Log Monitor (Easy) | [`event-log-monitor/`](event-log-monitor/) |
| dApp Support Agent (Easy) | [`dapp-support-agent/`](dapp-support-agent/) |
| Whale-Watching Agent (Medium) | [`whale-watching-agent/`](whale-watching-agent/) |
| Audit-Log Agent (Medium) | [`audit-log-agent/`](audit-log-agent/) |
| Doc-Search CLI (Easy) | [`doc-search-cli/`](doc-search-cli/) |
| Docs MCP + Chain MCP Agent (Medium) | [`docs-chain-mcp-agent/`](docs-chain-mcp-agent/) |
| Liquidation Watchdog (Medium) | [`liquidation-watchdog/`](liquidation-watchdog/) |
| Gas Fee Optimizer (Medium) | [`gas-fee-optimizer/`](gas-fee-optimizer/) |
| Wallet Risk Checker (Medium) | [`wallet-risk-checker/`](wallet-risk-checker/) |
| Weekly Digest Agent (Medium) | [`weekly-digest-agent/`](weekly-digest-agent/) |
| Portfolio Assistant (Hard) | [`portfolio-assistant/`](portfolio-assistant/) |
| Natural-Language Payment Agent (Medium) | [`natural-language-payment-agent/`](natural-language-payment-agent/) |
| Invoice-to-Payment Agent (Medium) | [`invoice-to-payment-agent/`](invoice-to-payment-agent/) |
| Recurring Payment Agent (Medium) | [`recurring-payment-agent/`](recurring-payment-agent/) |
| Docs MCP Onboarding (Easy) | [`docs-mcp-onboarding/`](docs-mcp-onboarding/) |
| Yield Optimizer (Hard) | [`yield-optimizer/`](yield-optimizer/) |
| Multi-Protocol Aggregator (Hard) | [`multi-protocol-aggregator/`](multi-protocol-aggregator/) |
| Doc-Grounded Builder Agent (Hard) | [`doc-grounded-builder-agent/`](doc-grounded-builder-agent/) |
| Skill + Chain MCP Flow (Medium, SkillEngine) | [`skill-chain-mcp-flow/`](skill-chain-mcp-flow/) |
| Threshold Guardian (Medium, SkillEngine) | [`threshold-guardian/`](threshold-guardian/) |
| Policy Gatekeeper (Medium, SkillEngine) | [`policy-gatekeeper/`](policy-gatekeeper/) |
| Dry-Run Executor (Medium, SkillEngine) | [`dry-run-executor/`](dry-run-executor/) |
| Batch Executor (Medium, SkillEngine) | [`batch-executor/`](batch-executor/) |
| Trend Watcher (Hard, SkillEngine) | [`trend-watcher/`](trend-watcher/) |
| Saga Executor (Hard, SkillEngine) | [`saga-executor/`](saga-executor/) |
| Arbitrage Scanner (Hard, SkillEngine) | [`arbitrage-scanner/`](arbitrage-scanner/) |

Each folder has its own README with setup, usage and real sample output. License: MIT.
