# Response Structures

Detailed breakdown of Delta Lab response types.

## ⚠️ APY Value Format (CRITICAL)

**All APY/rate values in Delta Lab responses are decimal floats, NOT percentages:**

- `apy.value = 0.98` means **98% APY** (not 0.98%)
- `apy.value = 2.40` means **240% APY** (not 2.40%)
- `net_apy = 0.05` means **5% net APY** (not 0.05%)
- `funding_rate = 0.0001` means **0.01% per 8h** (not 0.0001%)

**To display as percentage:** Multiply by 100 (e.g., `apy['value'] * 100` = `98%`)

**Applies to all fields:** `apy.value`, `net_apy`, `funding_rate`, `implied_apy`, `underlying_apy`, `reward_apr`, `fixed_rate_mark`, etc.

## Opportunity Object

The core data structure representing a yield opportunity.

```python
{
    "instrument_id": 123,
    "instrument_type": "PERP",  # PERP, LENDING_SUPPLY, LENDING_BORROW, BOROS_MARKET, PENDLE_PT, YIELD_TOKEN
    "side": "LONG",  # LONG or SHORT (position direction; semantics depend on instrument_type)
    "venue": "hyperliquid",  # Protocol/venue name
    "market_id": 456,
    "market_external_id": "BTC-USD",
    "market_type": "PERP",
    "chain_id": 999,  # For on-chain opportunities
    "maturity_ts": "2024-12-31T00:00:00Z",  # For fixed-term instruments

    # Asset references
    "deposit_asset": {"asset_id": 3, "symbol": "USDT"},
    "receipt_asset": {"asset_id": 101, "symbol": "mUSDT"},
    "exposure_asset": {"asset_id": 1, "symbol": "BTC"},

    # Internal asset IDs
    "opportunity": {
        "deposit_asset_id": 3,
        "receipt_asset_id": 101,
        "exposure_asset_id": 1,
        "basis_asset_id": 1
    },

    # Instrument metadata
    "instrument": {
        "quote_asset_id": 3,
        "base_asset_id": 1,
        "extra": {}  # Protocol-specific metadata
    },

    # APY information
    "apy": {
        "value": 0.12,  # DECIMAL format: 0.12 = 12% APY (not 0.12%); can be null
        "components": {...},  # See APY Components below
        "as_of": "2024-02-12T12:00:00Z",
        "lookback_days": 7
    },

    # Risk metrics
    "risk": {
        "vol_annualized": 0.5,
        "erisk_proxy": 0.05,
        "tvl_usd": 1000000,
        "size_usd": 2000000,
        "liquidity_usd": 500000,
    },
    "quality_ok": 1,
    "market_label": "BTC-USD"
}
```

### Key Fields Explained

#### `instrument_type`

Delta Lab uses uppercase enums (exact strings in the API response):

- `PERP` - Perpetual futures (funding rate opportunities)
- `LENDING_SUPPLY` - Supply-side lending (you receive yield)
- `LENDING_BORROW` - Borrow-side lending (you pay a cost; APY is often negative)
- `BOROS_MARKET` - Boros fixed-rate markets (`fixed_rate_mark`)
- `PENDLE_PT` - Pendle PT markets (`implied_apy`, `underlying_apy`)
- `YIELD_TOKEN` - Yield-bearing token yields (`underlying_apy`, `reward_apr`)

#### `side`

- `LONG` - Take the long side of the instrument (e.g. supply/lend, long perp, receive fixed-rate)
- `SHORT` - Take the short side of the instrument (e.g. borrow, short perp, pay fixed-rate)

For most instruments, `apy.value` is already signed as the *net yield* for that position:
- Positive `apy.value` → the position receives yield
- Negative `apy.value` → the position pays yield (cost)

#### Asset References

- `deposit_asset` - What you deposit to enter the position
- `receipt_asset` - What you receive (e.g., mToken for Moonwell)
- `exposure_asset` - What price risk you have (e.g., BTC for BTC perp)

