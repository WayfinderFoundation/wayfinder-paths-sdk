# High-value reads (adapter-first)

Prefer pulling onchain data via `AerodromeAdapter` rather than scraping UI.

## Pool & incentives discovery

- `await adapter.list_pools()`  
  Returns Sugar “pool rows” with gauge/bribe/fee reward contract addresses, reserves, emissions rate, etc.

- `await adapter.pools_by_lp()`  
  Convenience lookup map: `{lp_address -> SugarPool}`.

- `await adapter.rank_v2_pools_by_emissions_apr(top_n=..., candidate_count=...)`  
  Heuristic ranking for **gauge emissions APR** (AERO emissions / staked TVL).

- `await adapter.rank_pools_by_usdc_per_ve(top_n=..., limit=..., require_all_prices=...)`  
  Uses Sugar `epochsLatest` to estimate **fees+bribes per veAERO vote** (priced into USDC).

## Wallet / position snapshot

- `await adapter.get_full_user_state(account=..., include_usd_values=..., include_slipstream=..., multicall_chunk_size=...)`  
  Pulls:
  - v2 LP balances + gauge stakes + earned rewards
  - veNFTs (locks, voting power, last voted, used weights)
  - claimables (rebase, fees, bribes)
  - Slipstream position NFTs (optional)

## Slipstream analytics

- `await adapter.slipstream_pool_state(pool=...)`  
  Current tick, liquidity, fee parameters.

- `await adapter.slipstream_range_metrics(pool=..., tick_lower=..., tick_upper=..., amount0_raw=..., amount1_raw=...)`  
  Liquidity share + composition for a proposed range.

- `await adapter.slipstream_volume_usdc_per_day(pool=..., lookback_blocks=..., max_logs=...)`  
  Swap-log based volume approximation.

- `await adapter.slipstream_sigma_annual_from_swaps(...)` + `await adapter.slipstream_prob_in_range_week(...)`  
  Crude volatility + “stay-in-range” probability heuristics.

## Recommended wiring (local scripts/strategies)

For local scripts, use:

```python
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.aerodrome_adapter.adapter import AerodromeAdapter

adapter = get_adapter(AerodromeAdapter, "main", config_path="config.json")
```

This auto-loads config, wires `strategy_wallet`, and injects a signing callback when required.

