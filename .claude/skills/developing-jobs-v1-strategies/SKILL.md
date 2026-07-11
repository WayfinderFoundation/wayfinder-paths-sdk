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
wayfinder job backtest-diagnose <id>          # READ next_step + recommendations — the framework tells you what to try
# apply the ONE change next_step names, repeat backtest --quick / diagnose (see "the improve loop" below)
# THE decision step — tune AND validate out-of-sample in one shot (never trust an in-sample grid):
wayfinder job experiments <id> --grid grid.json --wf-test-bars 240 --wf-folds 4
wayfinder job backtest <id>                   # full-history confirmation of the promoted params
```

The `backtest` output is a compact stats summary (`stats` + `profile` + artifact paths). Add `--full` only if you truly need the raw curves; they're always on disk (`job backtest-view`).

## The improve loop — read the recommendations, change ONE thing (don't thrash)

`backtest-diagnose` returns a **`next_step`** (the single most important action) and a ranked **`recommendations`** list, each with the `evidence` (the actual stats/buckets) that triggered it — computed by the framework, so you never hand-roll `json.load(latest.json)` one-liners again. The loop that keeps churn low:

1. Run `backtest --quick`, then `backtest-diagnose`.
2. Read `next_step`. Apply **exactly one** change it names (widen the stop, add an entry filter, cut leverage, loosen entry…). Changing three things at once means you can't tell which one helped.
3. Re-run `backtest --quick` and compare the headline. Kept the gain? Keep the change. Otherwise revert it.
4. Repeat until `recommendations[0].severity == "validate"` (a promising in-sample result) — then go to the decision step.

Severity ladder: `blocking` (too few trades / liquidated — fix first, nothing downstream matters) → `high` (no edge / poor payoff) → `medium` (trend-riding / costs / stop bleed) → `low` (side or session skew) → `validate` (good enough to prove out-of-sample). A `blocking` rec means **stop tuning params** and fix that first.

**Limited churn:** iterate on `--quick`, one change per loop, and let `job experiments` do parameter sweeps in one CPU-safe pass. A full-history `backtest` is only for confirming a promoted candidate — re-running it after every tweak is the churn that pegs the box. If `--quick 1000` takes more than ~1–2 minutes, lower `warmup_bars` or the quick window; that's a signal, not something to wait out.

## Judge a strategy out-of-sample, or you're just curve-fitting

A single backtest tunes and scores on the **same** data — its Sharpe / profit factor are **not evidence of an edge**. A "great" in-sample result (Sharpe 6+, PF 10+) almost always means you overfit. The only number that means anything is **out-of-sample**:

- Run `job experiments <id> --grid grid.json --wf-test-bars 240 --wf-folds 4`. This picks params on each fold's train window and scores them on the **held-out** window it never saw. It defaults to a **bounded rolling** train window, so it stays fast (seconds–low minutes) even on a full dataset — don't pass `--wf-anchored` (expanding window, ~4x slower) unless you specifically want anchored folds, and don't hand-shrink the dataset to make it finish.
- Read the walk-forward report and judge on: **`decay_ratio`** (OOS mean ÷ IS mean — want it near 1; ≪ 1 = overfit), **`oos_positive_folds`** (want most folds profitable OOS), and OOS mean return vs IS mean. If OOS collapses vs IS, the strategy is fit to noise — go back to the idea, don't tune harder.
- Also sanity-check the *regime*: if you're shorting an asset that fell 50% over the window, most of the "edge" is the trend, not the strategy — confirm it holds on a flat/up stretch too.
- **Model costs.** Set `fee_bps` / `slippage_bps` in `execution_params`; a strategy with hundreds of trades and `total_fees: 0` is fiction.

## Ship it — offer to deploy once (and only once) it validates

When a candidate clears walk-forward (`decay_ratio` near 1, most `oos_positive_folds`), it's earned a deploy. Do this, don't skip to it:

1. `wayfinder job promote-params <id> --grid <grid_id>` — writes the winning params into the job's `execution_params`.
2. `wayfinder job backtest <id>` — one full-history confirmation on the promoted params.
3. **Offer the deploy to the user** — summarize the OOS numbers (net, decay_ratio, oos_positive_folds, max drawdown) and *ask* before making it live. Going live is fund-moving; never enable it unprompted.
4. On a yes: enable the runner loop (`wayfinder job set-mode <id> --mode monitor` / `wayfinder job resume <id>`, or `core_runner` add-job) so `update` runs on the interval.

Do NOT offer to deploy a strategy that only looks good in-sample — that's the curve-fit trap the whole loop exists to avoid.

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

## Make it fast: `precompute` (one vectorized pass, not per-bar pandas)

Per-bar pandas inside `decide()` costs ~5ms of fixed overhead per rolling/ewm/concat call — a 15-op `decide()` runs at ~30 bars/s and turns every grid into a crawl. Move ALL indicator math into the optional `precompute` hook: one vectorized pass, columns merged onto the bars, `decide()` just reads them.

```python
def precompute(frames: dict) -> dict:
    """frames: per-symbol raw bars (full history in backtest, bounded window
    live). Return per-symbol frames of derived columns, row-aligned with the
    input. CAUSAL transforms only (rolling / shift / ewm) — never anything
    that reads future rows."""
    close = frames["IMX"]["close"].astype(float)
    feats = frames["IMX"][[]].copy()
    feats["z"] = (close - close.rolling(20).mean()) / close.rolling(20).std()
    return {"IMX": feats}