#### `maturity_ts`

- `null` for perpetual/open-ended positions
- ISO timestamp for fixed-term instruments (PT, fixed-rate markets)

## APY Components

Detailed breakdown of APY sources (all fields are optional).

```python
"components": {
    # PERP (funding) (ALL VALUES ARE DECIMALS: 0.01 = 1%)
    "funding_rate_hourly_avg": 0.00001,  # 0.001% per hour
    "funding_apy_est": 0.0876,  # 8.76% annualized
    "funding_rate_hourly_latest": 0.000012,  # 0.0012% per hour
    "mark_price_usd": 45000,  # Current mark price
    "oi_usd": 1000000000,  # Open interest in USD
    "volume_usd": 500000000,  # 24h volume in USD

    # PENDLE_PT
    "implied_apy": 0.12,  # 12% implied APY
    "underlying_apy": 0.04,  # 4% underlying/reference APY

    # BOROS_MARKET (fixed-rate)
    "fixed_rate_mark": 0.10,  # 10% fixed rate quote

    # Lending markets
    "supply_apr": 0.05,  # 5% APR
    "supply_reward_apr": 0.02,  # 2% APR from rewards
    "protocol_supply_apr": 0.07,  # supply_apr + supply_reward_apr
    "borrow_apr": 0.08,  # 8% APR cost
    "borrow_reward_apr": 0.01,  # 1% APR rewards offset
    "protocol_borrow_apr": 0.07,  # borrow_apr - borrow_reward_apr
    "base_yield_apy": 0.03,  # deposit token intrinsic yield (e.g., wstETH)

    # YIELD_TOKEN
    "reward_apr": 0.03,  # additional rewards
    "apy_base": 0.06,  # base yield APY
    "apy_base_7d": 0.055,  # 7-day mean base yield

    # NOTE: Each opportunity populates only a subset of component fields.
}
```

### Component Interpretation

For **perp funding (`PERP`)**:
- Hyperliquid: positive funding = longs pay shorts
- For `side="SHORT"` (short perp hedge), positive `funding_apy_est` is yield; negative is a cost
- For `side="LONG"` (long perp), the interpretation flips

For **lending (`LENDING_SUPPLY` / `LENDING_BORROW`)**:
- Supply (`LENDING_SUPPLY`) → `protocol_supply_apr = supply_apr + supply_reward_apr`
- Borrow (`LENDING_BORROW`) → `protocol_borrow_apr = borrow_apr - borrow_reward_apr`
- If `base_yield_apy` (or `underlying_apy`) exists, `apy.value` reflects compounding:
  `(1 + protocol_rate) * (1 + underlying_yield) - 1`

For **Pendle PT (`PENDLE_PT`)**:
- `implied_apy` is the rate you lock in (best-effort)

For **Boros (`BOROS_MARKET`)**:
- `fixed_rate_mark` is the fixed rate quote

## Risk Metrics

```python
"risk": {
    "vol_annualized": 0.5,  # Annualized volatility (0.5 = 50%)
    "erisk_proxy": 0.05,  # Estimated risk proxy (lower is better)
    "tvl_usd": 1000000,  # Total value locked in protocol/market
    "size_usd": 2000000,  # Capacity proxy (used as secondary sort / quality checks)
    "liquidity_usd": 500000,  # Available liquidity
}
```

### Risk Interpretation

- `erisk_proxy` - Lower is better. Combines vol, liquidity, and other factors
- `tvl_usd` - Higher TVL generally means more established/safe (when populated)
- `size_usd` - Per-instrument capacity proxy used in quality filters/sorting
- `liquidity_usd` - Higher liquidity means easier entry/exit (when populated)

## Delta-Neutral Candidate

A matched carry/hedge pair.

