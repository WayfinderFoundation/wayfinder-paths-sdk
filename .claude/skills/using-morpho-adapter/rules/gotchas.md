# Morpho gotchas

- **Markets are isolated:** every action targets a specific Morpho `marketId`. The adapter argument is still named `market_unique_key` for compatibility.
- **Markets are not vaults:** market supply/borrow uses `market_unique_key`; vault deposit/withdraw/mint/redeem uses `vault_address`.
- **Collateral is separate from supply:** borrowing requires `supply_collateral(...)` (not just `lend(...)`).
- **Full close uses shares:** `repay_full=True` / `withdraw_full=True` uses shares to avoid dust from interest accrual.
- **Bundler is optional:** atomic allocator+borrow requires a compatible bundler address (`bundler_address` config or method argument). Current Bundler3 uses `multicall(Call[])` and adapter contracts; the adapter's bundled path is for the legacy bytes-array Morpho bundler style.
- **Rewards are Merkl-first:** current Morpho rewards are Merkl-distributed by default. Legacy URD claims are opt-in and depend on historical distribution data.
- **Vault V2 deposit protection:** direct ERC-4626 vault writes do not add Morpho SDK/Bundler3 share-price slippage checks or native-token wrapping.
