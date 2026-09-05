# Strategy search — a plan toward the best strategy, not a tighter loop around the first idea

The failure mode this file exists to prevent, observed live: six parameter
variants of a signal that never had predictive power, exit behavior changed on
a 12-trade bucket, and no structural alternative ever tried. Disciplined
process, no edge. The fix is a **search plan**: validate the signal before
building, go broad across structurally different ideas before going deep on
one, and let the validation ladder — not enthusiasm — decide what survives.

## 0. Signal first — no backtest until the signal beats drift

Any new idea starts with a signal scan, not a strategy build:

```
wayfinder job signal-scan <id> --timeframes 1h,4h,1d    # the WHOLE library, one call
# MCP: core_jobs(action="signal_scan", job_id=…, timeframes=["1h","4h","1d"])
wayfinder job signal-check <id> --column entry_signal --direction short|long|auto
# MCP: core_jobs(action="signal_check", job_id=…, column=…, direction=…)
wayfinder job holdout-check <id> --signal new_low_5 --horizon 24 --direction short
# MCP: core_jobs(action="holdout_check", job_id=…, signal=…, horizon=…, direction=…)
```

`signal-scan` event-studies the entire canonical trigger library (momentum,
breakout, MA-cross, RSI/BB fades, volatility, exhaustion — BOTH directions)
across the requested timeframes in one call. Its discipline is built in, not
optional:

1. **Fingerprint first.** The report opens with a dataset fingerprint — read
   it before any candidate row. `cost_to_range > ~0.3` kills short-horizon
   families outright; `acf1`/variance ratios say whether reversal or
   continuation families are even plausible (a VR rejection of a random walk
   is a routing hint, not proof of mean reversion); a dominant regime
   quadrant means any edge found is regime-conditional.
2. **The holdout is reserved before anything is measured.** The scan never
   sees the final 15% of history; the cutoff timestamp is in the report.
   NOBODY looks at that tail until a candidate is frozen — then
   `holdout-check` spends the ONE pre-registered confirmation (signal +
   timeframe + horizon + direction; directional t >= 1). The trial ledger
   remembers a spent tail; a second look is data snooping.
3. **Scan wide, promote narrow.** PROMOTE = BH `q <= 0.10` AND sign
   agreement in >= 3 of 4 chronological folds — not raw `t >= 2` (with 100+
   tests, ~5 lucky |t|>=2 passes are EXPECTED; the q-value already accounts
   for that). Take at most 3 promoted cards forward: CORE (best q, closest
   to the user's thesis), ADJACENT (same mechanism, different timeframe or
   horizon), DIVERGENT (a different family) — and record them in the job's
   `candidates` ledger with exactly those buckets.
4. **Minimal viable strategy first.** Build the CORE card as: the exact
   scanned entry, a fixed-time exit at the measured horizon, the protective
   stop, nothing else. Judge THAT by backtest before any exit engineering.
   Then let the card's `path_stats` pick the exit family:
   | Path shape | Exit family |
   |---|---|
   | MFE arrives early (`bars_to_mfe <= horizon/3`) and edge decays at longer horizons | target exit |
   | MFE builds to the horizon | fixed-time or trailing |
   | `mfe_mae_ratio < ~1.25` | time-stop only — a tight stop eats the edge |
5. **Every scan is a recorded trial.** The scan appends every executed test
   to `results/research/signal_scan/ledger.jsonl` and reports cumulative
   counts — cite them ("3rd scan of this workspace, 400 tests to date").
   Never re-test a dead candidate under a new name; the ledger hash catches
   it.

Because triggers are events with signed t-stats, a "failed short" trigger
with `t >= +2` surfaces as a LONG candidate instead of a dead end.
Hand-write `signal-check` columns only for triggers genuinely outside the
library — a pre-specified user signal is ONE trial and keeps the simpler
standard: signed `|t| >= 2` (declare `--direction`; a genuine short edge has
NEGATIVE forward returns) plus a holdout confirmation.

The check event-studies a column: forward returns
after signal fires vs the **unconditional drift** of the same series — a
random entry at the same frequency earns the drift for free, so the signal
must beat it, not zero. Events are decimated to horizon spacing (overlapping
forward windows would count one selloff as dozens of samples — `n` is the
honest count, `n_raw` the trigger-bar count). Read the per-horizon verdicts:
an edge is `|t| >= 2` in the declared direction
with `n >= 30`. **If no horizon beats drift, the entry has no predictive power
— change the idea, not the parameters.** No exit engineering, sizing, or
filter tuning rescues entries that carry no information. This 30-second check
is what converts "six variants of a dead idea" into one honest sentence.