```python
{
    "basis_root_symbol": "BTC",
    "exposure_asset": {"asset_id": 1, "symbol": "BTC"},

    "carry_leg": {
        # Full Opportunity object (carry leg, typically side="LONG")
        "instrument_type": "LENDING_SUPPLY",
        "side": "LONG",
        "venue": "moonwell",
        "apy": {"value": 0.08, ...},  # 8% APY (decimal format)
        ...
    },

    "hedge_leg": {
        # Full Opportunity object (hedge leg, typically a short perp)
        "instrument_type": "PERP",
        "side": "SHORT",
        "venue": "hyperliquid",
        "apy": {"value": -0.03, ...},  # -3% APY cost (funding you pay)
        ...
    },

    "net_apy": 0.05,  # 5% net yield (0.08 + (-0.03) = 0.05 = 5%)
    "erisk_proxy": 0.05  # Combined risk metric
}
```

### Net APY Calculation

```
net_apy = carry_leg.apy.value + hedge_leg.apy.value
```

Note: The hedge_leg APY is already signed correctly:
- If you're paying to hedge (e.g., paying funding), it's negative
- Net APY is the combined return after hedging costs

### Example Pairs

**Supply + short perp (classic carry + hedge):**
```python
{
    "carry_leg": {
        "instrument_type": "LENDING_SUPPLY",
        "side": "LONG",
        "venue": "moonwell",
        "apy": {"value": 0.08}  # 8% supply APY
    },
    "hedge_leg": {
        "instrument_type": "PERP",
        "side": "SHORT",
        "venue": "hyperliquid",
        "apy": {"value": -0.03}  # -3% funding cost for shorts
    },
    "net_apy": 0.05  # 5% net
}
```

**Fixed-rate receive + short perp:**
```python
{
    "carry_leg": {
        "instrument_type": "BOROS_MARKET",
        "side": "LONG",
        "venue": "boros",
        "apy": {"value": 0.10}  # 10% fixed rate
    },
    "hedge_leg": {
        "instrument_type": "PERP",
        "side": "SHORT",
        "venue": "hyperliquid",
        "apy": {"value": 0.12}  # 12% funding received by shorts
    },
    "net_apy": 0.22  # 22% combined (10% + 12%)
}
```

## BasisInfo

Basis symbol resolution information.

```python
{
    "input_symbol": "btc",  # What you queried with (case-insensitive)
    "root_symbol": "BTC",  # Canonical symbol
    "root_asset_id": 1,  # Primary asset ID for this basis
    "basis_group_id": 42,  # Internal grouping ID
    "basis_asset_ids": [1, 123, 456]  # All asset IDs in this basis group
}
```

This tells you:
- The canonical symbol for the basis you queried
- All related asset IDs that are considered part of this basis
- The basis_group_id for internal reference

## Summary

High-level statistics about the results.

```python
{
    "instrument_type_counts": {
        "PERP": 15,
        "LENDING_SUPPLY": 8,
        "LENDING_BORROW": 4,
        "BOROS_MARKET": 3,
        "PENDLE_PT": 2,
        "YIELD_TOKEN": 1
    }
}
```

Useful for quick understanding of what types of opportunities are available.

## AssetsByAddressResponse

Response from `get_assets_by_address()` containing all assets matching a contract address.

```python
{
    "assets": [
        {
            "asset_id": 123,
            "symbol": "WETH",
            "name": "Wrapped Ether",
            "decimals": 18,
            "chain_id": 1,
            "address": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            "coingecko_id": "weth"
        },
        {
            "asset_id": 456,
            "symbol": "WETH",
            "name": "Wrapped Ether",
            "decimals": 18,
            "chain_id": 8453,  # Same address, different chain
            "address": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            "coingecko_id": "weth"
        }
    ]
}
```

### Use Cases

- Find all versions of a wrapped/bridged asset across chains
- Resolve asset IDs from known contract addresses
- Discover which chains support a specific token

## AssetBasisResponse