def decide(ctx: ExecutionContext) -> list[OrderIntent]:
    frame = ctx.view.symbol_frame("IMX")     # includes the "z" column
    z = float(frame["z"].iloc[-1])
    ...

def build_strategy(params: dict | None = None):
    import types
    ns = types.SimpleNamespace()
    ns.decide = decide; ns.precompute = precompute; ns.warmup_bars = 60
    return ns
```

Cross-symbol features (pair z-scores, spreads) read several input frames and attach to the traded symbol's rows. FUNDING RATES are first-class: `wayfinder job fetch-funding <id> --days 60 --exchange binance` (or `hyperliquid`) pulls history into the feature store and declares the feature — bars then carry a `funding` column in backtest AND live. Long-history candles: `wayfinder job fetch-dataset <id> --days 365 --source ccxt --exchange binance`. Other exogenous series follow the same shape: declare under `execution_spec.data_contract.features`, append rows to `state/features.jsonl` (`{timestamp, name, value, symbol}`).

## Rules (these are the mistakes to avoid)

1. **Keep `decide()` cheap.** Compute indicators on the handed frame only (it's bounded to `warmup_bars`). Never re-slice the full history or `copy()` a growing frame every bar. If a backtest is slow, the `profile.hint` tells you why — set/lower `warmup_bars`.
2. **Never recompute PnL/stats by hand.** Ad-hoc pandas drifts from the framework and confuses everyone ("why is your PnL different than the framework?"). Read `job backtest` `stats` and `job backtest-diagnose`. They are the source of truth.
3. **Tune AND validate with `job experiments --grid … --wf-test-bars N --wf-folds K`, not manual edit-and-rerun.** It's CPU-safe (bounded to the box's cores), sweeps several params at once, and — crucially — scores them **out-of-sample**. A plain grid or a hand-tuned single backtest is in-sample only; its metrics are not an edge. Decide with `decay_ratio` and `oos_positive_folds`, not in-sample Sharpe.
4. **The dev box is small (2 vCPU).** Iterate on `--quick 1000`; only do the full-history run to confirm a candidate. Don't wrap backtests in `timeout` — read the ETA on the progress line.
5. **The strategy lives in the job's script**, not `.wayfinder_runs/`. Edit that file, then `wayfinder job backtest` — don't maintain a scratch copy.
