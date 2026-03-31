# Aerodrome reads (classic pools + gauges + veAERO state)

## Data accuracy (no guessing)

- Do **not** invent APRs, fee yields, reward rates, or token prices.
- Only report values fetched from the adapter or chain-backed helper methods.
- If a call fails, respond with "unavailable" and show the exact adapter call or script.

## Primary data source

- Adapter: `wayfinder_paths/adapters/aerodrome_adapter/adapter.py`
- README: `wayfinder_paths/adapters/aerodrome_adapter/README.md`
- Chain: Base only (`CHAIN_ID_BASE = 8453`)

## High-value reads

### Enumerate markets / gauge-enabled pools

- Call: `ok, result = await adapter.get_all_markets(start=0, limit=50, include_gauge_state=True)`
- Output: `(bool, dict)` with:
  - `protocol`, `chain_id`, `start`, `limit`, `total`
  - `markets`: list of normalized pool/gauge rows
- Use this for the adapter-facing "market list" view, not `list_pools()`.

### Sugar lens pool discovery

- `await adapter.sugar_all(limit=500, offset=0)` returns raw `SugarPool` rows.
- `await adapter.list_pools(page_size=500, max_pools=None)` is the easiest broad pool scan.
- `await adapter.pools_by_lp()` maps LP token address to `SugarPool`.

Use these when you need broader pool analytics, including pools that are easier to reason about through Sugar than through the gauge-first `get_all_markets()` surface.

### Route / swap reads

- `await adapter.quote_best_route(token_in, token_out, amount_in)` finds the best route for an exact-in swap.
- `await adapter.get_amounts_out(amount_in, routes)` evaluates a specific route list.

These are quoting helpers only. They do not broadcast a swap transaction.

### Gauge / incentive analytics

- `await adapter.sugar_epochs_latest(limit=...)` returns recent Sugar epoch rows.
- `await adapter.sugar_epochs_by_address(lp=...)` gets epoch data for a specific LP token.
- `await adapter.rank_pools_by_usdc_per_ve(limit=...)` ranks pools by incentive efficiency.
- `await adapter.get_gauge(pool=...)` resolves the gauge address for a pool.

### Single-pool reads

- `await adapter.get_pool(tokenA=..., tokenB=..., stable=False)` resolves the pool address for a pair.
- `await adapter.get_gauge(pool=...)` returns the gauge address or a failure if no gauge exists.

### Wallet state

- `ok, state = await adapter.get_full_user_state(account="0x...", include_votes=False)`
- Output includes wallet LP balances, staked LP balances, pending emissions, and veAERO NFT information for the paged pool set.

For veAERO NFT discovery without the full pool scan:
- `ok, token_ids = await adapter.get_user_ve_nfts(owner="0x...")`

## Ad-hoc read script

```python
"""Scan Aerodrome gauge-enabled markets on Base."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.aerodrome_adapter import AerodromeAdapter

async def main() -> None:
    adapter = await get_adapter(AerodromeAdapter)
    ok, result = await adapter.get_all_markets(limit=25)
    if not ok:
        raise RuntimeError(result)
    print("total=", result["total"])
    for market in result["markets"][:5]:
        print(
            market.get("pool"),
            market.get("stable"),
            market.get("gauge"),
            market.get("gauge_reward_rate"),
        )

if __name__ == "__main__":
    asyncio.run(main())
```

## Method summary

| Method | Returns | Best for |
|--------|---------|----------|
| `get_all_markets(...)` | Gauge-enabled market dict | Normalized Aerodrome market list |
| `list_pools(...)` | `list[SugarPool]` | Pool scans and UI filtering |
| `sugar_all(...)` | `list[SugarPool]` | Raw Sugar analytics |
| `pools_by_lp()` | `dict[lp, SugarPool]` | LP token lookups |
| `quote_best_route(...)` | Best route quote | Swap routing |
| `get_amounts_out(...)` | Per-hop output amounts | Route verification |
| `sugar_epochs_latest(...)` | `list[SugarEpoch]` | Recent fees/bribes/emissions |
| `rank_pools_by_usdc_per_ve(...)` | Ranked pool rows | Incentive screening |
| `get_pool(...)` / `get_gauge(...)` | Single-address resolution | Pool-level inspection |
| `get_full_user_state(...)` | Wallet LP / veAERO snapshot | Portfolio state |
| `get_user_ve_nfts(...)` | veNFT token ids | veAERO inventory |
