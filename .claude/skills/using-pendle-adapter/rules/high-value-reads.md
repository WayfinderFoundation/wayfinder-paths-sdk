# Pendle reads (markets + time series)

## Data accuracy (no guessing)

- Do **not** invent APYs/TVL/price series. Fetch from Pendle endpoints via the adapter.
- If calls fail (network/auth), respond "unavailable" and include the exact script needed.

## Primary data source

- Adapter: `wayfinder_paths/adapters/pendle_adapter/adapter.py`
- Base URL: `https://api-v2.pendle.finance/core`

## PTs vs YTs (quick mental model)

- **PT (Principal Token):** "fixed yield" leg; `fixedApy` = Pendle `impliedApy`
- **YT (Yield Token):** "floating yield" leg; `floatingApy` = `underlyingApy - impliedApy`

## Chain IDs

- `arbitrum` → `42161`
- `base` → `8453`
- `hyperevm` → `999`

## Ad-hoc read scripts

### List markets with APYs (RECOMMENDED)

Use `list_active_pt_yt_markets()` - it returns **flattened, normalized** data:

```python
"""Fetch Pendle markets on a chain."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.pendle_adapter import PendleAdapter

async def main():
    adapter = get_adapter(PendleAdapter)
    markets = await adapter.list_active_pt_yt_markets(chain=8453)  # or "base"

    print(f"Found {len(markets)} markets")
    for m in sorted(markets, key=lambda x: x.get("fixedApy", 0), reverse=True):
        print(f"{m['name']:<25} implied={m['fixedApy']:.2%} underlying={m['underlyingApy']:.2%} liq=${m['liquidityUsd']:,.0f}")

if __name__ == "__main__":
    asyncio.run(main())
```

Output fields from `list_active_pt_yt_markets()`:
- `name`, `marketAddress`, `ptAddress`, `ytAddress`, `syAddress`, `underlyingAddress`
- `fixedApy`, `underlyingApy`, `floatingApy`
- `liquidityUsd`, `volumeUsd24h`, `totalTvlUsd`
- `expiry`, `daysToExpiry`

### Raw API data (use with caution)

`fetch_markets()` returns raw Pendle API response - data is **nested under `details`**:

```python
"""Fetch raw Pendle API data (note: data nested in 'details')."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.pendle_adapter import PendleAdapter

async def main():
    adapter = get_adapter(PendleAdapter)
    result = await adapter.fetch_markets(chain_id=8453, is_active=True)

    for m in result.get("markets", []):
        details = m.get("details", {})  # <-- DATA IS NESTED HERE
        print(f"{m['name']}: implied={details.get('impliedApy', 0):.2%} liq=${details.get('liquidity', 0):,.0f}")

if __name__ == "__main__":
    asyncio.run(main())
```

### Market snapshot

```python
"""Fetch snapshot for a specific market."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.pendle_adapter import PendleAdapter

MARKET = "0x5d6e67fce4ad099363d062815b784d281460c49b"  # yoETH on Base

async def main():
    adapter = get_adapter(PendleAdapter)
    snapshot = await adapter.fetch_market_snapshot(chain_id=8453, market_address=MARKET)
    print(snapshot)

if __name__ == "__main__":
    asyncio.run(main())
```

### Market history (time series)

```python
"""Fetch historical data for a market."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.pendle_adapter import PendleAdapter

MARKET = "0x5d6e67fce4ad099363d062815b784d281460c49b"

async def main():
    adapter = get_adapter(PendleAdapter)
    history = await adapter.fetch_market_history(
        chain_id=8453,
        market_address=MARKET,
        time_frame="day",
    )
    for row in history.get("results", [])[-5:]:
        print(row)

if __name__ == "__main__":
    asyncio.run(main())
```

## Method summary

| Method | Returns | Best for |
|--------|---------|----------|
| `list_active_pt_yt_markets(chain)` | Flattened list | Market discovery, scanners |
| `fetch_markets(chain_id)` | Raw API (nested `details`) | When you need all raw fields |
| `fetch_market_snapshot(chain_id, market)` | Single market state | Point-in-time checks |
| `fetch_market_history(chain_id, market)` | Time series | Historical analysis |
