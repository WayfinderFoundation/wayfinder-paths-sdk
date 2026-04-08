# Aerodrome Slipstream reads (deployment-aware CL pools, positions, analytics, and veAERO)

## Data accuracy (no guessing)

- Do **not** invent ticks, prices, fee growth, or reward rates.
- Use the adapter’s pool, market, position, and shared veAERO reads directly.
- If a call fails, return "unavailable" and include the exact script or adapter call.

## Primary data source

- Adapter: `wayfinder_paths/adapters/aerodrome_slipstream_adapter/adapter.py`
- Shared helpers: `wayfinder_paths/adapters/aerodrome_common.py`
- Manifest: `wayfinder_paths/adapters/aerodrome_slipstream_adapter/manifest.yaml`
- Chain: Base only (`CHAIN_ID_BASE = 8453`)

## High-value reads

### Enumerate markets and discover pools

- `ok, result = await adapter.get_all_markets(start=0, limit=50, deployments=None, include_gauge_state=True)`
- Output: `(bool, dict)` with:
  - `protocol`, `chain_id`, `chain_name`, `deployments`, `start`, `limit`, `total`
  - `markets`: normalized Slipstream pool rows

Use this as the repo-convention market list for Slipstream.

- `await adapter.find_pools(tokenA=..., tokenB=..., tick_spacings=None, deployments=...)`
- `await adapter.get_pool(tokenA=..., tokenB=..., tick_spacing=..., deployment_variant=...)`
- `await adapter.get_gauge(pool=...)`
- `await adapter.slipstream_best_pool_for_pair(tokenA=..., tokenB=..., deployments=...)`

Use these when you know the pair and need to resolve the pool set, a single pool, a gauge, or the deepest-liquidity pool across deployments.

### Pool and range analytics

- `ok, data = await adapter.slipstream_pool_state(pool=...)`
- `ok, metrics = await adapter.slipstream_range_metrics(pool=..., tick_lower=..., tick_upper=..., amount0_raw=..., amount1_raw=...)`
- `ok, data = await adapter.slipstream_volume_usdc_per_day(pool=..., lookback_blocks=..., max_logs=...)`
- `ok, data = await adapter.slipstream_fee_apr_percent(metrics=..., volume_usdc_per_day=..., expected_in_range_fraction=1.0)`
- `ok, data = await adapter.slipstream_sigma_annual_from_swaps(pool=..., lookback_blocks=..., max_logs=...)`
- `ok, data = await adapter.slipstream_prob_in_range_week(pool=..., tick_lower=..., tick_upper=..., sigma_annual=...)`

These are analytics helpers implemented in the adapter itself. They rely on on-chain pool state, swap logs, and token metadata from the shared helpers.

### Position and wallet reads

- `ok, pos = await adapter.get_pos(token_id=..., position_manager=None, account="0x...", include_usd=False)`
- `ok, state = await adapter.get_full_user_state(account="0x...", deployments=None, include_usd=False, include_zero_positions=False, include_votes=False, include_vote_claimables=False)`

Use `get_pos(...)` for one NFT and `get_full_user_state(...)` for the full wallet view across deployments.

### Shared veAERO reads

The Slipstream adapter inherits the same shared veAERO helpers as the classic adapter:

- `get_user_ve_nfts(owner=...)`
- `ve_balance_of_nft(token_id=...)`
- `ve_locked(token_id=...)`
- `can_vote_now(token_id=...)`
- `get_reward_contracts(gauge=...)`
- `get_vote_claimables(token_id=..., deployments=..., include_zero_positions=False, include_usd_values=False)`
- `get_rebase_claimable(token_id=...)`
- `estimate_votes_for_lock(aero_amount_raw=..., lock_duration=...)`
- `estimate_ve_apr_percent(usdc_per_ve=..., votes_raw=..., aero_locked_raw=...)`

`can_vote_now(...)` reports epoch metadata derived from `lastVoted`; it does **not** apply the first-hour / last-hour vote-window restrictions. For transaction safety, rely on `vote(..., check_window=True)` or `reset_vote(..., check_window=True)`.

Use these when you need veNFT inventory, lock metadata, epoch and last-voted metadata, reward-contract discovery, or vote-claimable inspection around Slipstream gauges.

## Ad-hoc read script

```python
"""Pick a Slipstream pool, then run range analytics."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.aerodrome_slipstream_adapter import AerodromeSlipstreamAdapter

async def main() -> None:
    adapter = await get_adapter(AerodromeSlipstreamAdapter)
    ok, best = await adapter.slipstream_best_pool_for_pair(
        tokenA="0x4200000000000000000000000000000000000006",
        tokenB="0x940181a94A35A4569E4529A3CDfB74e38FD98631",
    )
    if not ok:
        raise RuntimeError(best)

    ok, metrics = await adapter.slipstream_range_metrics(
        pool=best["pool"],
        tick_lower=best["slot0"]["tick"] - 600,
        tick_upper=best["slot0"]["tick"] + 600,
        amount0_raw=10**15,
        amount1_raw=10**18,
    )
    print(metrics)

if __name__ == "__main__":
    asyncio.run(main())
```

## Method summary

| Method | Returns | Best for |
|--------|---------|----------|
| `get_all_markets(...)` | Deployment-aware market dict | Normalized Slipstream market list |
| `find_pools(...)`, `get_pool(...)`, `slipstream_best_pool_for_pair(...)` | Pool rows / best market | Pool discovery and selection |
| `slipstream_pool_state(...)` | Pool state dict | Current price, fee, and liquidity inspection |
| `slipstream_range_metrics(...)` | Range metrics dict | Position sizing and active-liquidity share |
| `slipstream_volume_usdc_per_day(...)`, `slipstream_fee_apr_percent(...)` | Analytics dicts | Fee-rate estimation |
| `slipstream_sigma_annual_from_swaps(...)`, `slipstream_prob_in_range_week(...)` | Analytics dicts | Volatility and range probability |
| `get_pos(...)` | One NFT position | Position-level debugging |
| `get_full_user_state(...)` | Wallet position and veNFT snapshot | Portfolio reporting |
| `get_user_ve_nfts(...)`, `ve_locked(...)`, `get_vote_claimables(...)` | veNFT state | veAERO inspection |
