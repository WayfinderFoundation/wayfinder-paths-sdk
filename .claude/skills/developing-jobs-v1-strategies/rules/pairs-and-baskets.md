# Pairs & baskets — multi-leg strategies done right

The framework holds **simultaneous positions natively**: the ledger keys
positions by symbol, `decide()` may emit several intents per tick, and equity
aggregates across all open legs. **Never build a one-sided "proxy" for a
spread trade** — a long-only stand-in is not market-neutral; its P&L is
mostly directional beta, and it tells you nothing about the spread.

## Step 0 (mandatory): the admission gate

Before building ANY pair idea, run the gate:

```
wayfinder job pair-check <id> --symbols ETH,SOL --days 720
# MCP: core_jobs(action="pair_check", job_id=…, symbols=["ETH","SOL"], days=720)
```

Eight checks; hard fails REJECT: Engle–Granger cointegration **both
directions** on log prices, OU **half-life** in a tradeable band (12–480h),
**rolling stability** (cointegrated in most 75-day windows, not just the full
sample), cost hurdle (expected capture ≥ 3× both-leg round trip), data
sufficiency (≥ 365d — one trending regime cannot validate mean reversion).
Advisory: Hurst (> 0.5 = the spread trends — fading it is fading a trend),
mean-crossing frequency, net funding carry.

**A REJECT is the methodology working.** Correlated is not cointegrated:
most crypto majors track each other but their ratio trends, and no z-score
threshold rescues a spread that does not mean-revert. On REJECT, tell the
user plainly and offer the alternatives that DO have documented edge (below).
On PASS, the result hands you the parameters: `suggested.hedge_ratio` (size
legs by this, never 1:1), `lookback_bars` (~4× half-life), `time_stop_bars`
(~2.5× half-life).

## The multi-leg skeleton (hedge-ratio sized, both legs emitted together)

```python
import numpy as np

from wayfinder_paths.jobs.execution import OrderIntent

warmup_bars = 200  # >= z-score lookback + buffer

def precompute(frames: dict) -> dict:
    a = np.log(frames["ETH"]["close"].astype(float))
    b = np.log(frames["SOL"]["close"].astype(float))
    hedge = 0.63           # from pair-check suggested.hedge_ratio
    lookback = 92          # ~4x half-life in bars, from suggested.lookback_bars
    spread = a - hedge * b
    z = (spread - spread.rolling(lookback).mean()) / spread.rolling(lookback).std()
    feats = frames["ETH"][[]].copy()
    feats["pair_z"] = z
    # Live kill switch: rolling ADF of the spread; stand down on breakdown.
    # from wayfinder_paths.jobs.research import rolling_adf_kill  (purity-safe)
    # feats["adf_kill"] = rolling_adf_kill(spread, window=lookback * 4)
    return {"ETH": feats}

def decide(ctx):
    z = float(ctx.view.latest("ETH")["pair_z"])  # last row incl. precompute cols
    eth, sol = ctx.ledger.positions.get("ETH"), ctx.ledger.positions.get("SOL")
    hedge, notional = 0.63, 100.0
    if eth is None and sol is None and z > 2.0:
        # Spread rich: short ETH, long SOL — BOTH legs, hedge-ratio sized.
        return [
            OrderIntent(action="open", venue="hyperliquid", symbol="ETH",
                        side="short", notional=notional, reduce_only=False),
            OrderIntent(action="open", venue="hyperliquid", symbol="SOL",
                        side="long", notional=notional * hedge, reduce_only=False),
        ]
    if eth is not None and sol is not None and abs(z) < 0.5:
        return [
            OrderIntent(action="close", venue="hyperliquid", symbol="ETH",
                        side="buy", size=eth.size, reduce_only=True,
                        metadata={"exit_reason": "z_exit"}),
            OrderIntent(action="close", venue="hyperliquid", symbol="SOL",
                        side="sell", size=sol.size, reduce_only=True,
                        metadata={"exit_reason": "z_exit"}),
        ]
    return []
```

