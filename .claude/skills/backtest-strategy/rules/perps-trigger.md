# Trigger pattern — `backtest_perps_trigger` (Active perps + HIP-3)

For actively-traded Hyperliquid strategies (perp or HIP-3 dexes), use this pattern.
The strategy code you write here is **the same code that runs live and reconciles** —
no copy-paste, no re-implementation, no drift.

## When to use

- Hyperliquid perp strategies that trade on a per-bar trigger (mean-reversion, momentum
  with stops, signal-driven entry/exit, etc.).
- HIP-3 dex strategies (xyz, flx, vntl, hyna, km).
- Anything where execution decisions per bar are non-trivial (slippage-aware orders,
  partial fills, reduce-only flips, multi-venue allocation).

If your strategy is fully expressible as a "target weights vector per bar" with no
runtime decisions, plain `quick_backtest` / `backtest_delta_neutral` are simpler.

## Architecture

```
signal_fn(prices, funding, params) → SignalFrame      # pure, vectorized
decide_fn(ctx) → places orders via ctx.<venue>.handler  # per-bar
```

`ctx` exposes:
- `ctx.perp` — primary perp `MarketHandler`
- `ctx.hip3[dex]` — handler per declared HIP-3 dex
- `ctx.params` — frozen params from the ref / backtest call
- `ctx.state` — free-form `StateStore` (persists across `update()` in live)
- `ctx.signal` — precomputed `SignalFrame` (vectorized over full history in backtest)
- `ctx.t` — current bar timestamp (use this — never `datetime.now()`)

Three handler implementations satisfy the same `MarketHandler` protocol:
- `BacktestHandler` — fills queue to next-bar open. `fill_model="replay"` fills on the same bar the signal was computed; use ONLY for live↔history reconciliation.
- `LiveHandler` — wraps `HyperliquidAdapter`.
- `ReconcileHandler` — records intents only, reads positions from `StateStore.snapshot_at(t)`.

## Workflow

1. Write `signal.py` with `compute_signal(prices, funding, params) -> SignalFrame`.
   - Pure. Vectorized. No `datetime.now()`, `time.*`, or RNG (a purity sandbox catches these).
2. Write `decide.py` with `async def decide(ctx) -> None`.
   - Reads `ctx.signal_at_now()`, places orders via `ctx.perp.place_order(...)`.
   - Optionally call `await scale_pending_atomically(ctx)` (legacy-fidelity throttle)
     or `await reservable_size_for(ctx, ctx.perp, ...)` per order (live-faithful FIFO).
3. Run `await backtest_perps_trigger(signal_fn=..., decide_fn=..., symbols=..., start=..., end=..., params=...)`.
   - Output is a `BacktestResult` with the same shape as `quick_backtest`.
4. Iterate on params/signal/decide until performance is acceptable.
5. **If deploying:** call `emit_backtest_ref(...)` to write `backtest_ref.candidate.json`,
   then `scripts/promote_backtest_ref.py <strategy>` to bond it to `backtest_ref.json`.

## Required disciplines

- **`decide(ctx)` is a pure function of `ctx`. No side-channel state reads.**
  Every value decide needs is on the context. The framework guarantees these are
  computed the same way in backtest and live:
  - **NAV** — read `ctx.nav`. Never call `await ctx.perp.get_margin_balance()` from
    inside decide. In backtest, `BacktestHandler.get_margin_balance()` returns `0.0`
    (the driver tracks NAV separately); in live, calling it bypasses the framework's
    snapshot of pre-trade truth. This is the canonical live↔backtest divergence trap.
  - **Positions** — `await ctx.perp.get_positions()`. Same shape in backtest (handler-tracked)
    and live (exchange-queried).
  - **Mids** — `ctx.perp.mid(sym)`. Synchronous; backtest reads the bar's price, live reads
    pre-fetched mids.
  - **Time** — `ctx.t`. Never `datetime.now()` (purity sandbox raises `PurityViolation`).
