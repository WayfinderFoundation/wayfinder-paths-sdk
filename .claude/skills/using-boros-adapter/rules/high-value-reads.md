# Boros reads (markets + quotes)

## Return type pattern (critical)

**All BorosAdapter async methods return `tuple[bool, result]`** — you must unpack:

```python
# WRONG (will iterate over the tuple, first element is bool):
quotes = await adapter.list_tenor_quotes(underlying_symbol="ETH")
for q in quotes:  # ERROR: first item is True/False!

# RIGHT:
success, quotes = await adapter.list_tenor_quotes(underlying_symbol="ETH")
if not success:
    print("Failed to fetch quotes")
    return
for q in quotes:
    print(q.mid_apr)
```

This pattern applies to **all** adapter methods: `list_markets`, `get_market`, `quote_market`, `get_orderbook`, `get_account_balances`, etc.

## Field naming convention (snake_case)

**All adapter return fields use `snake_case`**, NOT `camelCase`:

```python
# WRONG - camelCase (API raw format):
m.get('marketId')      # ❌ Returns None
m.get('platformName')  # ❌ Returns None

# RIGHT - snake_case (adapter normalized format):
m.get('market_id')         # ✅ e.g., 47
m.get('platform')          # ✅ e.g., "Hyperliquid"
m.get('underlying_symbol') # ✅ e.g., "ETH"
m.get('collateral')        # ✅ e.g., {"token_id": 3, "symbol": "USD₮0", ...}
```

The adapter normalizes API responses to snake_case. Always use snake_case field names.

## Data accuracy (no guessing)

- Do **not** invent APRs/APYs. Always compute/quote using `BorosAdapter.quote_market(...)` / `quote_markets_for_underlying(...)` (or `list_tenor_quotes(...)` for fast `/markets` snapshots).
- If Boros API calls fail, return "unavailable" and include the exact adapter/client call that failed.

## Primary data source

- Adapter: `wayfinder_paths/adapters/boros_adapter/adapter.py`
- Boros is deployed on **Arbitrum (chain_id = 42161)** in this repo’s default configuration.

## High-value reads

### List markets (discovery)

- Call: `success, markets = await adapter.list_markets(is_whitelisted=True, skip=0, limit=100)`
- Preferred (auto-paginates): `success, markets = await adapter.list_markets_all(is_whitelisted=True, page_size=100)`
- Output: `(bool, list[dict])` — market dicts (API-native schema). Use these dicts as the `market` input to `quote_market(...)`.

Notes:
- Boros enforces `limit <= 100`.
- The underlying asset is typically `metadata.assetSymbol` (don’t guess field names).

### Orderbook snapshot

- Call: `success, book = await adapter.get_orderbook(market_id, tick_size=0.001)`
- Output: `(bool, dict)` — orderbook with `long`/`short` sides and tick arrays (schema-flexible).

### Quote a single market (APR summary)

- Call: `success, quote = await adapter.quote_market(market, tick_size=0.001)`
- Shortcut (no need for a market dict): `success, quote = await adapter.quote_market_by_id(market_id, tick_size=0.001)`
- Output: `(bool, BorosMarketQuote)` — dataclass fields:

**Core fields:**
- `market_id`, `market_address`, `symbol`, `underlying`
- `tenor_days`, `maturity_ts`
- `collateral_address`, `collateral_token_id` (NOTE: not `token_id`)
- `tick_step`

**APR fields:**
- `mid_apr`, `best_bid_apr`, `best_ask_apr`
- `mark_apr`, `floating_apr`, `long_yield_apr`
- `funding_7d_ma_apr`, `funding_30d_ma_apr`

**Market data (optional):**
- `volume_24h`, `notional_oi`, `asset_mark_price`
- `next_settlement_time`, `last_traded_apr`, `amm_implied_apr`

Interpretation:
- `mid_apr` is the "implied fixed APR now" for that market (use bid/ask depending on the side you will execute).

### Quote multiple markets for an underlying (tenor curve builder)

- Call: `success, quotes = await adapter.quote_markets_for_underlying(underlying_symbol, ...)`
- Output: `(bool, list[BorosMarketQuote])`
- Recommended params:
  - `platform="hyperliquid"` (optional filter)
  - `prefer_market_data=True` (fast: uses `/markets` embedded bid/ask/mid when available)
  - `page_size=100` (scan all markets; Boros enforces `<=100`)
- Use to build a curve across maturities and pick the best risk-adjusted tenor.

### Fast market+rate snapshot (no orderbooks)

- Call: `success, quotes = await adapter.list_tenor_quotes(underlying_symbol="HYPE", platform="hyperliquid")`
- Output: `(bool, list[BorosTenorQuote])` — dataclass fields:

