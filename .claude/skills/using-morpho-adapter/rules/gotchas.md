# Morpho gotchas

- **Markets are isolated:** every action targets a specific `market_unique_key` (loan/collateral/oracle/IRM/LLTV are immutable per market).
- **Collateral is separate from supply:** borrowing requires `supply_collateral(...)` (not just `lend(...)`).
- **Full close uses shares:** `repay_full=True` / `withdraw_full=True` uses shares to avoid dust from interest accrual.
- **Bundler is optional:** atomic allocator+borrow requires a bundler address (`bundler_address` config or method argument).
- **Rewards are multi-source:** Merkl claims use the Merkl distributor; URD claims use Morpho distributions. Use `get_claimable_rewards(...)` first.
