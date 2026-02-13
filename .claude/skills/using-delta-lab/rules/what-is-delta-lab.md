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
- **LONG** opportunities - You receive yield (lending, PT, short perp funding)
- **SHORT** opportunities - You pay yield (borrowing, YT, long perp funding)

### Delta-Neutral Pair

A **delta-neutral pair** consists of:
- **Carry leg** - The position earning yield
- **Hedge leg** - The position offsetting price exposure
- **Net APY** - Combined yield after hedging costs

Example: Long BTC spot + short BTC perp = delta-neutral carry trade

### Instrument Types

Different ways to gain exposure:
- `perp` - Perpetual futures
- `spot` - Spot holdings
- `lending` - Lending positions
- `fixed_rate` - Fixed-rate markets (Boros)
- `pt` - Principal tokens (Pendle)
- `yt` - Yield tokens (Pendle)

## When to Use Delta Lab

Use Delta Lab when you need to:
- Find the highest APY for a given asset across all protocols
- Discover delta-neutral opportunities
- Compare funding rates vs lending rates vs fixed rates
- Build basis trading strategies
- Analyze risk-adjusted yields