Response from `get_asset_basis()` showing basis group membership.

```python
{
    "asset_id": 1,
    "symbol": "ETH",
    "basis": {
        "basis_group_id": 1,
        "root_asset_id": 1,
        "root_symbol": "ETH",
        "role": "ROOT"  # ROOT, WRAPPED, YIELD_BEARING, or COLLATERAL
    }
}
```

If the asset is not in a basis group, `"basis"` will be `None`:

```python
{
    "asset_id": 999,
    "symbol": "OBSCURE_TOKEN",
    "basis": None
}
```

### Role Types

- `"ROOT"` - The canonical/primary asset (e.g., ETH)
- `"WRAPPED"` - Wrapped version (e.g., WETH)
- `"YIELD_BEARING"` - Yield-bearing derivative (e.g., stETH, rETH)
- `"COLLATERAL"` - Used as collateral in lending (e.g., aETH)

### Use Cases

- Check if two assets are fungible (same basis group)
- Understand asset relationships (wrapped vs native)
- Verify asset eligibility for basis trading

## AssetTimeseriesResponse (DataFrame)

**Note:** The client returns `dict[str, pd.DataFrame]`, not raw JSON.

### Response Structure

```python
{
    "price": DataFrame(
        columns=["price_usd"],
        index=DatetimeIndex  # ts as index
    ),
    "yield": DataFrame(
        columns=[
            "yield_token_asset_id",
            "yield_token_symbol",
            "apy_base",
            "apy_base_7d",
            "exchange_rate",
            "tvl_usd"
        ],
        index=DatetimeIndex
    ),
    "lending": DataFrame(
        columns=[
            "market_id",
            "asset_symbol",
            "chain_id",
            "venue",
            "supply_apr",
            "borrow_apr",
            "supply_reward_apr",
            "borrow_reward_apr",
            "net_supply_apy",
            "net_borrow_apy",
            "avg_supply_apy",
            "avg_borrow_apy",
            "utilization",
            "supply_tvl_usd",
            "borrow_tvl_usd",
            "collateral_tvl_usd",
            "fee",
            "rewards_estimated",
            "base_yield_apy",
            "underlying_apy",
            "combined_supply_apy",
        ],
        index=DatetimeIndex
    ),
    "funding": DataFrame(
        columns=[
            "instrument_id",
            "venue",
            "market_external_id",
            "funding_rate",
            "mark_price_usd",
            "oi_usd",
            "volume_usd"
        ],
        index=DatetimeIndex
    ),
    "pendle": DataFrame(
        columns=[
            "market_id",
            "chain_id",
            "venue",
            "pt_symbol",
            "maturity_ts",
            "implied_apy",
            "underlying_apy",
            "reward_apr",
            "pt_price",
            "tvl_usd"
        ],
        index=DatetimeIndex
    ),
    "boros": DataFrame(
        columns=[
            "market_id",
            "chain_id",
            "venue",
            "market_external_id",
            "fixed_rate_mark",
            "floating_rate_oracle",
            "pv"
        ],
        index=DatetimeIndex
    )
}
```

### Series Descriptions

#### Price Series
- `price_usd` - USD price at each timestamp
- Useful for correlation analysis, plotting price charts

#### Yield Series
- Yield-bearing tokens (stETH, rETH, etc.)
- `apy_base` - Base APY from protocol
- `apy_base_7d` - 7-day average APY
- `exchange_rate` - Token exchange rate to underlying
- `tvl_usd` - Total value locked

#### Lending Series
- Multiple markets per timestamp (different venues/chains)
- `supply_apr` / `borrow_apr` - Base rates
- `supply_reward_apr` / `borrow_reward_apr` - Additional rewards
- `net_supply_apy` / `net_borrow_apy` - Net rates (when populated)
- `utilization` - Market utilization ratio
- `supply_tvl_usd` / `borrow_tvl_usd` - Market sizes
- `base_yield_apy` / `underlying_apy` - Token intrinsic yield (if the lending asset is yield-bearing)
- `combined_supply_apy` - Compounded supply yield when intrinsic yield exists

