# Strategy search — a plan toward the best strategy, not a tighter loop around the first idea

The failure mode this file exists to prevent, observed live: six parameter
variants of a signal that never had predictive power, exit behavior changed on
a 12-trade bucket, and no structural alternative ever tried. Disciplined
process, no edge. The fix is a **search plan**: validate the signal before
building, go broad across structurally different ideas before going deep on
one, and let the validation ladder — not enthusiasm — decide what survives.

## 0. Signal first — no backtest until the signal beats drift

Any new idea starts with a signal check, not a strategy build:

```
wayfinder job signal-check <id> --column entry_signal [--horizons 4,24,48]
# MCP: core_jobs(action="signal_check", job_id=…, column="entry_signal")
```

Write the entry condition as a boolean column in `precompute()` (you need that
column for `decide()` anyway), and the check event-studies it: forward returns
after signal fires vs the **unconditional drift** of the same series — a
random entry at the same frequency earns the drift for free, so the signal
must beat it, not zero. Read the per-horizon verdicts: an edge is `t >= 2`
with `n >= 30`. **If no horizon beats drift, the entry has no predictive power
— change the idea, not the parameters.** No exit engineering, sizing, or
filter tuning rescues entries that carry no information. This 30-second check
is what converts "six variants of a dead idea" into one honest sentence.

For any pair / long-short / basket idea, the equivalent step 0 is the
admission gate — `wayfinder job pair-check <id> --symbols A,B --days 720`
(see `rules/pairs-and-baskets.md`). A REJECT is the methodology working, not
a failure: it just saved days of tuning a spread that does not mean-revert.

Multiple-testing honesty: if you signal-check many candidate columns, expect
~1 in 20 to clear t≈2 by luck. Scan wide, but demand stability (does it hold
on both halves of the data?) before building on a marginal pass.

## 1. Breadth before depth

Before building anything, sketch **2–3 structurally different candidates** for
the user's goal — different families, not different parameters:

- trend/momentum (daily or 4h bars, vol-targeted)
- carry (funding-rank basket, funding-spread pair)
- mean reversion (ONLY with a passed admission gate)
- cross-sectional (rank a universe, trade the extremes)

Signal-check all sketches; build only the most promising. One structural idea
failing does not mean "tune it harder" — it means try the next branch. The
best strategy for the user is usually in a different family than the first
idea, and you only find it by looking.

## 2. Depth budget — when to stop digging

- Max **~4 structural iterations** per idea (entry rule, filter, exit logic —
  not parameter values).
- Parameters are tuned ONLY via one `experiments` grid + walk-forward pass —
  never by sequential single edits scored against the quick window. Prefer
  parameter **plateaus**: the grid summary carries a `plateau` block
  (neighbor-robustness of the top cell); a `plateau_score < 0.5` means the
  top cell is a lone spike — likely noise — so pick from the best stable
  region instead.
- If the signal check never beats drift, or PF never crosses 1.0 after the
  grid, the idea is dead. Say so, with the numbers, and move to the next
  branch.

## 3. The validation ladder (THE protocol — every rung, in order)

```
signal-check  →  backtest --quick  →  backtest-diagnose  →
full-history backtest  →  walk-forward OOS (experiments --wf-*)  →  promote
```

A change is kept only when it improves the quick window AND the full history
(or walk-forward). A change that wins on the quick window but loses on
full/OOS is **rejected** — this exact case occurred live (a PF 1.06 "win"
that was a window artifact). Diagnose `recommendations` are hypotheses ranked
by evidence, not edicts: the payload's `how_to_use` says the same.

## 4. Risk-control invariant

Stops and time-stops are **never removed** to improve a backtest. They may be
retuned (widened, tightened) only through the validation ladder — grid +
walk-forward, kept only if OOS agrees. "The stop bucket lost money" on a
small sample is a hypothesis about stop placement, not evidence the stop
should go. The diagnose engine's bucket rules gate on n ≥ 8 and ≥ 25% of
closes for the same reason — below that, a bucket is noise.

## 5. Stop conditions and honest exits

After 2–3 dead structural ideas on the same goal, stop and present the
evidence: what was tried, what each signal check / backtest showed, and which
documented-edge families remain untried (table below). Recommending a
different direction with numbers is a SUCCESS outcome. Continuing to tune a
dead idea is the failure outcome.

## 6. What "good" looks like (realistic expectations per family)

| Family | Bars | Trades | Cost sensitivity | Plausible post-cost | Min data |
|---|---|---|---|---|---|
| Funding-carry basket | 1h–8h rebal daily/weekly | many small | low (rebal only) | Sharpe 1–2, steady | 1y+ funding |
| Cross-sectional momentum | 1d, weekly rebal | dozens/yr | medium | Sharpe 0.5–1.5 | 2y+, liquid top-40 |
| Time-series trend (vol-targeted) | 1d | tens/yr | low | Sharpe 0.5–1, lumpy | 2–3y |
| Funding-spread convergence pair | 1h–8h | tens/yr | medium | Sharpe 1–2 + carry | 1y+ funding both legs |
| Price stat-arb pair (gate-passed) | 1h–1d | ~15+ crossings/90d | HIGH (2 legs × 2 sides) | often ≤ 0 after costs on majors | 2y+, multiple regimes |

Calibration, not gospel: chase neither PF 3.0 on hourly majors (not real) nor
accept PF 1.02 on 20 trades (not evidence). Hourly mean reversion on major
pairs predictably fails after costs — the definitive crypto pairs study finds
intraday reversion exists but underperforms once both-leg round-trip costs are
paid. Fetch 2–3 years of data first (`fetch-dataset --days 720 --source ccxt
--exchange binance`); 180 days is one regime and cannot validate anything
cyclical.

## 7. Experiment ledger discipline

Every `experiments` run is recorded — `wayfinder job experiments <id>` with no
grid lists them (MCP: `core_jobs(action="experiments", job_id=…)` without a
`grid`). Check it before testing a
configuration: re-running a dead config because the session forgot it is pure
waste. When presenting results to the user, cite the ledger ("tried 3 grids
across 2 ideas; best OOS decay_ratio 0.4 — fit to noise") instead of vibes.