**Core fields:**
- `market_id`, `address`, `symbol`, `underlying_symbol`
- `maturity` (Unix timestamp, NOT `maturity_date`)
- `tenor_days`

**APR fields:**
- `mid_apr`, `mark_apr`, `floating_apr`, `long_yield_apr`

**Market data (optional):**
- `volume_24h`, `notional_oi`

NOTE: `BorosTenorQuote` does NOT have `collateral_token_id` — use `get_enriched_market()` or `BorosMarketQuote` for collateral info.

### Asset/collateral token info

Get token addresses and metadata for all Boros collateral types:

- `success, assets = await adapter.get_assets()` — all assets with `tokenId`, `address`, `symbol`, `decimals`
- `success, asset = await adapter.get_asset_by_token_id(token_id=3)` — single asset lookup

```python
# Example: get USDT address for deposits
success, asset = await adapter.get_asset_by_token_id(token_id=3)
usdt_address = asset["address"]  # "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9"
```

## Market Discovery Helpers

### List available underlyings

Get unique underlying symbols with market counts and platforms:

- Call: `success, underlyings = await adapter.list_available_underlyings(active_only=True)`
- Output: `(bool, list[dict])` with `symbol`, `markets_count`, `platforms`

```python
# What underlyings are available?
ok, underlyings = await adapter.list_available_underlyings()
# [{"symbol": "ETH", "markets_count": 8, "platforms": ["Hyperliquid", "Binance"]}, ...]
```

### Filter markets by collateral

Get markets that use a specific collateral token:

- Call: `success, markets = await adapter.list_markets_by_collateral(token_id=3, active_only=True)`
- Output: `(bool, list[dict])` — enriched market dicts with collateral info joined

```python
# What markets use USDT collateral?
ok, markets = await adapter.list_markets_by_collateral(token_id=3)
for m in markets:
    # Fields are snake_case: market_id, symbol, underlying_symbol, platform, collateral
    print(f"Market {m['market_id']}: {m['symbol']} ({m['platform']})")
    print(f"  Collateral: {m['collateral']['symbol']} (token_id={m['collateral']['token_id']})")
```

### Get enriched market

Get a single market with all metadata joined:

- Call: `success, market = await adapter.get_enriched_market(market_id=47)`
- Output: `(bool, dict)` — market + collateral + margin type + status

Returns fields:
- `market_id`, `symbol`, `underlying_symbol`, `platform`
- `collateral` — `{token_id, symbol, address, decimals}`
- `is_isolated_only`, `max_leverage`
- `state`, `is_active`, `maturity_ts`, `tenor_days`
- `mid_apr`, `floating_apr`, `mark_apr`

### Historical rate data

Get OHLCV + rate history for a market:

- Call: `success, history = await adapter.get_market_history(market_id=47, time_frame="1h")`
- Output: `(bool, list[dict])` — candles with rate data

Valid `time_frame` values: `5m`, `1h`, `1d`, `1w`

```python
# Get last day of hourly rate history
ok, history = await adapter.get_market_history(market_id=47, time_frame="1h")
for candle in history[-24:]:
    print(f"{candle.get('t')}: mark={candle.get('mr')}, floating={candle.get('ofr')}")
```

## When to Use Which Method

| Task | Recommended Method |
|------|-------------------|
| Filter by underlying (e.g., "ETH markets") | `list_tenor_quotes(underlying_symbol="ETH")` (existing) |
| Filter by platform (e.g., "Hyperliquid") | `list_tenor_quotes(platform="hyperliquid")` (existing) |
| Filter by collateral type | `list_markets_by_collateral(token_id=3)` (new) |
| List unique underlying symbols | `list_available_underlyings()` (new) |
| Get historical rates | `get_market_history(market_id=47)` (new) |
| Get market with all metadata | `get_enriched_market(market_id=47)` (new) |
| Fast tenor curve quotes | `list_tenor_quotes()` or `quote_markets_for_underlying()` (existing) |
| Detailed quote with orderbook | `quote_market()` or `quote_market_by_id()` (existing) |

### Account state reads (MUST check before suggesting trades)

**Always fetch current state before suggesting or executing any Boros trade:**

All return `(bool, result)`:
- `success, positions = await adapter.get_active_positions()` — existing rate positions
- `success, balances = await adapter.get_account_balances(token_id=3)` — collateral summary (isolated/cross/total)
- `success, collaterals = await adapter.get_collaterals()` — full raw collateral data
- `success, orders = await adapter.get_open_limit_orders()` — pending limit orders
- `success, status = await adapter.get_withdrawal_status()` — withdrawal state
- `success, amount = await adapter.get_pending_withdrawal_amount()` — locked withdrawal amount

**Why check first:**
- Avoid suggesting duplicate positions when one already exists
- Avoid depositing when collateral is already sufficient
- Don't trade if withdrawal is pending (funds locked)
