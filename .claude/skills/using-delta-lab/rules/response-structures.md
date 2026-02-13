# Response Structures

Detailed breakdown of Delta Lab response types.

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
        "value": 0.12,  # 12% APY (can be null)
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
    # Perpetual funding
    "funding_rate_hourly_avg": 0.00001,  # Average hourly funding rate
    "funding_apy_est": 0.0876,  # Annualized funding rate estimate
    "funding_rate_hourly_latest": 0.000012,  # Latest hourly funding rate
    "mark_price_usd": 45000,  # Current mark price
    "oi_usd": 1000000000,  # Open interest in USD
    "volume_usd": 500000000,  # 24h volume in USD

    # Fixed-rate markets (Boros)
    "implied_apy": 0.12,  # Implied APY from orderbook

    # Lending markets
    "supply_apr": 0.05,  # Supply APR
    "supply_reward_apr": 0.02,  # Additional rewards for supplying
    "borrow_apr": 0.08,  # Borrow APR
    "borrow_reward_apr": 0.01,  # Additional rewards for borrowing

    # Pendle PT/YT
    "underlying_apy": 0.04,  # Underlying yield rate
    "reward_apr": 0.03,  # Additional rewards

    # Pool APY
    "apy_base": 0.06,  # Base APY from fees
    "apy_base_7d": 0.055,  # 7-day average base APY

    # Fixed-rate
    "fixed_rate_mark": 0.10  # Fixed rate mark (Boros)
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
        "apy": {"value": 0.08, ...},
        ...
    },

    "hedge_leg": {
        # Full Opportunity object (SHORT side - hedges exposure)
        "instrument_type": "perp",
        "side": "SHORT",
        "venue": "hyperliquid",
        "apy": {"value": -0.03, ...},  # Cost of hedge (funding you pay)
        ...
    },

    "net_apy": 0.05,  # 8% - 3% = 5% net yield
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
        "apy": {"value": 0.0}  # No yield from holding spot
    },
    "hedge_leg": {
        "instrument_type": "perp",
        "side": "SHORT",
        "apy": {"value": 0.12}  # Receiving positive funding as short
    },
    "net_apy": 0.12  # Funding received
}
```

**Lend + short perp:**
```python
{
    "carry_leg": {
        "instrument_type": "lending",
        "side": "LONG",
        "venue": "moonwell",
        "apy": {"value": 0.08}  # Lending APY
    },
    "hedge_leg": {
        "instrument_type": "perp",
        "side": "SHORT",
        "venue": "hyperliquid",
        "apy": {"value": 0.12}  # Funding received
    },
    "net_apy": 0.20  # Lending yield + funding
}
```

**Lend + long fixed rate (lock in yield):**
```python
{
    "carry_leg": {
        "instrument_type": "lending",
        "side": "LONG",
        "venue": "moonwell",
        "apy": {"value": 0.08}  # Floating lending rate
    },
    "hedge_leg": {
        "instrument_type": "fixed_rate",
        "side": "LONG",
        "venue": "boros",
        "apy": {"value": -0.05}  # Fixed rate you lock in (cost)
    },
    "net_apy": 0.03  # Locked-in spread
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
