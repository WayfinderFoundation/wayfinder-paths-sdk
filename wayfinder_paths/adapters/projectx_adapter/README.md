# ProjectXLiquidityAdapter

Adapter for interacting with ProjectX (HyperEVM Uniswap v3 fork) concentrated liquidity:

- Pool overview + tick spacing
- List wallet-owned NPM positions
- Mint / increase / decrease liquidity
- Collect fees + burn positions
- Exact-input swaps via PRJX Router

The adapter can target any ProjectX v3 pool by passing `pool_address` when constructing it,
and you can resolve a pool for a token pair via `find_pool_for_pair()`.
