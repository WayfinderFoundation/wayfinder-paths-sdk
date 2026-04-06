# Aerodrome reads (classic pools, gauges, Sugar analytics, and veAERO)

## Data accuracy (no guessing)

- Do **not** invent APRs, fee yields, reward rates, or token prices.
- Only report values fetched from the adapter, the shared Aerodrome mixin helpers, or chain-backed token helpers.
- If a call fails, respond with "unavailable" and show the exact adapter call or script.

## Primary data source

- Adapter: `wayfinder_paths/adapters/aerodrome_adapter/adapter.py`
- Shared helpers: `wayfinder_paths/adapters/aerodrome_common.py`
- README: `wayfinder_paths/adapters/aerodrome_adapter/README.md`
- Chain: Base only (`CHAIN_ID_BASE = 8453`)

## High-value reads

### Enumerate markets / gauge-enabled pools

- Call: `ok, result = await adapter.get_all_markets(start=0, limit=50, include_gauge_state=True)`
- Output: `(bool, dict)` with:
  - `protocol`, `chain_id`, `start`, `limit`, `total`
  - `markets`: list of normalized pool/gauge rows

Use this for the adapter-facing market list, not `list_pools()`.

### Sugar pool and epoch analytics

- `await adapter.sugar_all(limit=500, offset=0)` returns raw `SugarPool` rows.
- `await adapter.list_pools(page_size=500, max_pools=None)` is the easiest broad pool scan.
- `await adapter.pools_by_lp()` maps LP token address to `SugarPool`.
- `await adapter.sugar_epochs_latest(limit=...)` returns recent `SugarEpoch` rows.
- `await adapter.sugar_epochs_by_address(pool=...)` returns epoch rows for one LP token / pool.
- `await adapter.rank_pools_by_usdc_per_ve(top_n=..., limit=...)` ranks pools by latest fee-plus-bribe value per ve vote.
- `await adapter.v2_pool_tvl_usdc(pool)` estimates classic pool TVL in USDC.
- `await adapter.v2_staked_tvl_usdc(pool)` estimates staked TVL in USDC.
- `await adapter.v2_emissions_apr(pool)` estimates emissions APR for one classic gauge pool.
- `await adapter.rank_v2_pools_by_emissions_apr(top_n=..., candidate_count=...)` ranks classic pools by emissions APR.

Use these when you need broader pool analytics, including pools that are easier to reason about through Sugar than through the voter-driven `get_all_markets()` surface.

### Route quotes and single-pool resolution

- `await adapter.quote_best_route(amount_in=..., token_in=..., token_out=..., intermediates=None)` finds the best exact-in route among direct and single-intermediate candidates.
- `await adapter.get_amounts_out(amount_in, routes)` evaluates a specific route list.
- `await adapter.get_pool(tokenA=..., tokenB=..., stable=False)` resolves the pool address for a pair.
- `await adapter.get_gauge(pool=...)` returns the gauge address or a failure if no gauge exists.

### Wallet state

- `ok, state = await adapter.get_full_user_state(account="0x...", start=0, limit=200, include_votes=False, include_vote_claimables=False)`
- Output includes wallet LP balances, staked LP balances, pending emissions, and veAERO NFT information for the paged pool set.

For veAERO NFT discovery without the full pool scan:
- `ok, token_ids = await adapter.get_user_ve_nfts(owner="0x...")`

### Shared veAERO and reward reads

The classic adapter inherits these shared helpers from `aerodrome_common.py`:

- `ok, data = await adapter.ve_balance_of_nft(token_id=...)`
- `ok, data = await adapter.ve_locked(token_id=...)`
- `ok, data = await adapter.can_vote_now(token_id=...)`
- `ok, data = await adapter.get_reward_contracts(gauge=...)`
- `ok, data = await adapter.get_vote_claimables(token_id=..., include_zero_positions=False, include_usd_values=False)`
- `ok, claimable = await adapter.get_rebase_claimable(token_id=...)`
- `ok, votes = await adapter.estimate_votes_for_lock(aero_amount_raw=..., lock_duration=...)`
- `ok, apr = await adapter.estimate_ve_apr_percent(usdc_per_ve=..., votes_raw=..., aero_locked_raw=...)`

Use these when you need to inspect veNFT lock state, check whether a veNFT can vote this epoch, resolve fee and bribe reward contracts for a gauge, or estimate vote weight and ve-linked APR from adapter inputs.

## Ad-hoc read script

```python
"""Rank classic Aerodrome pools and inspect one veNFT."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.aerodrome_adapter import AerodromeAdapter

async def main() -> None:
    adapter = await get_adapter(AerodromeAdapter)
    ranked = await adapter.rank_v2_pools_by_emissions_apr(top_n=5)
    for apr, pool in ranked:
        print(pool.lp, pool.symbol, apr)

    ok, token_ids = await adapter.get_user_ve_nfts(owner="0xYourAddress")
    if ok and token_ids:
        ok_vote, vote_info = await adapter.can_vote_now(token_id=token_ids[0])
        print("can_vote=", ok_vote, vote_info)

if __name__ == "__main__":
    asyncio.run(main())
```

## Method summary

| Method | Returns | Best for |
|--------|---------|----------|
| `get_all_markets(...)` | Gauge-enabled market dict | Normalized Aerodrome market list |
| `list_pools(...)` / `sugar_all(...)` | `list[SugarPool]` | Pool scans and Sugar-backed analytics |
| `sugar_epochs_latest(...)` / `sugar_epochs_by_address(...)` | `list[SugarEpoch]` | Recent fee, bribe, and emissions data |
| `rank_pools_by_usdc_per_ve(...)` | Ranked rows | Incentive efficiency screening |
| `rank_v2_pools_by_emissions_apr(...)` | Ranked classic pools | Emissions APR screening |
| `quote_best_route(...)` / `get_amounts_out(...)` | Route quote data | Exact-in routing checks |
| `get_pool(...)` / `get_gauge(...)` | Single-address resolution | Pool-level inspection |
| `get_full_user_state(...)` | Wallet LP and veAERO snapshot | Portfolio state |
| `get_user_ve_nfts(...)`, `ve_locked(...)`, `can_vote_now(...)` | veNFT state | veAERO inspection |
| `get_vote_claimables(...)` / `get_reward_contracts(...)` | Reward-contract and claimable data | Fee and bribe analysis |
