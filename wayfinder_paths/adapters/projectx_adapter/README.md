# ProjectXLiquidityAdapter

Adapter for interacting with ProjectX (HyperEVM Uniswap v3 fork) concentrated liquidity:

- Pool overview + tick spacing
- List wallet-owned NPM positions
- Mint + increase liquidity (strategy-oriented helpers)
- Collect fees + burn positions (via NPM multicall)
- Exact-input swaps via PRJX Router
- Points reads via the ProjectX points API
- Recent pool swaps via the ProjectX Goldsky subgraph

Public methods follow the repo convention: `async def ... -> tuple[bool, data | str]`.

## Current ProjectX / HyperEVM assumptions

Research checked against the ProjectX app, HyperEVM RPC, and live contracts in May 2026:

- HyperEVM mainnet is chain `999`; the public RPC is `https://rpc.hyperliquid.xyz/evm`.
- ProjectX liquidity rows in the app are V3-style concentrated-liquidity pools.
- Current ProjectX fee tiers include `100`, `500`, `1000`, `2000`, `3000`, `10000`, and `20000`.
- The app displays native HYPE as `0x0000000000000000000000000000000000000000`, but ProjectX V3 periphery uses WHYPE (`0x5555555555555555555555555555555555555555`) as `WETH9`.
- `swap_exact_in()` is a direct PRJX V3 router `exactInputSingle` helper. It is not the app's Reown/AppKit aggregator path.
- Rewards support is points-only: `fetch_prjx_points()` returns the public points API response (currently `walletAddress`, `pointsTotal`, `rank` for known wallets). No claimable onchain ProjectX reward/gauge flow is exposed by this adapter.

Verified addresses:

- Factory: `0xFf7B3e8C00e57ea31477c32A5B52a58Eea47b072`
- Router: `0x1EbDFC75FfE3ba3de61E7138a3E8706aC841Af9B`
- Nonfungible Position Manager: `0xeaD19AE861c29bBb2101E834922B2FEee69B9091`
- Quoter: `0x239F11a7A3E08f2B8110D4CA9F6B95d4c8865258`

Configuration:

- `wallet_address` (required, passed directly to constructor)
- `pool_address` (optional overall, required only for pool-scoped methods; also accepts `pool`, `projectx_pool_address`, `projectx_pool`, and checks nested `strategy` config)

Notable helpers:

- `pool_overview()`
- `current_balances()`
- `list_positions()`
- `get_full_user_state(account, include_overview=True, include_balances=True, include_positions=True, include_points=True)` (standardized snapshot wrapper)
- `mint_from_balances(tick_lower, tick_upper, slippage_bps=...)`
- `increase_liquidity_balanced(token_id, tick_lower, tick_upper, slippage_bps=...)`
- `burn_position(token_id)` (convenience wrapper over `remove_liquidity(..., collect=True, burn=True)`)
- `swap_exact_in(from_token, to_token, amount_in, slippage_bps=...)`
- `find_pool_for_pair(token_a, token_b, prefer_fees=...)`

Notes:

- ProjectX points are updated daily; missing/unchanged points immediately after activity is expected.
- Use WHYPE for native-like HYPE swaps. Native HYPE zero-address inputs are intentionally rejected.
- Prefer `mint_from_balances()` over direct `add_liquidity()` so ProjectX pool `tickSpacing()` is read live instead of inferred from a generic Uniswap fee map.
- A live smoke test is available but skipped by default: `PROJECTX_HYPEREVM_SMOKE=1 poetry run pytest -o addopts= wayfinder_paths/adapters/projectx_adapter/test_hyperevm_smoke.py -q`.