#### Funding Series
- Perpetual funding rates over time
- `funding_rate` - Hourly funding rate (positive = longs pay shorts)
- `mark_price_usd` - Mark price
- `oi_usd` / `volume_usd` - Open interest and volume

#### Pendle Series
- PT/YT markets over time
- `implied_apy` - Implied APY from PT price
- `underlying_apy` - Underlying protocol APY
- `maturity_ts` - Maturity timestamp

#### Boros Series
- Fixed-rate markets over time
- `fixed_rate_mark` - Fixed rate quote
- `floating_rate_oracle` - Floating rate reference
- `pv` - Present value

### Working with DataFrames

```python
# Get timeseries data
data = await DELTA_LAB_CLIENT.get_asset_timeseries("ETH", series="price,funding")

# Access series
price_df = data["price"]
funding_df = data["funding"]

# Plot price
price_df["price_usd"].plot(title="ETH Price")

# Calculate funding rate statistics
funding_df.groupby("venue")["funding_rate"].describe()

# Filter by venue
hl_funding = funding_df[funding_df["venue"] == "hyperliquid"]

# Resample to daily average
daily_avg = funding_df.resample("1D")["funding_rate"].mean()
```

## Screening Responses

All screening endpoints return `ScreenResponse`:

```python
{
    "data": [...],  # List of feature rows
    "count": 20     # Number of rows returned
}
```

### ScreenPriceRow

```python
{
    "asof_ts": "2025-02-27T12:00:00Z",
    "asset_id": 1,
    "symbol": "BTC",
    "price_usd": 95000.0,
    "ret_1d": 0.02,        # 1-day return (decimal: 0.02 = 2%)
    "ret_7d": 0.05,
    "ret_30d": 0.15,
    "ret_90d": 0.40,
    "vol_7d": 0.35,         # Annualized volatility (decimal)
    "vol_30d": 0.45,
    "vol_90d": 0.50,
    "mdd_30d": -0.12,       # Max drawdown (negative: -0.12 = -12%)
    "mdd_90d": -0.20
}
```

### ScreenLendingRow

```python
{
    "asof_ts": "2025-02-27T12:00:00Z",
    "market_id": 42,
    "market_type": "MORPHO",
    "chain_id": 8453,
    "market_external_id": "0x...",
    "market_label": "WETH/USDC (80% LLTV)",
    "asset_id": 3,
    "symbol": "USDC",
    "venue_id": 7,
    "venue_name": "morpho",
    "is_collateral_enabled": True,
    "is_frozen": False,
    "is_paused": False,
    "base_yield_apy": 0.03,
    "underlying_apy": 0.03,
    "net_supply_apr_now": 0.045,       # Current net supply APR (decimal)
    "net_supply_mean_7d": 0.042,
    "net_supply_mean_30d": 0.038,
    "net_supply_std_30d": 0.005,
    "net_supply_z_30d": 1.4,           # Z-score vs 30d mean
    "combined_net_supply_apr_now": 0.06, # Including reward APR
    "combined_supply_mean_7d": 0.055,
    "combined_supply_mean_30d": 0.050,
    "combined_supply_std_30d": 0.008,
    "combined_supply_z_30d": 1.25,
    "net_borrow_apr_now": 0.065,
    "net_borrow_mean_7d": 0.060,
    "net_borrow_mean_30d": 0.058,
    "net_borrow_std_30d": 0.004,
    "net_borrow_z_30d": 1.75,
    "util_now": 0.82,                  # Utilization ratio
    "util_mean_30d": 0.78,
    "util_z_30d": 0.8,
    "liquidity_usd": 50000000.0,
    "supply_tvl_usd": 200000000.0,
    "borrow_tvl_usd": 164000000.0,
    "ltv_max": 0.80,
    "liq_threshold": 0.825,
    "liquidation_penalty": 0.05,
    "borrow_spike_score": 2.1          # Anomaly detection score
}
```