**Signal vs system — what a failed signal-check does and does not mean.** The
check tests the TRIGGER in isolation. A complete trade *system* — entry gate +
minimum hold + asymmetric exits + re-arm + stop — can be profitable even when
its raw trigger never beats drift, because the geometry (where it's allowed to
enter, how it exits each side) does the work the trigger doesn't. This is not
hypothetical: the live IMX momentum short's NewLow5 trigger fails signal-check
on the full history while the full system backtests profitably across windows.
So: for NEW ideas, the gate stands — don't build on a dead trigger. For a
fully-specified system (a live bot, a shipped reference strategy, a complete
rule set the user hands you), a failed signal-check means "no standalone
timing alpha" — report that, then judge the SYSTEM by full backtest +
walk-forward, never by the trigger alone.

For basket / cross-sectional ideas the equivalent test is
`wayfinder job rank-check <id> --column <ranking>` — the Spearman rank IC of
the ranking column vs relative forward returns (|t| >= 2, n >= 30, sign
stable across both halves). A ranking that does not order future returns
cannot be rescued by rebalance cadence or weighting.

For any pair / long-short idea, the equivalent step 0 is the
admission gate — `wayfinder job pair-check <id> --symbols A,B --days 720`
(see `rules/pairs-and-baskets.md`). A REJECT is the methodology working, not
a failure: it just saved days of tuning a spread that does not mean-revert.

Multiple-testing honesty: if you signal-check many candidate columns, expect
~1 in 20 to clear t≈2 by luck. Scan wide, but demand the q-gate and fold
stability before building on a marginal pass — `signal-scan` computes both.

**Composition (workspace signals).** When the canonical library exhausts on a
series, compose — but through the scan, never around it. Declare up to 12
`SignalDef`s in `workspace/src/signals.py` (indicator combos, feature-
conditioned triggers, session-windowed variants) and rerun `signal-scan`:
they join the canonical sweep under ONE pooled BH family with the same
decimation, folds, and reserved holdout, and `holdout-check` confirms them by
name. The discipline that makes this expansion honest:

- **Hypothesis-driven only.** Every def cites its reason for existing — a
  fingerprint quadrant (e.g. vr24 continuation), a `path_stats` shape, or a
  §2b failure-table row. Blind permutation of indicators is not research;
  it inflates the family and buries the real candidates.
- **Breadth costs power.** Every added def raises the promote bar for the
  WHOLE scan — the q-gate divides by the family size. Twelve strong
  hypotheses beat fifty permutations arithmetically, not just aesthetically.
- **No serial one-off `signal-check` mining.** A loop of single checks on
  hand-rolled columns has no multiple-testing control; that is p-hacking
  with extra steps. `signal-check` is for ONE declared custom entry.
- **The ledger sees the code.** Scans record the signals-file sha; renaming
  a def to relaunch it shows up in audit as the same code. Holdout stays
  one-shot per candidate regardless of provenance.
- **New information beats new transforms.** Funding (`job fetch-funding`),
  session structure (canonical session triggers), and cross-asset context
  (the multi-symbol view; `rank-check` for continuous rankings) add signal
  the closes alone cannot carry — reach for a new data axis before the
  fifteenth reshuffle of the same prices.
Deferred rigor (recorded, deliberately not built yet): HAC/bootstrap standard
errors, Deflated Sharpe / PBO overfit stats, BTC/ETH market-relative
controls. If a result only matters under those corrections, it was too
marginal to trade anyway.

## 0b. Replicating a live / known strategy — port, don't transcribe

