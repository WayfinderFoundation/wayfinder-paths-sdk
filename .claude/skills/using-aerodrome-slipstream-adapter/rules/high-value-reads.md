# Aerodrome Slipstream reads (concentrated liquidity + gauges)

## Data accuracy (no guessing)

- Do **not** invent ticks, prices, fee growth, or reward rates.
- Use the adapter’s pool, market, and position reads directly.
- If a call fails, return "unavailable" and include the exact script or adapter call.

## Primary data source

- Adapter: `wayfinder_paths/adapters/aerodrome_slipstream_adapter/adapter.py`
- Manifest: `wayfinder_paths/adapters/aerodrome_slipstream_adapter/manifest.yaml`
- Chain: Base only (`CHAIN_ID_BASE = 8453`)

## High-value reads

### Enumerate markets

- `ok, result = await adapter.get_all_markets(start=0, limit=50, deployments=None, include_gauge_state=True)`
- Output: `(bool, dict)` with:
  - `protocol`, `chain_id`, `chain_name`, `deployments`, `start`, `limit`, `total`
  - `markets`: normalized Slipstream pool rows

Use this as the repo-convention market list for Slipstream.

### Discover pools

- `await adapter.find_pools(tokenA=..., tokenB=..., tick_spacings=None, deployments=...)`
- `await adapter.get_pool(tokenA=..., tokenB=..., tick_spacing=..., deployment_variant=...)`
- `await adapter.get_gauge(pool=...)`

These are the core discovery reads when you already know the pair or want a single pool.

### Position reads

- `ok, pos = await adapter.get_pos(token_id=..., position_manager=None, account="0x...", include_usd=False)`
- `ok, state = await adapter.get_full_user_state(account="0x...", deployments=None, include_usd=False, include_zero_positions=False, include_votes=False)`

Use `get_pos(...)` for one NFT and `get_full_user_state(...)` for the full wallet view across deployments.

### veAERO reads

The Slipstream adapter inherits the same veAERO read helpers as the classic Aerodrome adapter:
- `get_user_ve_nfts(owner=...)`
- `get_reward_contracts(gauge=...)`

## Ad-hoc read script

```python
"""List Slipstream markets across default Base deployments."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.aerodrome_slipstream_adapter import AerodromeSlipstreamAdapter

async def main() -> None:
    adapter = await get_adapter(AerodromeSlipstreamAdapter)
    ok, result = await adapter.get_all_markets(limit=20)
    if not ok:
        raise RuntimeError(result)
    print("deployments=", result["deployments"])
    for market in result["markets"][:5]:
        print(
            market.get("pool"),
            market.get("deployment_variant"),
            market.get("gauge"),
        )

if __name__ == "__main__":
    asyncio.run(main())
```

## Method summary

| Method | Returns | Best for |
|--------|---------|----------|
| `get_all_markets(...)` | Deployment-aware market dict | Normalized Slipstream market list |
| `find_pools(...)` | Pool rows | Pool discovery |
| `get_pool(...)` / `get_gauge(...)` | Single pool / gauge data | Focused inspection |
| `get_pos(...)` | One NFT position | Position-level debugging |
| `get_full_user_state(...)` | Wallet position snapshot | Portfolio reporting |
| `get_user_ve_nfts(...)` | veNFT token ids | veAERO inventory |
