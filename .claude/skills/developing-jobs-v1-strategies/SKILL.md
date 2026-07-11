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
# THE decision step — tune AND validate out-of-sample in one shot (never trust an in-sample grid):
wayfinder job experiments <id> --grid grid.json --wf-test-bars 240 --wf-folds 4
wayfinder job backtest <id>                   # full-history confirmation of the promoted params
```

The `backtest` output is a compact stats summary (`stats` + `profile` + artifact paths). Add `--full` only if you truly need the raw curves; they're always on disk (`job backtest-view`).

## Judge a strategy out-of-sample, or you're just curve-fitting

A single backtest tunes and scores on the **same** data — its Sharpe / profit factor are **not evidence of an edge**. A "great" in-sample result (Sharpe 6+, PF 10+) almost always means you overfit. The only number that means anything is **out-of-sample**:

- Run `job experiments <id> --grid grid.json --wf-test-bars 240 --wf-folds 4`. This picks params on each fold's train window and scores them on the **held-out** window it never saw.
- Read the walk-forward report and judge on: **`decay_ratio`** (OOS mean ÷ IS mean — want it near 1; ≪ 1 = overfit), **`oos_positive_folds`** (want most folds profitable OOS), and OOS mean return vs IS mean. If OOS collapses vs IS, the strategy is fit to noise — go back to the idea, don't tune harder.
- Also sanity-check the *regime*: if you're shorting an asset that fell 50% over the window, most of the "edge" is the trend, not the strategy — confirm it holds on a flat/up stretch too.
- **Model costs.** Set `fee_bps` / `slippage_bps` in `execution_params`; a strategy with hundreds of trades and `total_fees: 0` is fiction.

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
3. **Tune AND validate with `job experiments --grid … --wf-test-bars N --wf-folds K`, not manual edit-and-rerun.** It's CPU-safe (bounded to the box's cores), sweeps several params at once, and — crucially — scores them **out-of-sample**. A plain grid or a hand-tuned single backtest is in-sample only; its metrics are not an edge. Decide with `decay_ratio` and `oos_positive_folds`, not in-sample Sharpe.
4. **The dev box is small (2 vCPU).** Iterate on `--quick 1000`; only do the full-history run to confirm a candidate. Don't wrap backtests in `timeout` — read the ETA on the progress line.
5. **The strategy lives in the job's script**, not `.wayfinder_runs/`. Edit that file, then `wayfinder job backtest` — don't maintain a scratch copy.
