---
name: developing-jobs-v1-strategies
description: Build, backtest, diagnose, and iterate a jobs_v1 execution strategy end to end (decide(ctx) contract, windowed indicators to keep backtests fast, --quick sweeps, backtest-diagnose). Load this before creating a trading job/strategy.
metadata:
  tags: jobs, jobs_v1, strategy, backtest, execution, decide, hyperliquid
---

## When to use

Load this when the user wants a **recurring, backtestable trading strategy** (e.g. "create a strategy that shorts X on a breakout"). This is the jobs_v1 execution framework — one `decide()` runs in backtest, paper, and live.

Do NOT hand-write the strategy into `.wayfinder_runs/` (that's one-off scratch scripts, `core_run_script`). Do NOT copy `wayfinder_paths/strategies/apex_gmx_velocity/` — that's the **old `Strategy` base class**, not jobs_v1.

## The loop (use exactly these commands)

```
wayfinder job create <id> --script <strategy.py> --execution-contract jobs_v1 --interval 3600 --initial-capital 1000
# edit execution_spec.json (data_contract.symbols + bar_interval) and the strategy
wayfinder job fetch-dataset <id> --days 180
wayfinder job backtest <id> --quick 1000      # fast iteration: last 1000 bars, ~2 KB summary
wayfinder job backtest-diagnose <id>          # win/PnL by exit reason, hour, side — READ THIS, don't recompute
# tune params, repeat backtest --quick / diagnose
wayfinder job experiments <id> --grid grid.json   # sweep several params at once (bounded, CPU-safe)
wayfinder job backtest <id>                   # full-history confirmation run
```

The `backtest` output is a compact stats summary (`stats` + `profile` + artifact paths). Add `--full` only if you truly need the raw curves; they're always on disk (`job backtest-view`).

## The strategy contract (copy this skeleton)

```python
from wayfinder_paths.jobs.execution import ExecutionContext, OrderIntent

# Longest indicator lookback + a small buffer. Handed to the sim as the per-bar
# compute window so `decide()` NEVER sees the whole growing history — that is
# what turns a "simple" backtest into a multi-minute CPU peg.
warmup_bars = 60

def decide(ctx: ExecutionContext) -> list[OrderIntent]:
    frame = ctx.view.symbol_frame("IMX")   # already bounded to ~warmup_bars rows
    if len(frame) < 25:
        return []
    close = frame["close"].astype(float)
    ema = close.ewm(span=9, adjust=False).mean().iloc[-1]
    last = float(close.iloc[-1])
    low5 = float(close.iloc[-6:-1].min())
    pos = ctx.ledger.positions.get("IMX")
    if pos is None and last < low5:
        return [OrderIntent(action="open", venue="hyperliquid", symbol="IMX",
                            side="short", notional=100.0, reduce_only=False,
                            bracket={"stop_loss": last * 1.07, "take_profit": last * 0.9,
                                     "policy": "conservative"},
                            metadata={"entry_price": last})]
    if pos is not None and last > ema:
        return [OrderIntent(action="close", venue="hyperliquid", symbol="IMX",
                            side="buy", size=pos.size, reduce_only=True,
                            metadata={"exit_reason": "ema_reclaim"})]
    return []

def build_strategy(params: dict | None = None):
    import types
    ns = types.SimpleNamespace(); ns.decide = decide; ns.warmup_bars = warmup_bars
    return ns
```

Put `metadata={"exit_reason": "..."}` on close intents — `backtest-diagnose` buckets PnL by it.

## Rules (these are the mistakes to avoid)

1. **Keep `decide()` cheap.** Compute indicators on the handed frame only (it's bounded to `warmup_bars`). Never re-slice the full history or `copy()` a growing frame every bar. If a backtest is slow, the `profile.hint` tells you why — set/lower `warmup_bars`.
2. **Never recompute PnL/stats by hand.** Ad-hoc pandas drifts from the framework and confuses everyone ("why is your PnL different than the framework?"). Read `job backtest` `stats` and `job backtest-diagnose`. They are the source of truth.
3. **Sweep parameters with `job experiments` / `--grid`, not manual edit-and-rerun.** It's CPU-safe (bounded to the box's cores) and runs several params at once. Add `--quick` while exploring.
4. **The dev box is small (2 vCPU).** Iterate on `--quick 1000`; only do the full-history run to confirm a candidate. Don't wrap backtests in `timeout` — read the ETA on the progress line.
5. **The strategy lives in the job's script**, not `.wayfinder_runs/`. Edit that file, then `wayfinder job backtest` — don't maintain a scratch copy.
