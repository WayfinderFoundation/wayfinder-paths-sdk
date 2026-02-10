# ProjectXLiquidityAdapter

Adapter for interacting with ProjectX (HyperEVM Uniswap v3 fork) concentrated liquidity:

- Pool overview + tick spacing
- List wallet-owned NPM positions
- Mint + increase liquidity (strategy-oriented helpers)
- Collect fees + burn positions (via NPM multicall)
- Exact-input swaps via PRJX Router

Public methods follow the repo convention: `async def ... -> tuple[bool, data | str]`.

Configuration:

- `strategy_wallet.address` (required)
- `pool_address` (required; also accepts `pool`, `projectx_pool_address`, `projectx_pool`, and checks nested `strategy` config)

Notable helpers:

- `pool_overview()`
- `current_balances()`
- `list_positions()`
- `mint_from_balances(tick_lower, tick_upper, slippage_bps=...)`
- `increase_liquidity_balanced(token_id, tick_lower, tick_upper, slippage_bps=...)`
- `burn_position(token_id)` (convenience wrapper over `remove_liquidity(..., collect=True, burn=True)`)
- `swap_exact_in(from_token, to_token, amount_in, slippage_bps=...)`
- `find_pool_for_pair(token_a, token_b, prefer_fees=...)`