When the user references a strategy that already exists ("there's an X bot
that works"), the workflow is different from idea mining, and getting it
wrong inverts conclusions. Observed live: a pasted prose spec had its SMA50
gap filter written BACKWARDS vs the running bot, and every backtest that
followed tested the opposite entry universe — "everything loses" while the
real bot made money.

1. **Check the shipped library first** — `wayfinder job strategy-library`
   (MCP: `core_jobs(action="strategy_library")`) lists verbatim ports of
   audited live scripts with import lines and default params. If the strategy
   is there, the workspace script is ONE line:
   `from wayfinder_paths.jobs.strategies.<module> import build_strategy`.
   Never re-implement from memory or prose what already exists as code.
2. **Prose specs are lossy.** On ANY ambiguity or internal contradiction
   (a threshold quoted two ways, a filter whose direction is unclear), do not
   pick an interpretation and move on — implement BOTH readings and backtest
   both. The wrong reading usually reveals itself immediately.
3. **Fingerprint parity before conclusions.** Before trusting any result,
   compare the simulated trade fingerprint — trades per week, typical hold,
   win rate, exit-reason mix — against how the live strategy is described or
   observed to behave. A replication that trades 5x less often, or holds 10x
   longer, is not the same strategy, whatever the spec says.
4. **0-for-N on a known-working system means YOUR code is wrong.** If the
   user says it works live and your replication loses every trade, the prior
   is an implementation or transcription bug — find it (or diff against the
   library port) before reporting that the strategy has no edge.

## 1. Breadth before depth

Before building anything, sketch **2–3 structurally different candidates** for
the user's goal — different families, not different parameters:

- trend/momentum (daily or 4h bars, vol-targeted)
- carry (funding-rank basket, funding-spread pair)
- mean reversion (ONLY with a passed admission gate)
- cross-sectional (rank a universe, trade the extremes)

One `signal-scan` covers the trigger families for the job's symbols in a
single call (canonical + workspace defs); signal-check a single custom
sketch it doesn't cover, `rank-check` a cross-sectional ranking over the
job's multi-symbol view. The `positioning` family (funding/open-interest
divergence, liquidation flush) only measures on jobs that declare the
`funding` and `open_interest` feeds in `data_contract.features`; elsewhere
the scan lists it as `missing_feed` rather than dead. The same indicators
are chart specs (`fundz:N`, `oichg:N`, `funddiv:Z:R`, `flush:R:O`) and
`wayfinder_paths.jobs.indicators` functions a `precompute()` can call
directly. Build only
the most promising. One structural idea
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
  grid, that VERSION is dead. Say so, with the numbers, and evolve (below) —
  do not stop to present a menu.

## 2b. Idea evolution — carry the user's seed to a working strategy

The user's idea is a seed, not a spec. A failed validation is not a terminal
verdict — **the evidence names the successor**, and testing that successor is
your job, not the user's homework:

| Failure evidence | Natural successor |
|---|---|
| Stop-outs dominate losses, entries fine | regime filter (trade only with the trend) |
| Entry never beats drift on any horizon | different trigger or timeframe for the same thesis (`signal-scan --timeframes 1h,4h,1d` shows every library trigger at once) |
| Signal has edge in the WRONG direction (t significant, sign flipped) | invert the trade — a failed short entry that predicts bounces is a long entry; test it, don't discard it |
| Pair gate REJECTs (no cointegration) | funding-spread pair on the SAME assets, or momentum on the same assets |
| One leg carries the losses | hedge-ratio sizing; widen the universe |
| Edge exists but decays OOS | slower bars, longer formation window, fewer rebalances |
| Shorts drag in a narrow universe | widen the universe so "weakest" means something |
| Canonical scan exhausted (0 promotions across symbols/timeframes) | composition lane: hypothesis-driven workspace signals (feature-conditioned, session-windowed) swept by the next scan |
| Composed family ALSO exhausted | new data axis (funding, sessions, cross-asset rank) — or accept "no standalone timing alpha" and let the SYSTEM (gates + exits + regime) carry it; "signal mining exhausted" is terminal only after both families fail |

Rules of evolution: preserve the user's *intent* (their assets, their thesis
direction, their risk appetite) while changing the *mechanism*; one structural
change per step; every successor goes through the same ladder. Spend the
depth budget (~4 structural steps) before returning — coming back after one
dead variant with a list of suggestions is stopping too early. Return with
either a validated candidate or the honest map of everything tried.

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

Stop and present the evidence only when the evolution budget is exhausted:
~4 structural successors tried on the same goal, each validated and dead.
Then show the map — what was tried, what each signal check / backtest showed,
and which documented-edge families remain untried (table below). Recommending
a different direction with numbers after a spent budget is a SUCCESS outcome.
Two failure modes bound it on either side: tuning parameters on a dead signal
(churn), and returning to the user after a single dead variant with a menu
(stopping too early).

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