### ScreenPerpRow

```python
{
    "asof_ts": "2025-02-27T12:00:00Z",
    "instrument_id": 10,
    "market_id": 15,
    "venue_id": 3,
    "venue_name": "hyperliquid",
    "base_asset_id": 1,
    "base_symbol": "BTC",
    "quote_asset_id": 2,
    "quote_symbol": "USDT",
    "mark_price": 95000.0,
    "index_price": 94980.0,
    "basis_now": 0.0002,               # Current basis (decimal)
    "funding_now": 0.00005,            # Current funding rate
    "funding_mean_7d": 0.00004,
    "funding_std_7d": 0.00002,
    "funding_mean_30d": 0.00003,
    "funding_std_30d": 0.000015,
    "funding_mean_90d": 0.000035,
    "funding_std_90d": 0.00002,
    "funding_z_30d": 1.0,              # Z-score vs 30d mean
    "funding_z_90d": 0.75,
    "funding_pos_pct_30d": 0.85,       # % of time positive in 30d
    "funding_neg_pct_30d": 0.15,
    "basis_mean_7d": 0.00018,
    "basis_mean_30d": 0.00015,
    "basis_std_30d": 0.0001,
    "basis_z_30d": 0.5,
    "oi_now": 500000000.0,             # Open interest (USD)
    "oi_mean_7d": 480000000.0,
    "oi_change_vs_7d_mean": 0.04,      # OI change vs 7d mean (decimal)
    "volume_24h": 2000000000.0
}
```

### ScreenBorrowRouteRow

From `DELTA_LAB_CLIENT.screen_borrow_routes(...)` or MCP:
- `wayfinder://delta-lab/screen/borrow-routes/{sort}/{limit}/{basis}/{borrow_basis}`
- `wayfinder://delta-lab/screen/borrow-routes/{sort}/{limit}/{basis}/{borrow_basis}/{chain_id}`

```python
{
    "route_id": 1,
    "market_id": 123,
    "market_type": "MORPHO",
    "chain_id": 8453,
    "market_external_id": "0x...",
    "market_label": "WETH/USDC (80% LLTV)",
    "venue_id": 7,
    "venue_name": "morpho",
    "collateral_asset_id": 2,
    "collateral_symbol": "WETH",
    "borrow_asset_id": 3,
    "borrow_symbol": "USDC",
    "topology": "POOLED",
    "mode_type": "BASE",
    "mode_label": None,
    "ltv_max": 0.8,
    "liq_threshold": 0.85,
    "liquidation_penalty": 0.05,
    "debt_ceiling_usd": 1000000,
    "collateral_earns_pool_supply": True,
    "extra": {},
    "created_at": "2026-03-04T12:34:56+00:00"
}
```

Notes:
- `ltv_max`, `liq_threshold`, and `liquidation_penalty` are fractions (0.8 = 80%).
- Some fields can be `null` depending on venue/market metadata.

## AssetResponse

From `DELTA_LAB_CLIENT.get_asset(...)` or MCP:
- `wayfinder://delta-lab/assets/{asset_id}`

```python
{
    "asset_id": 1,
    "symbol": "USDC",
    "name": "USD Coin",
    "decimals": 6,
    "chain_id": 8453,
    "address": "0x...",
    "coingecko_id": "usd-coin"
}
```

## AssetSearchResponse

From MCP:
- `wayfinder://delta-lab/assets/search/{query}`
- `wayfinder://delta-lab/assets/search/{chain}/{query}`
- `wayfinder://delta-lab/assets/by-address/{address}/{chain_id}`

```python
{
    "assets": [AssetResponse, ...],
    "total_count": 3
}
```
