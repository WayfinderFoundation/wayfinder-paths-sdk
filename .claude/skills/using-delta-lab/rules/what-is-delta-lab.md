# What is Delta Lab?

Delta Lab is a **basis APY discovery and delta-neutral strategy research tool** that aggregates opportunities across multiple DeFi protocols and venues.

## What are "Basis" Assets?

A **basis** refers to a fundamental asset (e.g., BTC, ETH, HYPE) that can be:
- Held spot
- Traded perpetually
- Lent/borrowed
- Used as collateral
- Traded via fixed-rate markets
- Used in yield-bearing positions

## What Delta Lab Does

Delta Lab provides:

1. **Basis APY Sources** - All yield opportunities for a given asset across protocols
2. **Delta-Neutral Pairs** - Matched carry/hedge positions that neutralize price exposure
3. **Asset Metadata** - Lookup asset symbols, addresses, coingecko IDs by internal asset_id

## Data Sources

Delta Lab aggregates from:
- **Hyperliquid** - Perp funding rates, spot markets
- **Moonwell** - Lending/borrowing APRs
- **Boros** - Fixed-rate funding markets
- **Hyperlend** - Lending markets
- **Pendle** - PT/YT yields
- Other DeFi protocols

## Basis Symbols

When querying, use the **root symbol** (not coingecko ID):
- `BTC` - Bitcoin basis opportunities
- `ETH` - Ethereum basis opportunities
- `HYPE` - Hyperliquid basis opportunities
- `SOL` - Solana basis opportunities
- etc.

The API resolves the symbol to a `basis_group_id` and finds all related assets.

## Key Concepts

### Opportunity

An **opportunity** is a single position that provides yield:
- **LONG** side - Positions where `side="LONG"` (supply/lend, hold yield token/PT, long perp, receive fixed rate)
- **SHORT** side - Positions where `side="SHORT"` (borrow, short perp, pay fixed rate)

Use the sign of `apy.value` to determine receive vs pay:
- Positive `apy.value` → the position receives yield
- Negative `apy.value` → the position pays yield (cost)

### Delta-Neutral Pair

A **delta-neutral pair** consists of:
- **Carry leg** - The position earning yield
- **Hedge leg** - The position offsetting price exposure
- **Net APY** - Combined yield after hedging costs

Example: Supply wstETH (`LENDING_SUPPLY`) + short ETH perp (`PERP`) = delta-neutral carry trade

### Instrument Types

Delta Lab opportunities use these `instrument_type` enums:
- `PERP` - Perpetual futures (funding)
- `LENDING_SUPPLY` - Supply-side lending
- `LENDING_BORROW` - Borrow-side lending
- `BOROS_MARKET` - Boros fixed-rate markets
- `PENDLE_PT` - Pendle PT markets
- `YIELD_TOKEN` - Yield-bearing token yields

## When to Use Delta Lab

Use Delta Lab when you need to:
- Find the highest APY for a given asset across all protocols
- Discover delta-neutral opportunities
- Compare funding rates vs lending rates vs fixed rates
- Build basis trading strategies
- Analyze risk-adjusted yields
