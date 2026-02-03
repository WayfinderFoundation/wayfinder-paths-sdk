# Rate locking recipes (delta-neutral workflows)

Primary reference (Boros):
- Fixed funding receivables (basis traders): https://pendle.gitbook.io/boros/boros-academy/benefits-of-boros/fixed-funding-rates-receivables
- Fixed funding payments: https://pendle.gitbook.io/boros/boros-academy/benefits-of-boros/hedging-funding-rates-payment

## Funding sign convention (quick reference)

- **Negative funding** → shorts pay longs (longs receive)
- **Positive funding** → longs pay shorts (shorts receive)

See `gotchas.md` for more detail.

## Recipe: lock funding for a perp short (common delta-neutral hedge)

Scenario:
- You are **short** a perp on the underlying venue (e.g., Hyperliquid HYPE-PERP) to hedge a spot long.
- Funding is **positive** → you expect to **receive** funding as a short, but you don't want that funding to flip or collapse.

According to Boros docs, the standard hedge is:
- Open a **short YU** position on the matching Boros market to lock in a fixed funding receivable.

In Wayfinder Paths, the building blocks are:
- Funding observations:
  - `HyperliquidDataClient.get_funding_history(coin, start_ms, end_ms)` (Wayfinder API time series), or
  - perp meta/context via `HyperliquidAdapter.get_meta_and_asset_ctxs()`
  - Note: `HyperliquidAdapter` does **not** implement `get_funding_history(...)` (that was the cause of recent script failures).
- Fixed-rate observations: `BorosAdapter.quote_market(...)` / `quote_markets_for_underlying(...)`
- Execution: `BorosAdapter.place_rate_order(...)` (plus deposits/withdrawals)

## Recipe: hedge funding payments for a perp long

Scenario:
- You are **long** a perp and funding is **positive** (you're paying funding to shorts).

According to Boros docs:
- Open a **long YU** position on the matching Boros market to lock in a fixed funding payment.

## How to answer “what is the rate now?”

In this repo, “the Boros fixed rate now” for a given market means:
- `BorosMarketQuote.mid_apr` (or `best_bid_apr` / `best_ask_apr` depending on which side you’ll take).

Important:
- Do **not** estimate “current rates” from memory or from old values; always fetch fresh quotes via the adapter.

Repo example (HYPE):
- `wayfinder_paths/strategies/boros_hype_strategy/constants.py` sets `BOROS_HYPE_MARKET_ID = 18` (Hyperliquid HYPE-USD market).
- To read "rate now" for that exact market, you can:
  1) `success, market = await boros.get_market(18)`
  2) `success, quote = await boros.quote_market(market)`
  3) use `quote.mid_apr` / `quote.best_*_apr`

Practical discovery flow (read-only):
1) `success, markets = await boros.list_markets(is_whitelisted=True, ...)`
2) Filter to the underlying/perp venue you care about (e.g., Hyperliquid + HYPE)
3) For each candidate market: `success, quote = await boros.quote_market(market)`
4) Sort by `quote.mid_apr` and choose a tenor/maturity that matches your strategy horizon

Best practice:
- Also pull `get_orderbook(market_id, ...)` and estimate slippage before placing size.

## How to find *new* delta-neutral opportunities (beyond the existing strategy)

Look for underlyings where **three things overlap**:
1) A spot yield leg exists (staking, lending, loop, etc.)
2) A perp market exists (for delta hedging)
3) A Boros market exists (for funding rate lock)

Then evaluate:
- Spot yield (APY, lockups, liquidity)
- Expected funding (history + current, stability, regime changes)
- Boros implied APR (what you can lock now)
- Liquidity + slippage across *all* legs
- Liquidation risk on the perp hedge (leverage, margin buffers, volatility)
- Operational constraints (bridge delays, Boros withdrawal cooldowns, maturity roll)

In this repo, a good “first pass scanner” usually:
- enumerates venues/markets (read-only)
- computes net expected carry under conservative assumptions
- outputs an `ExecutionRequest` draft (but does not execute)