Mirror the entry for z < −2.0, add the time stop (`suggested.time_stop_bars`)
and the ADF kill switch as exits. `execution_spec.data_contract.symbols` must
list BOTH symbols, and `fetch-dataset`/`fetch-funding` pull both.

## Baskets: target weights, not hand-rolled intents

For 3+ legs (long strongest / short weakest), compute signed target weights in
`decide()` and let the bridge diff them against the ledger:

```python
from wayfinder_paths.jobs.strategies.portfolio import target_weights_to_intents

REBALANCE_BARS = 72  # rebalance cadence in bars

def decide(ctx):
    # Cheap gates FIRST — neither touches a DataFrame, so the strategy does
    # zero pandas work on the ~71 of 72 ticks it skips.
    # WARMUP gates on data in the view (bar_index); CADENCE gates on the
    # epoch-aligned every_n_bars — NEVER on bar_index % n (constant in live's
    # sliding window) and NEVER on tick counters in strategy_state (a state
    # reset re-warms them and the job goes dark for a full warmup period).
    # Ages, cooldowns and expiries: stamp ctx.bar_ordinal, measure with
    # ctx.bars_since(stamp) — never a stored bar_index (it never advances).
    if ctx.bar_index < warmup_bars or not ctx.every_n_bars(REBALANCE_BARS):
        return []
    # Rankings/vols are precompute columns; read only the LAST row per symbol.
    rows = {sym: ctx.view.latest(sym) for sym in ctx.view.symbols}
    weights = weights_from(rows)   # your ranking → signed weights, gross 1.0
    return target_weights_to_intents(ctx, weights, rebalance_threshold=0.05,
                                     min_trade_notional=15.0)
```

**Keep `decide()` cheap for baskets:** everything heavy (rankings, smoothed
funding, vols) is a `precompute` column; `decide()` gates on `ctx.bar_index`
(warmup) + `ctx.every_n_bars(n)` (cadence) and reads `ctx.view.latest(sym)`. Per-tick pandas inside `decide()` is the
10x slowdown that turns every grid into a crawl.

It handles opens, closes, sign flips, and partial rebalances; gross > 1 is
normalized unless you pass `normalize_gross=False` (deliberate leverage).
Weight by inverse volatility, not equally — equal notional concentrates risk
in the most volatile leg.

## The four families with documented post-cost edge

When a pair gate REJECTs (or the user just wants "something that makes
money"), these are the structurally sound directions, roughly in order of
documented strength:

1. **Funding-carry basket** — rank the liquid universe by smoothed funding,
   short the richest / long the cheapest (or collect one side vs spot),
   inverse-vol weights, daily/weekly rebalance. Funding is first-class:
   `fetch-funding --days 365` puts a `funding` column on every bar.
2. **Cross-sectional momentum** — ~28d formation, weekly rebalance, liquid
   top-40 only; longs strongest / shorts weakest as a market-neutral basket.
3. **Time-series trend, vol-targeted** — daily bars, long above / short below
   a slow trend measure, position size scaled to inverse realized vol. The
   lowest-infrastructure edge; crypto daily data trends.
4. **Funding-spread convergence pair** — the "spread" is the FUNDING
   differential between two perps, beta-neutral price legs; you are paid the
   carry while waiting for convergence. Gate the price relationship for
   stability, but the edge is the funding spread, not price reversion.

Funding is a first-order P&L term for every perp strategy — model it as
carry (`net_funding_carry` in the pair-check output; the `funding` column in
precompute), not just an entry filter.

## Cost honesty for multi-leg

Every round trip is **2 legs × entry + exit** — at 8.5bp per leg round trip,
~17bp per pair cycle. The gate's cost hurdle demands expected capture
(z_entry − z_exit) × spread σ ≥ 3× that. If a tuned pair only clears PF 1.0
by shrinking the z-exit, it is paying the spread capture back out in fees —
check `total_fees` vs gross P&L in diagnose.
