# Tokens (metadata + unit correctness)

## Primary data source

- Client: `wayfinder_paths/core/clients/TokenClient.py`
- Adapter wrapper: `wayfinder_paths/adapters/token_adapter/adapter.py`

## High-value reads

### Resolve token metadata (id/address → canonical details)

- Call: `TokenClient.get_token_details(query)`
- Input: `query` can be a token id or an address-like string (backend decides how to resolve)
- Output: token metadata with at least:
  - `id`, `address`, `symbol`, `name`, `decimals`
  - chain metadata: `chain_id`/`chain_code` or nested `chain` object depending on response

Best practice:
- Never assume decimals; always fetch and cache them per token.
- Treat the response as “schema-flexible” and check key presence.
- Prefer **token ids** over free-text symbol/name searches. Free-text queries can resolve to unexpected tokens (e.g. a wrapped/staked variant).

#### Canonical token id format (recommended)

**Format:** `<coingecko_id>-<chain_code>`

The first part is the **coingecko_id** (NOT the symbol). Common examples:
- `ethereum-arbitrum` — ETH on Arbitrum (coingecko_id is `ethereum`)
- `usd-coin-base` — USDC on Base (coingecko_id is `usd-coin`, NOT `usdc`)
- `usd-coin-polygon` — USDC on Polygon (coingecko_id is `usd-coin`, NOT `usdc`)
- `usdt0-arbitrum` — USDT on Arbitrum
- `hyperliquid-hyperevm` — HYPE on HyperEVM

**Important:** Do NOT use symbol-chain like `usdc-base`/`usdc-polygon` or chain-symbol like `polygon_usdc` in scripts, quotes, or execution tickets. `onchain_resolve_token` may tolerate these as a user-input fallback via fuzzy search, but you should immediately convert to the returned canonical id/address. Use `usd-coin-base`, `usd-coin-polygon`, or an exact address id instead.

Swap and quote amounts are decimal human-unit strings. They must include a decimal point, for example `"5.0"` instead of `"5"`. For full-balance actions, use the exact `amount_decimal` string from `get_wallets`; do not pass raw wei, integer-looking strings, or rounded floats.

When you need a specific ERC20 and you know the contract:
- Use a chain-scoped address id: `<chain_code>_<address>`
  - Example: `base_0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` (USDC on Base)
  - Example: `arbitrum_0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9` (USDT on Arbitrum)
- `onchain_resolve_token` may normalize near-miss address forms like `base-0x...` or `base:0x...`, but scripts and execution tickets should use the returned canonical `<chain_code>_<address>` id.
- This avoids cross-chain ambiguity for contracts deployed on multiple chains.
- If the lookup resolves to a different contract address than you specified, treat it as ambiguous and switch to the coingecko format or use the exact address.

### Gas token for a chain

- Call: `TokenClient.get_gas_token(chain_code)`
- Output: gas token metadata for the chain (symbol/decimals/address).

## Strategy patterns

- Convert human units → raw units using `decimals` before building txs.
- Convert raw balances → human units for reporting only (keep raw for execution).
