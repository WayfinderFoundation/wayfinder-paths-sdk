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
- `platform` field is a **dict** (`{"name": "Hyperliquid", "platformId": "Hyperliquid", ...}`), NOT a plain string. Use `m.get("platform", {}).get("name", "")` to extract the name.

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

**CRITICAL — `mid_apr` unit: total remaining tenor yield, NOT an annualized APR**

`mid_apr` (and the `mr` field in candles) is the **total fixed yield you earn from now to maturity**, expressed as a fraction of notional. It is NOT an annualized APR.

Example: `mid_apr=0.0377` with 22 days remaining means you earn **3.77% total** on notional over those 22 days.

Delta Lab verifies this via: `APY = (1 + mid_apr)^(365/tenor_days) - 1`
→ `(1.0377)^(365/22) - 1 = 83.8% APY` ✓

**Correct carry formulas (SHORT YU = receive fixed, pay floating):**
```python
from wayfinder_paths.adapters.boros_adapter import parse_market_name_maturity_ts

remaining_days = (maturity_ts - now_unix) / 86400
daily_fixed    = mid_apr / remaining_days        # daily fixed accrual as fraction of notional
daily_floating = floating_apr / 365.0            # floating_apr IS annualized
daily_carry    = daily_fixed - daily_floating
annualized_carry = daily_carry * 365             # net annualized carry

# LONG YU = opposite signs
annualized_carry_long = (daily_floating - daily_fixed) * 365
```

**Common mistake:** `carry = mid_apr - floating_apr` is wrong — it mixes units (total-tenor yield vs annualized rate) and underestimates carry by 10–20×.

`floating_apr` IS annualized; only `mid_apr` / `mr` is the total-tenor yield.

`long_yield_apr` includes leverage/margin effects and should not be used directly in carry calculations — compute it from first principles as shown above.

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

### Vault discovery

Use the vault helpers when the task is "where should I deposit collateral?" rather than "what fixed rate can I trade?"

- `success, vaults = await adapter.get_vaults_summary(account="0x...", include_expired=False)`
  - Output: `(bool, list[BorosVault])`
  - High-value fields: `amm_id`, `market_id`, `symbol`, `collateral_symbol`, `apy`, `expiry`, `tenor_days`, `tvl`, `tvl_usd`, `available_tokens`, `available_usd`, `user_deposit_tokens`, `user_deposit_usd`, `is_isolated_only`, `market_state`, `is_expired`
  - When `account` is provided, the adapter also attaches user LP balance / estimated deposited value
  - `tvl` / `available_tokens` / `user_deposit_tokens` are normalized to collateral token units, not raw `1e18` Boros values
  - `*_usd` fields are derived from the collateral asset price joined from `get_assets()`
- `include_expired=False` is the right default for UI/status views; leave it as `True` when you need to inspect or roll existing expired positions
- `success, vaults = await adapter.search_vaults(token_id=3, asset="ETH", account="0x...")`
  - Filter by collateral token (`token_id`) and/or symbol
- `success, best = await adapter.best_yield_vault(token_id=3, amount_tokens=1000.0, min_tenor_days=7.0, allow_isolated_only=True)`
  - Picks the highest-APY vault that is currently depositable and has enough capacity
- `adapter.is_vault_open_for_deposit(vault, min_tenor_days=7.0, allow_isolated_only=True)`
  - Use this instead of guessing from `is_active` / `state`

Important notes:
- `token_id` here is the **collateral token**, not the underlying perp symbol.
- Isolated-only vaults are intentionally excluded unless `allow_isolated_only=True`.
- "Open" means more than `is_active=true`: the helper also rejects expired, paused, below-tenor, and disallowed isolated-only vaults.
- Expired vaults are identified by absence from the active market metadata set; they can still appear in summaries when `include_expired=True` so rollover/withdraw flows can manage them.
- If you surface Boros vault values inside a strategy status view, keep marked USD reporting under a dedicated field (for example `boros_vault_reported_value_usd`) rather than silently changing the meaning of an existing sizing field.

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

### Get all markets

Get the repo-convention Boros market list with current rates, optional vault summary, and compact historical context:

- Call: `success, markets = await adapter.get_all_markets()`
- Output: `(bool, list[dict])`

Per-market fields:
- Top level: `market_id`, `market_address`, `symbol`, `underlying_symbol`, `platform`, `collateral`, `state`, `is_active`, `maturity_ts`, `tenor_days`, `is_isolated_only`, `max_leverage`
- `rates`: current market rates and activity, including `floating_apr`, `mark_apr`, `vault_apy`, `mid_apr`, `best_bid_apr`, `best_ask_apr`, `funding_7d_ma_apr`, `funding_30d_ma_apr`, `volume_24h`, `notional_oi`
- `vault`: AMM summary for that market, including `apy`, `expiry`, `collateral_symbol`, `tvl`, `tvl_usd`, `available_tokens`, `available_usd`
- `history`: compact history summary, including `latest_mark_rate`, `avg_mark_rate`, `latest_floating_rate`, `avg_floating_rate`, and latest funding moving averages

Useful flags:
- `active_only=True` — only keep currently active markets
- `account="0x..."` — include `vault.user` with deposited/available balances for that account
- `include_vault_summary=False` — market + rate view only
- `include_history_summary=False` — skip per-market history summary

Account-specific expired vault note:
- When `account` is provided, `get_all_markets()` also appends inactive vault-only rows for expired markets that have rolled off `/markets` but still have real user LP/deposit exposure.
- Those rows may use fallback labels like `BOROS-MARKET-34` when Boros no longer serves market metadata for that expired market ID.
- Rows are not appended for `availableBalanceToDeposit` alone, because that would duplicate idle collateral across many expired vault IDs.

Important note:
- `rates.mark_apr` is the current live market APR from `/markets`
- `history.latest_mark_rate` / `avg_mark_rate` come from candle `mr` values and should be treated as historical rate snapshots, not a raw candle dump

### Historical rate data

Get OHLCV + rate history for a market:

- Call: `success, history = await adapter.get_market_history(market_id=47, time_frame="1h")`
- Output: `(bool, list[dict])` — candles with rate data

Valid `time_frame` values: `5m`, `1h`, `1d`, `1w`

```python
# Get last day of hourly rate history
ok, history = await adapter.get_market_history(market_id=47, time_frame="1h")
for candle in history[-24:]:
    print(f"{candle.get('ts') or candle.get('t')}: mark={candle.get('mr')}, floating={candle.get('ofr')}")
```

Candle fields: `ts` (Unix timestamp; some older mocks may use `t`), `mr` (total remaining fixed yield — see CRITICAL note above), `ofr` (annualized floating rate — daily CLOSE, can be noisy), `b7dmafr` / `b30dmafr` (7/30-day MA funding), `u` (instantaneous oracle rate update).

**Getting maturity for historical/expired markets:**

Active markets expose `maturity_ts` via `quote_market_by_id`, but expired markets return 404. Instead, parse the market name directly:

```python
from wayfinder_paths.adapters.boros_adapter import parse_market_name_maturity_ts

# Market names encode maturity as YYMMDD suffix: BTCUSDT-BN-T-260327 → 2026-03-27
maturity_ts = parse_market_name_maturity_ts("BTCUSDT-BN-T-260327")  # → 1774569600

# In a backtest loop:
remaining_days = (maturity_ts - candle_ts_unix) / 86400
daily_carry = candle["mr"] / remaining_days - candle["ofr"] / 365
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
