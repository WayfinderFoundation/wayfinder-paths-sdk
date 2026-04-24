# Delta Lab v2 client surface

The Python client now exposes the full Delta Lab API (entity / catalog /
graph / search / point TS / latest / bulk / orchestration) on top of the
legacy `get_basis_apy_sources`, `get_top_apy`, `get_asset_timeseries`,
`screen_*` methods (all still available).

**All new methods are on `DELTA_LAB_CLIENT`** (same singleton).

## Output-size guardrails (pick the right tool)

| Need | Use | Size |
|---|---|---|
| Current price + 1d/7d/30d/90d returns + vol + mdd | `get_asset_price_latest(asset_id=…)` | <1 KB |
| Full lending screening snapshot (50 fields) | `get_market_lending_latest(market_id, asset_id)` | ~1.5 KB |
| Discover top opportunities for a symbol | `search_opportunities(basis_root=…, side="LONG", limit=25)` | ~5 KB |
| Full analytic opportunity payload | `get_basis_apy_sources(basis_symbol=…, limit=25)` | 1.3 MB @ limit=500 — **cap low** |
| Single-asset discovery bundle | `explore(symbol=…, relations_depth=1)` | ~22 KB |
| Backtest data (opps + funding TS + lending TS) | `fetch_backtest_bundle(...)` | 50–500 KB, scripts only |
| Walk the full catalog | `iter_list("/list/basis-roots/", batch=100)` | unbounded — Python scripts only |

**Never** default to `limit=500` on opportunity endpoints in agent
contexts. Default `limit=25` lands under 10 KB for every search surface.

## Composition recipes (chain calls for hard asks)

Most single methods are narrow primitives. When one method doesn't give
you what you want, **compose** — discovery → drill-down → fan-out is the
standard flow. Key recipes:

### "Lending backtest data for X basis"

`fetch_backtest_bundle(basis_root="ETH", side="LONG")` ranks across **all**
instrument types; if Boros/Pendle opps dominate the ranking, `lending_ts`
comes back empty even when lending markets exist for that basis. The
bundle API has no `instrument_type` filter. For lending-only, compose:

```python
# 1) Pick the lending opps you care about
page = await DELTA_LAB_CLIENT.search_opportunities(
    basis_root="ETH", side="LONG", instrument_type="LENDING_SUPPLY", limit=20
)
opps = page["items"]
pairs = [(o["market_id"], o["deposit_asset_id"]) for o in opps]
inst_ids = [o["instrument_id"] for o in opps if o.get("instrument_id")]

# 2) Fan out the TS directly (auto-chunked at 100)
lending_ts = await DELTA_LAB_CLIENT.bulk_lending(pairs=pairs, lookback_days=30)
funding_ts = await DELTA_LAB_CLIENT.bulk_funding(instrument_ids=inst_ids, lookback_days=30)
```

Same pattern works for `instrument_type="PERP" / "PENDLE_PT" / "BOROS_MARKET"`.

### "What wraps/derives from X?"

`get_asset_relations(asset_id=X, depth=1)` on a hub like ETH returns 100+
rows (WRAPS + BASIS + REBASING_TO_BASE, all in one list). Use
`summarize_asset_relations` first, then drill in by type:

```python
summary = await DELTA_LAB_CLIENT.summarize_asset_relations(asset_id=2, depth=1)
# -> {"by_relation_type": {"WRAPS": {"count": 86, "examples": [...]}, ...}}
# If the user wanted the full list of wrappers:
wraps = await DELTA_LAB_CLIENT.get_asset_relations(
    asset_id=2, depth=1, relation_types="WRAPS"
)
```

### "Top APY across multiple symbols"

