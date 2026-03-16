---
paths: ["**/wallet*", "**/balance*", "config.json"]
---

# Wallet Management & Portfolio Discovery

Read-only wallet information is exposed via MCP resources, and fund-moving / tracking actions via the `mcp__wayfinder__wallets` tool.

**Quick balance check:**

- Use MCP resource `wayfinder://balances/{label}` for enriched token balances (USD totals + chain breakdown).
- Use MCP resource `wayfinder://wallets/{label}` for tracked protocol history for a wallet label.
- Use `mcp__wayfinder__wallets(action="discover_portfolio", ...)` for live protocol position discovery (Hyperliquid perp, Moonwell supplies, etc.).

**Read-only resources:**

- `wayfinder://wallets` - list all wallets and tracked protocols
- `wayfinder://wallets/{label}` - full profile for a wallet (protocol interactions, transactions)
- `wayfinder://balances/{label}` - enriched token balances
- `wayfinder://activity/{label}` - recent wallet activity (best-effort)
- `wayfinder://contracts` - list all locally-deployed contracts (name, address, chain, verification status)
- `wayfinder://contracts/{chain_id}/{address}` - full metadata + ABI for a deployed contract

**Tool actions (`mcp__wayfinder__wallets`):**

- `create` - create a new local wallet (writes to `config.json`)
- `annotate` - record a protocol interaction (internal use)
- `discover_portfolio` - query adapters for positions

**Automatic tracking:**

- Profiles auto-update when you use `mcp__wayfinder__execute`, `mcp__wayfinder__hyperliquid_execute`, or `mcp__wayfinder__run_script` (with `wallet_label`)

**Portfolio discovery:**

- Use `mcp__wayfinder__wallets(action="discover_portfolio", wallet_label="main")` to fetch all positions
- Only queries protocols the wallet has previously interacted with
- **Warning:** If 3+ protocols are tracked, tool returns a warning and asks for confirmation or use `parallel=true`
- Use `protocols=["hyperliquid"]` to query specific protocols only

**Manual annotation:**

- Use `action="annotate"` if you know a wallet has used a protocol not yet tracked

**Best practices:**

- Use `wayfinder://wallets` to see all wallets and their tracked protocols at a glance
- Annotate manually if a protocol interaction predates this system
