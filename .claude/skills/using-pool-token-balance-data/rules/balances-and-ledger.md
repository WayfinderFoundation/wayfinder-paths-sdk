# Balances and ledger

## Balance reads (API and on-chain)

- Adapter: `wayfinder_paths/adapters/balance_adapter/adapter.py`
- API client: `wayfinder_paths/core/clients/BalanceClient.py` (`BALANCE_CLIENT`)
- **MCP resource (preferred for quick checks):** `wayfinder://balances/{label}` - returns enriched token balances (USD totals + chain breakdown) for a wallet label.
  - Use via `ReadMcpResourceTool(server="wayfinder", uri="wayfinder://balances/main")`.
- **MCP resource:** `wayfinder://activity/{label}` - returns recent wallet activity (best-effort).

High-value reads:
- `BALANCE_CLIENT.get_enriched_wallet_balances(wallet_address=..., exclude_spam_tokens=True)`
  - Source: Wayfinder API (`/blockchain/balances/enriched/`).
  - Output: a full portfolio view (all tokens across chains) with USD totals + chain breakdown.

- `BALANCE_CLIENT.get_token_balance(wallet_address=..., token_id=..., human_readable=True)`
  - Source: Wayfinder API (`/public/balances/token/`).
  - Output: schema-flexible dict with a token balance payload.

- `BALANCE_CLIENT.get_pool_balance(pool_address=..., chain_id=..., user_address=..., human_readable=True)`
  - Source: Wayfinder API (`/public/balances/pool/`).
  - Output: schema-flexible dict with a pool/share balance payload.

- `BalanceAdapter.get_balance(wallet_address=..., token_id=... | token_address=..., chain_id=...)`
  - Source: on-chain read (uses RPC URLs) for ERC20 balance checks.
  - Output: raw balance in base units (int).

- `BalanceAdapter.get_vault_wallet_balance(token_id)`
  - Source: on-chain read (uses configured `strategy_wallet` from `config.json`).
  - Input: `token_id` is a Wayfinder token identifier (e.g. `usd-coin-arbitrum`).
  - Output: raw balance in base units (int).

- `BalanceAdapter.get_wallet_balances_multicall(assets=[...], wallet_address=None)`
  - Source: on-chain multicall grouped by chain.
  - Output: per-asset results including `balance_raw`, inferred `decimals`, and optional `balance_decimal`.

## Local ledger bookkeeping (dev + analysis)

- Adapter: `wayfinder_paths/adapters/ledger_adapter/adapter.py`
- Storage: `wayfinder_paths/core/clients/LedgerClient.py` writes JSON into `.ledger/` (gitignored)

Use ledger operations for:
- capturing swaps/deposits/withdrawals (so strategies can unwind safely)
- recording snapshots (`Strategy.status()` best-effort snapshotting when ledger present)

Best practice:
- Treat ledger writes as best-effort (don’t break strategy execution if ledger fails).
