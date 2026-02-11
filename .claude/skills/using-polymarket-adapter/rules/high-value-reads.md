# Polymarket reads (markets + orderbooks + time series)

## Data accuracy (no guessing)

- Do **not** invent prices, time series, or market status. Fetch via the adapter.
- When endpoints fail, report “unavailable” and include the exact script/call needed to retry.

## MCP shortcuts (Claude Code)

- Search markets/events: `mcp__wayfinder__polymarket(action="search", query="bitcoin daily", limit=10)`
- Trending markets: `mcp__wayfinder__polymarket(action="trending", limit=25)`
- Market metadata by slug: `mcp__wayfinder__polymarket(action="get_market", market_slug="...")`
- Price history (token_id): `mcp__wayfinder__polymarket(action="price_history", token_id="...", interval="1d", fidelity=5)`
- Full user status: `mcp__wayfinder__polymarket(action="status", wallet_label="main")`

## Primary sources (in this repo)

- Adapter: `wayfinder_paths/adapters/polymarket_adapter/adapter.py`
- Adapter docs: `wayfinder_paths/adapters/polymarket_adapter/README.md`
- Constants (addresses + base URLs): `wayfinder_paths/core/constants/polymarket.py`

## Mental model (IDs you will see)

- **Market slug** (Gamma): use `get_market_by_slug(slug)` and show to users.
- **CLOB token id** (Gamma `clobTokenIds`): one per outcome; use for CLOB price/book/history and for trading.
- **ConditionId** (Gamma `conditionId`): on-chain identifier; use for redemption after resolution.

## Recommended market discovery flow

1) Start with fuzzy search:
   - `search_markets_fuzzy(query=..., limit=...)` (Gamma `public-search` + local rerank)
2) Filter to tradable markets (every time):
   - `enableOrderBook == True`
   - `clobTokenIds` present/non-empty
   - `acceptingOrders == True`
   - `active == True` and `closed != True`
3) If search results aren’t tradable, fall back to “trending”:
   - `list_markets(closed=False, order="volume24hr", ascending=False, limit=50)`

Practical note: Gamma often returns `outcomes`, `outcomePrices`, and `clobTokenIds` as JSON-encoded strings. The adapter normalizes these into Python lists.

## Time series (CLOB `prices-history`)

Use `get_prices_history(token_id=..., ...)`:

- `hist["history"]` is a list of `{ "t": <unix_ts>, "p": <price> }`
- Treat `p` as **implied probability** (0–1)

If you’re analyzing a market by slug, use `get_market_prices_history(market_slug=..., outcome=...)` (slug → token id → history).

## Ad-hoc analysis scripts (copy/paste)

### Search a topic and print tradable candidates

```python
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.polymarket_adapter.adapter import PolymarketAdapter

def is_tradable(m: dict) -> bool:
    return bool(m.get("enableOrderBook") and m.get("clobTokenIds") and m.get("acceptingOrders") and m.get("active") and not m.get("closed"))

async def main():
    a = get_adapter(PolymarketAdapter)
    ok, rows = await a.search_markets_fuzzy(query="super bowl mvp", limit=25)
    assert ok, rows
    tradable = [m for m in rows if is_tradable(m)]
    for m in tradable[:10]:
        print(m["slug"], "|", m.get("question"))
    await a.close()

asyncio.run(main())
```

### “Mover” scan across an event (compute deltas from time series)

Use `get_event_by_slug(event_slug)` to get a coherent set (e.g. MVP candidates), then pull per-outcome history. Limit concurrency to avoid 429s.

```python
import asyncio, time
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.polymarket_adapter.adapter import PolymarketAdapter

def hist_points(hist: dict) -> list[tuple[int, float]]:
    pts = []
    for p in hist.get("history") or []:
        if isinstance(p, dict) and "t" in p and "p" in p:
            pts.append((int(p["t"]), float(p["p"])))
    return sorted(pts, key=lambda x: x[0])

async def main():
    a = get_adapter(PolymarketAdapter)
    ok, ev = await a.get_event_by_slug("super-bowl-lx-mvp")
    assert ok, ev

    markets = [m for m in (ev.get("markets") or []) if isinstance(m, dict) and m.get("enableOrderBook") and m.get("clobTokenIds")]
    now = int(time.time())
    start = now - 24 * 3600

    sem = asyncio.Semaphore(6)
    async def fetch_delta(m: dict):
        ok_tid, tid = a.resolve_clob_token_id(market=m, outcome="YES")
        if not ok_tid:
            ok_tid, tid = a.resolve_clob_token_id(market=m, outcome=0)  # fallback for non-YES/NO markets
        if not ok_tid:
            return None
        async with sem:
            ok, hist = await a.get_prices_history(token_id=tid, interval=None, start_ts=start, end_ts=now, fidelity=60)
        if not ok or not isinstance(hist, dict):
            return None
        pts = hist_points(hist)
        if not pts:
            return None
        return (m.get("slug"), pts[0][1], pts[-1][1], pts[-1][1] - pts[0][1])

    rows = [r for r in await asyncio.gather(*[fetch_delta(m) for m in markets]) if r]
    rows.sort(key=lambda r: r[3], reverse=True)
    for slug, p0, p1, dp in rows[:10]:
        print(f"{slug}: {p0:.3f} -> {p1:.3f} ({dp:+.3f})")

    await a.close()

asyncio.run(main())
```

## Method summary

| Method | Returns | Best for |
| --- | --- | --- |
| `search_markets_fuzzy(query, ...)` | list | Fuzzy discovery by text |
| `list_markets(...)` | list | Trending / filtered scans |
| `get_market_by_slug(slug)` | dict | Market metadata + IDs |
| `get_event_by_slug(slug)` | dict | Market sets (MVP, brackets, etc.) |
| `get_price(token_id)` | dict | Current price |
| `get_order_book(token_id)` | dict | Book snapshot |
| `get_prices_history(token_id, ...)` | dict | Historic time series |
| `get_positions(user)` | list | Exposure snapshot |
| `get_trades(...)` / `get_activity(...)` | list | “What happened” history |
| `get_full_user_state(account, ...)` | dict | One-shot status (positions + balances + PnL; open orders if wallet configured) |
