# What is Boros?

Boros (by Pendle Finance) is an **on-chain interest-rate swap / yield-trading venue** that (at launch) focuses on perp funding rates, with designs intended to support other "rate oracles" over time.

It uses a **hybrid CLOB + AMM model** and supports **margin/leverage**.

## Yield Units (YU)

Rates are represented as **Yield Units (YU)**: roughly, "1 YU corresponds to the yield stream of 1 unit of collateral" for a given market.

- For USDT collateral (token_id=3): 1 YU ≈ $1 worth of yield exposure
- For HYPE collateral (token_id=5): 1 YU = 1 HYPE worth of yield exposure

## How You Use It (Conceptually)

1. **Pick a market** (e.g., ETH funding rate on Hyperliquid)
2. **Post collateral** (USDT, HYPE, etc.)
3. **Trade long/short YU** to express a view or hedge funding exposure

## Implied APR vs Underlying APR

The key pricing concept:

- **Implied APR**: The "price" of YU on the orderbook — what you lock in when you trade
- **Underlying APR**: The actual funding rate from the perp venue — what settles

**Your PnL depends on:**
- How implied APR moves after your trade
- How the underlying rate settles over time

## Settlement Cadence

Settlement mirrors the underlying venue:
- **Hyperliquid markets**: Hourly settlements
- **Binance markets**: 8-hour settlements
- **OKX markets**: 8-hour settlements

## Margin Types

- **Cross margin** (`is_isolated_only=false`): Collateral shared across all positions
- **Isolated margin** (`is_isolated_only=true`): Collateral locked to a specific market only

## Why Use Boros? (Profit Drivers)

### 1. Trade the "price" of funding expectations (implied APR)

On Boros, the thing that moves like a price is **Implied APR**:
- **Long YU** → profits if implied APR **rises** after entry
- **Short YU** → profits if implied APR **falls** after entry

### 2. Earn "carry" when floating vs fixed is in your favor

Each settlement nets Underlying APR (floating funding) against your fixed implied APR:
- **Long YU** = pay fixed, receive floating → positive carry when **Underlying > Implied**
- **Short YU** = receive fixed, pay floating → positive carry when **Implied > Underlying**

### 3. Arbitrage / relative value (cross-venue)

Traders look for dislocations between:
- Boros implied APR and the venue's realized/expected funding
- Two venues' funding dynamics (relative value)

**Practical note:** Your edge usually comes from (a) forecasting mean-reversion/regime shifts in implied APR, (b) harvesting carry when fixed vs floating is mispriced, and (c) execution (order placement, fees, liquidity, avoiding liquidation).

## Hedge Recipes (Removing Funding-Rate Uncertainty)

Boros is basically an on-chain way to **swap floating funding for a fixed rate** (implied APR) until maturity.

### A) You're paying funding on a perp (e.g., long perp in positive funding)

**Goal:** Lock in a fixed funding cost (stop caring if funding spikes).

**Action:** Open a **LONG YU** for the same notional.

**Result:** You receive the same floating funding from Boros while paying fixed implied APR → netting to "fixed funding paid."

### B) You're receiving funding (e.g., cash-and-carry: long spot + short perp)

**Goal:** Lock in a fixed yield (stop caring if funding collapses).

**Action:** Open a **SHORT YU** for the same notional.

**Result:** You pay floating on Boros and receive fixed implied APR → netting your overall carry to "fixed funding received."

### Sizing Rule of Thumb

Hedge with **~1 YU per 1 unit of perp notional** referenced by that YU market.

**⚠️ Important:** This is the *notional target*, not your deposit size. See `gotchas.md` for the critical distinction between collateral (margin) and YU (notional). Your deposited collateral does **not** determine YU 1:1 — margin requirements depend on mark APR, time to maturity, and protocol factors. Always compute margin-based max size and apply a safety buffer.