- **`ctx.state` is strategy-owned, not framework-owned.** Use it for multi-bar bookkeeping
  the strategy genuinely needs (e.g. a cooldown timer, a regime flag). Do NOT smuggle
  framework values like NAV into `ctx.state` — that's how the apex_gmx NAV bug crept in
  (decide stored NAV on first run, then read the stale value forever after).
- **Slippage helpers are idealized in backtest.** `quantity_at_price` / `price_for_quantity`
  assume infinite depth at mid. Reconciliation catches the deviation between assumed and
  realized live slippage.
- **Slippage helpers are idealized in backtest.** `quantity_at_price` / `price_for_quantity`
  assume infinite depth at mid. Reconciliation catches the deviation between assumed and
  realized live slippage.
- **HIP-3 venues must be declared.** Strategies declare `HIP3_DEXES = ["xyz", ...]` on the
  parent class; the framework refuses to run if an unknown dex is named.
- **Don't override locked methods.** `ActivePerpsStrategy.update()` and `_run_trigger()` are
  `@final`; subclassing tries to override them raise at class definition.

## Sizing helpers (opt-in, in `wayfinder_paths.core.perps.sizing`)

- `await scale_pending_atomically(ctx)` — proportionally throttle queued orders to fit
  free margin + costs. Mirrors legacy `run_backtest`'s `get_atomic_trade_scale`.
  Backtest-only (live no-ops; the exchange enforces margin server-side).
- `await reservable_size_for(ctx, handler, sym, side, requested_size)` — per-order primitive
  that returns the largest size that fits remaining margin. FIFO-faithful — call once per
  order before placing. Works in backtest and live identically.

Pick one strategy: legacy-fidelity (atomic) for parity tests; live-faithful (reservable)
for production decide.

## Backtest ref file

The ref is a **deployment manifest**, not a strategy spec. It pins:
- Source SHA-256 of `signal.py` and `decide.py` modules
- Data symbols + interval + window + `fingerprint_frames(...)` over price+funding
- Frozen `params` dict
- `execution_assumptions` (fill_model, fee_bps, slippage_bps, min_order_usd)
- `performance` (recorded backtest stats — `sharpe`, `total_return`, `max_drawdown`, etc.)
- `monitoring.drift_tolerances` (per-axis severity thresholds the reconciler applies)

Logic lives in code modules; the ref pins what was actually used to produce the published
numbers so the reconciler can detect drift between live and validated backtest.

## Subclass shape

```python
from pathlib import Path
from wayfinder_paths.core.strategies.active_perps import ActivePerpsStrategy

class MyStrategy(ActivePerpsStrategy):
    REF = Path(__file__).parent / "backtest_ref.json"
    SIGNAL = "wayfinder_paths.strategies.my_strategy.signal:compute_signal"
    DECIDE = "wayfinder_paths.strategies.my_strategy.decide:decide"
    HIP3_DEXES = []
```

Lifecycle methods (`deposit`, `withdraw`, `exit`, `_status`, `quote`) have sensible
defaults; override per-strategy as needed. `update()` and `_run_trigger()` are locked.

## Reconciliation

Run `scripts/active_perps_strategy_recon.py <strategy> --start ... --end ...` to replay
decide() against historical state snapshots and diff against live HL fills. Reports drift
on five axes (trigger_timing, decision_parity, size_drift, fill_price_drift, fill_completion)
and writes `<strategy_dir>/reconciliation/<run_ts>.json`. **Never halts** — agent inspects
the report and decides what to do.

Recommended runner job (cron-style, daily window):

```bash
poetry run wayfinder runner add-job \
  --name <strategy>-recon --type script \
  --script scripts/active_perps_strategy_recon.py \
  --args "<strategy> --start ... --end ..." \
  --interval 86400
```

## Risk limits

Drop a `risk_limits.json` next to `backtest_ref.json` for opt-in halts:

```json
{
  "max_drawdown": -0.15,
  "max_gross_exposure_usd": 5000,
  "max_position_per_symbol_usd": 2000,
  "max_daily_loss_usd": 200,
  "pause_after_consecutive_losses": 5
}
```

All keys optional. Absent file = no halts. Limits are checked at the top of `update()`
before the trigger runs and return `(False, "Halted: ...")` rather than raising.
