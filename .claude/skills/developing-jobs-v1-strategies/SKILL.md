---
name: developing-jobs-v1-strategies
description: Build, backtest, diagnose, validate, and prepare jobs_v1 trading strategies for deployment. Use for recurring strategies, starter-strategy evaluation, robustness checks, funding-aware perp research, or jobs_v1 strategy PRs.
---

# Developing jobs_v1 strategies

Contract: `jobs-research-contract-v1`.

Load this skill once per session. If the session or compaction summary already
records the contract above, keep using it and open only the rule page needed for
the current step; do not reload this file on every iteration.

## Core loop

Use the framework's `core_jobs` actions when available. The CLI equivalents are
shown for local development:

```text
wayfinder job create <id> --script <strategy.py> --execution-contract jobs_v1 --interval 3600 --initial-capital 1000
wayfinder job fetch-dataset <id> --days 720 --source ccxt --exchange hyperliquid --include-funding
wayfinder job signal-scan <id> --timeframes 1h,4h,1d
wayfinder job backtest <id> --quick 1000
wayfinder job backtest-diagnose <id>
wayfinder job experiments <id> --grid grid.json --wf-test-bars 240 --wf-folds 4
wayfinder job robustness-check <id> --plan robustness.json
wayfinder job backtest <id>
```

For every iteration:

1. Validate the idea before tuning it. Use `strategy-library` before
   reimplementing a shipped strategy, `pair-check` for pairs/baskets,
   `signal-scan` for trigger discovery, `signal-check` for one declared custom
   trigger, and `rank-check` for cross-sectional ranking.
2. Put causal, vectorized indicator work in `precompute(frames)`. Keep
   `decide(ctx)` limited to reading the current bounded view and emitting
   intents. Use shared helpers in `wayfinder_paths.jobs.indicators` and
   `signal_library` before creating another implementation.
3. Iterate with quick backtests. Apply one diagnosed change at a time and keep
   it only when the framework stats improve.
4. Tune and validate with experiments plus walk-forward. A single backtest is
   development evidence, not proof of an edge.
5. Run the declared robustness plan before recommending deployment. Treat
   zero-activation gates and incomplete funding history as unvalidated, not as
   passes.
6. Only promote parameters and offer deployment after out-of-sample and
   robustness evidence are acceptable. Never move funds or enable live mode
   without explicit user approval.

## Strategy invariants

- The strategy lives in `workspace/src/`; only `workspace/` and `job.yaml` are
  versioned and proposable.
- Use `ctx.bar_index` for warmup, `ctx.every_n_bars(n)` for cadence, and
  `ctx.bar_ordinal` / `ctx.bars_since(stamp)` for any age, cooldown or expiry.
  Do not keep tick counters in strategy state, use `bar_index % n`, or store
  `bar_index` as a clock — it is the bounded view length and is constant live.
- `precompute` must be causal: rolling, lagged, expanding, or EWM transforms
  only. No future shifts, centered windows, or full-sample normalization.
- Name observable gates `gate_*` and attach the exact gate column consumed by
  `decide()`. A separately reconstructed audit proxy is not acceptable.
- Positive perp funding means longs pay shorts. Fetch funding with the candle
  dataset when possible and disclose missing symbols or history.
- Framework stats, fills, fees, funding, and drawdowns are the source of truth.
  Do not hand-recompute strategy PnL in a scratch script.
- Put `metadata={"exit_reason": "..."}` on close intents so diagnosis can
  attribute outcomes.
- Model fees and slippage. Use realistic leverage and sweep it against
  drawdown/liquidation risk rather than selecting it from return alone.
- Recent-run scenarios used to design or select a candidate are development
  evidence. They cannot also be its audit holdout.
- Robustness results are advisory in contract v1 and do not bypass the existing
  candidate approval gate.

## Load details only when needed

- Designing or evolving an idea: `rules/strategy-search.md`
- Pairs, baskets, breadth, or multi-leg sizing:
  `rules/pairs-and-baskets.md`
- Gates, funding coverage, scenario roles, or robustness interpretation:
  `rules/robustness-and-gates.md`
- Candidate proposals, agent modes, and post-apply monitoring:
  `rules/deploy-and-agent-loop.md`
- Actual live activation, funding, halt, or withdrawal:
  `rules/going-live.md`

The parent strategy agent owns orchestration and final interpretation. Delegate
only bounded numeric work to the quant agent, passing exact artifacts, ranges,
assumptions, and the required output. Do not ask the quant agent to recreate
the execution engine or promotion gate.