Don't loop `get_basis_apy_sources` per symbol (1.3 MB each). Use
`search_opportunities` with no `basis_root` filter, or the legacy
`get_top_apy`. For specific drill-downs, chain: `search_opportunities`
(pick the winner's `instrument_id` + `market_id`) → `get_basis_apy_sources`
on that one symbol to get the full analytic payload for just the chosen
opp.

### "Current price + vol for N assets"

```python
# Single call, auto-chunks at 100; returns dict[asset_id, PriceLatest | None]
snapshots = await DELTA_LAB_CLIENT.bulk_latest_prices(asset_ids=[1, 2, 3, ...])
```

Not `asyncio.gather` over `get_asset_price_latest` — that wastes requests.

### "Screen lending markets for X basis by current APR"

```python
# Discover the candidate markets
page = await DELTA_LAB_CLIENT.search_markets(
    market_type="LENDING", basis_root="ETH", limit=50
)
pairs = [(m["market_id"], m["asset_id"]) for m in page["items"]]

# Pull latest snapshots for all — one call, full screening feature set
latest = await DELTA_LAB_CLIENT.bulk_latest_lending(pairs=pairs)
ranked = sorted(
    [v for v in latest.values() if v is not None],
    key=lambda l: l.net_supply_apr_now or 0,
    reverse=True,
)
```

### "Protocol-native address → full market TS"

```python
m = await DELTA_LAB_CLIENT.get_market_by_venue_external(
    venue="aave-bsc", external_id="0x6807dc923806fe8fd134338eabca509979a7e0cb"
)
df = await DELTA_LAB_CLIENT.get_market_lending_ts(
    market_id=m.market_id, asset_id=2, lookback_days=30
)
```

### "All markets for symbol X on chain Y"

`get_asset_markets(symbol=..., chain_id=...)` filters on the **asset's**
chain, not the market's. For cross-chain tokens (ETH, BTC, USDC, …) the
asset record has `asset_chain_id = null`, so any non-null `chain_id`
here returns zero rows. Two correct compositions:

```python
# A) Use search_markets — its chain_id filters on market.chain_id
page = await DELTA_LAB_CLIENT.search_markets(basis_root="ETH", chain_id=56)

# B) Unfiltered get_asset_markets + client-side filter
rows = await DELTA_LAB_CLIENT.get_asset_markets(symbol="ETH")
bsc_rows = [r for r in rows if r["market_chain_id"] == 56]
```

A is preferred for bounded queries (honours `limit` + `has_more`); B is
fine for small scans where you want every role (`BASE`, `LENDING_ASSET`,
…) including the non-listed ones.

### "Cross-chain path between two assets"

```python
# First confirm they're connected
paths = await DELTA_LAB_CLIENT.get_graph_paths(
    from_asset_id=2, to_asset_id=1, max_hops=3
)
# Then follow up with instrument-level queries on the intermediate hops
```

### General pattern

`search_*` to find IDs → `bulk_*` or `*_latest` to hydrate → typed
records or DataFrames to consume. Avoid the enriched analytic endpoints
(`get_basis_apy_sources`, full `get_asset_timeseries`) until you've
narrowed to a specific opp / asset / market.

## Error handling

```python
from wayfinder_paths.core.clients.delta_lab_types import DeltaLabAPIError

try:
    ai = await DELTA_LAB_CLIENT.get_asset_by_id(asset_id=99_999_999)
except DeltaLabAPIError as exc:
    exc.code        # "not_found", "bulk_cap_exceeded", "invalid_parameter", …
    exc.status      # HTTP status
    exc.message     # human message
```

`*_latest(...)` methods return `None` instead of raising on `not_found` —
sparse snapshots are a normal state (e.g. `get_asset_yield_latest(2)`
returns `None` because base ETH has no yield snapshot; Boros markets
have no perp `funding/latest/`). Other error codes still raise.

## Two "opportunity" shapes — easy to confuse

- **Discovery** (`search_opportunities(...)`) — `~14 fields` per row:
  `side, venue, chain_id, market_id, maturity_ts, basis_symbol,
  instrument_id, instrument_type, deposit/exposure/receipt asset ids+symbols`.
  Use this to **scan** or find an `instrument_id`.
- **Analytic** (`get_basis_apy_sources(...)`) — the enriched payload with
  `apy.*, risk.*, opportunity.*, summary.instrument_type_counts,
  warnings`, grouped into `directions.LONG[] / directions.SHORT[]`. Use
  this to **decide** on a specific opportunity.

## Entity lookups (typed returns)

```python
ai = await DELTA_LAB_CLIENT.get_asset_by_id(asset_id=2)         # AssetInfo
rows = await DELTA_LAB_CLIENT.get_asset_markets(symbol="ETH")   # list[dict]
v  = await DELTA_LAB_CLIENT.get_venue_by_id(venue_id=7)         # VenueInfo
v2 = await DELTA_LAB_CLIENT.get_venue_by_name(name="aave-bsc")  # VenueInfo
m  = await DELTA_LAB_CLIENT.get_market_by_id(market_id=912)     # MarketInfo
m2 = await DELTA_LAB_CLIENT.get_market_by_venue_external(
    venue="aave-bsc", external_id="0x6807dc…"
)
i  = await DELTA_LAB_CLIENT.get_instrument_by_id(instrument_id=40778)  # InstrumentInfo
```

Every typed record carries a `.raw: dict` with the full server payload so
forward-compatible fields are still reachable.

## Catalogs

```python
roots = await DELTA_LAB_CLIENT.list_basis_roots(limit=25, offset=0)
# -> {items, count, total_count} — total_count is ~3,891.
# Server sorts by symbol ASCII-ascending: numeric-prefixed symbols
# (0G, 10, 100M, 1HR, 1INCH…) come before alphabetic ones. If you
# want alphabetic-only, filter client-side (`[r for r in items if r["symbol"][:1].isalpha()]`).
members = await DELTA_LAB_CLIENT.list_basis_members(root_symbol="ETH")  # list
venues  = await DELTA_LAB_CLIENT.list_venues(venue_type="LENDING")     # list
chains  = await DELTA_LAB_CLIENT.list_chains()                          # list
types   = await DELTA_LAB_CLIENT.list_instrument_types()                # list
```

**Pagination walker** (scripts only — the full basis-roots catalog is
~3,900 items, 468 KB):

```python
async for item in DELTA_LAB_CLIENT.iter_list("/list/basis-roots/", batch=100):
    ...
```

## Graph

```python
rels = await DELTA_LAB_CLIENT.get_asset_relations(
    asset_id=2,
    direction="both",        # {"forward", "backward", "both"}
    depth=1,                 # 1..3 — depth=2 on ETH is 202 KB
    relation_types="WRAPS",                       # or…
    # relation_types=["WRAPS", "REBASING_TO_BASE"],  # list accepted, CSV-joined
)
paths = await DELTA_LAB_CLIENT.get_graph_paths(
    from_asset_id=2, to_asset_id=1, max_hops=3,
    relation_types=["WRAPS", "REBASING_TO_BASE"],
)
```

**For the typical "what wraps / derives from X?" ask**, the raw list is
large (ETH depth-1 = 112 rows). Use `summarize_asset_relations(...)`
instead — same inputs, returns a compact by-relation-type summary that
fits in an agent turn:

```python
s = await DELTA_LAB_CLIENT.summarize_asset_relations(asset_id=2, depth=1)
# {"asset_id": 2, "total": 112, "by_relation_type": {
#   "WRAPS":           {"count": 86, "examples": ["wstETH", "sfrxETH", "rETH"]},
#   "BASIS":           {"count": 25, "examples": [...]},
#   "REBASING_TO_BASE":{"count": 1,  "examples": ["stETH"]},
# }, "items": [...full list if needed]}
```

## Search (returns `{items, count, has_more, offset}`)

```python
# q= and query= are synonyms (matches legacy search_assets kwarg)
await DELTA_LAB_CLIENT.search_assets_v2(q="ETH", chain_id=1, limit=25)
# equivalent: search_assets_v2(query="ETH", chain_id=1, limit=25)
await DELTA_LAB_CLIENT.search_markets(venue="aave-bsc", market_type="LENDING")
await DELTA_LAB_CLIENT.search_instruments(instrument_type="PERP", basis_root="ETH")
await DELTA_LAB_CLIENT.search_opportunities(
    basis_root="ETH", side="LONG",
    venue="hyperliquid",          # filter opportunities to a specific venue
    chain_id=42161,
    instrument_type="PERP",
)
await DELTA_LAB_CLIENT.search_venues(venue_type="LENDING")
```

`search_opportunities` accepts `basis_root`, `side` (`LONG`/`SHORT`),
`venue`, `chain_id`, and `instrument_type` filters — use them server-side
rather than filtering the returned list, so the page window stays on the
matching rows. Example: "top 10 LONG ETH opportunities on Hyperliquid" =
`search_opportunities(basis_root="ETH", side="LONG", venue="hyperliquid", limit=10)`.

- `search_assets_v2` is **distinct** from the legacy `search_assets`
  (different endpoint: `/search/assets/` vs `/assets/search/`). Both
  remain available.
- Pass `fields="full"` to disable field projection on
  `search_assets_v2` and `search_markets` (no-op elsewhere).
- Walk pagination with `search_all(fn, **kwargs)`:

```python
async for item in DELTA_LAB_CLIENT.search_all(
    DELTA_LAB_CLIENT.search_assets_v2, q="ETH", batch=50, max_items=500
):
    ...
```

## Point timeseries + latest

TS methods return a `pd.DataFrame` indexed on `ts` (empty frame on no
data). Latest methods return a **typed dataclass or `None`**.

```python
df = await DELTA_LAB_CLIENT.get_asset_price_ts(
    asset_id=2, lookback_days=7, limit=168  # 7 days × 24 hourly rows
)
pl = await DELTA_LAB_CLIENT.get_asset_price_latest(asset_id=2)
# PriceLatest(asset_id, asof_ts, price_usd, ret_{1,7,30,90}d, vol_{7,30,90}d, mdd_{30,90}d, raw)

# Lending always requires asset_id
df = await DELTA_LAB_CLIENT.get_market_lending_ts(market_id=912, asset_id=2, lookback_days=30)
ll = await DELTA_LAB_CLIENT.get_market_lending_latest(market_id=912, asset_id=2)
# LendingLatest with the full screening feature set on .raw

bl = await DELTA_LAB_CLIENT.get_market_boros_latest(market_id=18900)     # BorosLatest | None
pd_ = await DELTA_LAB_CLIENT.get_market_pendle_latest(market_id=42)      # PendleLatest | None
fl = await DELTA_LAB_CLIENT.get_instrument_funding_latest(instrument_id=100)  # FundingLatest | None
yl = await DELTA_LAB_CLIENT.get_asset_yield_latest(asset_id=2)           # YieldLatest | None
```

**All TS methods accept `lookback_days`, `limit`, `start`, `end`** (ISO
string or `datetime`). Server cap is `limit=10000`.

## Bulk (auto-chunked at 100 ids, 5 parallel in-flight)

Bulk methods **auto-split** inputs >100 items into concurrent sub-calls
and merge. Duplicates in the input list are deduplicated before the
call is made.

```python
# TS — returns dict[int, DataFrame]
price_dfs = await DELTA_LAB_CLIENT.bulk_prices(
    asset_ids=[1, 2, 3], lookback_days=7, limit_per_key=168
)

# Latest — returns dict[int, PriceLatest | None]
latest = await DELTA_LAB_CLIENT.bulk_latest_prices(asset_ids=[1, 2, 3])

# Lending uses (market_id, asset_id) tuples
lending_dfs = await DELTA_LAB_CLIENT.bulk_lending(pairs=[(912, 2), (50, 7)])
lending_latest = await DELTA_LAB_CLIENT.bulk_latest_lending(pairs=[(912, 2)])
# dict[tuple[int, int], LendingLatest | None]
```

Full list: `bulk_prices`, `bulk_yields`, `bulk_funding`, `bulk_pendle`,
`bulk_boros`, `bulk_lending` (TS) + `bulk_latest_*` (latest).

## Orchestration (single-call bundles)

### `explore(symbol, chain_id=None, relations_depth=1)`

One-shot discovery bundle: `{query, asset, matches, relations, markets,
price_latest, yield_latest}`. Default `relations_depth=1` keeps payload
~22 KB on common symbols; depth=2 → 108 KB (warns), depth=3 → 303 KB
(warns). Use for "tell me everything about X" queries.

### `fetch_backtest_bundle(basis_root, side=None, lookback_days=30, limit_per_key=500, instrument_limit=None)`

Returns a typed `BacktestBundle`:

```python
bundle = await DELTA_LAB_CLIENT.fetch_backtest_bundle(
    basis_root="ETH", side="LONG", lookback_days=30, instrument_limit=20
)
bundle.opportunities     # pd.DataFrame — discovery-shape rows
bundle.funding_ts        # dict[int, pd.DataFrame] keyed by instrument_id
bundle.lending_ts        # dict[tuple[int, int], pd.DataFrame]
bundle.start, bundle.end # datetimes
```

Equivalent to fanning out `search_opportunities` + per-instrument
`bulk_funding` + per-(market, asset) `bulk_lending` on the client, but
in one server-side call. **Python scripts only** — typical payload is
50–500 KB. Not for MCP.
