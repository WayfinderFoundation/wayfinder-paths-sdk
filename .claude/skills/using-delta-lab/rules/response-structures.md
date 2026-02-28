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
    "instrument_type": "perp",  # perp, spot, lending, fixed_rate, pt, yt
    "side": "LONG",  # LONG (receive yield) or SHORT (pay yield)
    "venue": "hyperliquid",  # Protocol/venue name
    "market_id": 456,
    "market_external_id": "BTC-USD",
    "market_type": "perpetual",
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
        "liquidity_usd": 500000,
        "duration_risk": 0.1,
        "liquidity_penalty": 0.02
    }
}
```

### Key Fields Explained

#### `instrument_type`

- `perp` - Perpetual futures (funding rate opportunities)
- `spot` - Spot holdings (no yield unless staked/lent)
- `lending` - Lending/borrowing positions
- `fixed_rate` - Fixed-rate markets (Boros)
- `pt` - Principal tokens (Pendle)
- `yt` - Yield tokens (Pendle)

#### `side`

- `LONG` - You receive yield (lending, short perp in positive funding, holding PT)
- `SHORT` - You pay yield (borrowing, long perp in positive funding, holding YT)

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
    # Perpetual funding (ALL VALUES ARE DECIMALS: 0.01 = 1%)
    "funding_rate_hourly_avg": 0.00001,  # 0.001% per hour
    "funding_apy_est": 0.0876,  # 8.76% annualized
    "funding_rate_hourly_latest": 0.000012,  # 0.0012% per hour
    "mark_price_usd": 45000,  # Current mark price
    "oi_usd": 1000000000,  # Open interest in USD
    "volume_usd": 500000000,  # 24h volume in USD

    # Fixed-rate markets (Boros)
    "implied_apy": 0.12,  # 12% APY

    # Lending markets
    "supply_apr": 0.05,  # 5% APR
    "supply_reward_apr": 0.02,  # 2% APR from rewards
    "borrow_apr": 0.08,  # 8% APR cost
    "borrow_reward_apr": 0.01,  # 1% APR rewards offset

    # Pendle PT/YT
    "underlying_apy": 0.04,  # 4% underlying yield
    "reward_apr": 0.03,  # 3% additional rewards

    # Pool APY
    "apy_base": 0.06,  # 6% base APY
    "apy_base_7d": 0.055,  # 5.5% 7-day average

    # Fixed-rate
    "fixed_rate_mark": 0.10  # 10% fixed rate
}
```

### Component Interpretation

For **perp funding**:
- Positive `funding_apy_est` = longs pay shorts
- Negative `funding_apy_est` = shorts pay longs
- Check `side` to determine if you receive or pay

For **lending**:
- LONG side → `supply_apr` + `supply_reward_apr`
- SHORT side → `borrow_apr` - `borrow_reward_apr` (you pay this)

For **fixed-rate**:
- `implied_apy` is what you lock in
- Compare to `funding_apy_est` (floating) for arbitrage

## Risk Metrics

```python
"risk": {
    "vol_annualized": 0.5,  # Annualized volatility (0.5 = 50%)
    "erisk_proxy": 0.05,  # Estimated risk proxy (lower is better)
    "tvl_usd": 1000000,  # Total value locked in protocol/market
    "liquidity_usd": 500000,  # Available liquidity
    "duration_risk": 0.1,  # Duration risk (for fixed-term)
    "liquidity_penalty": 0.02  # Penalty for low liquidity
}
```

### Risk Interpretation

- `erisk_proxy` - Lower is better. Combines vol, liquidity, and other factors
- `tvl_usd` - Higher TVL generally means more established/safe
- `liquidity_usd` - Higher liquidity means easier entry/exit
- `duration_risk` - Relevant for fixed-term instruments (time to maturity)
- `liquidity_penalty` - Adjustment to APY for liquidity risk

## Delta-Neutral Candidate

A matched carry/hedge pair.

```python
{
    "basis_root_symbol": "BTC",
    "exposure_asset": {"asset_id": 1, "symbol": "BTC"},

    "carry_leg": {
        # Full Opportunity object (LONG side - you receive yield)
        "instrument_type": "lending",
        "side": "LONG",
        "venue": "moonwell",
        "apy": {"value": 0.08, ...},  # 8% APY (decimal format)
        ...
    },

    "hedge_leg": {
        # Full Opportunity object (SHORT side - hedges exposure)
        "instrument_type": "perp",
        "side": "SHORT",
        "venue": "hyperliquid",
        "apy": {"value": -0.03, ...},  # -3% APY cost (funding you pay)
        ...
    },

    "net_apy": 0.05,  # 5% net yield (0.08 - 0.03 = 0.05 = 5%)
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

**Long spot + short perp (cash-and-carry):**
```python
{
    "carry_leg": {
        "instrument_type": "spot",
        "side": "LONG",
        "apy": {"value": 0.0}  # 0% - no yield from holding spot
    },
    "hedge_leg": {
        "instrument_type": "perp",
        "side": "SHORT",
        "apy": {"value": 0.12}  # 12% - receiving positive funding as short
    },
    "net_apy": 0.12  # 12% net from funding received
}
```

**Lend + short perp:**
```python
{
    "carry_leg": {
        "instrument_type": "lending",
        "side": "LONG",
        "venue": "moonwell",
        "apy": {"value": 0.08}  # 8% lending APY
    },
    "hedge_leg": {
        "instrument_type": "perp",
        "side": "SHORT",
        "venue": "hyperliquid",
        "apy": {"value": 0.12}  # 12% funding received
    },
    "net_apy": 0.20  # 20% combined (8% + 12%)
}
```

**Lend + long fixed rate (lock in yield):**
```python
{
    "carry_leg": {
        "instrument_type": "lending",
        "side": "LONG",
        "venue": "moonwell",
        "apy": {"value": 0.08}  # 8% floating lending rate
    },
    "hedge_leg": {
        "instrument_type": "fixed_rate",
        "side": "LONG",
        "venue": "boros",
        "apy": {"value": -0.05}  # -5% fixed rate cost to lock in
    },
    "net_apy": 0.03  # 3% locked-in spread (8% - 5%)
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
        "perp": 15,
        "lending": 8,
        "fixed_rate": 3,
        "pt": 2,
        "yt": 1
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
            "chain_id",
            "venue",
            "supply_apr",
            "borrow_apr",
            "supply_reward_apr",
            "borrow_reward_apr",
            "utilization",
            "supply_tvl_usd",
            "borrow_tvl_usd"
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
- `utilization` - Market utilization ratio
- `supply_tvl_usd` / `borrow_tvl_usd` - Market sizes

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
