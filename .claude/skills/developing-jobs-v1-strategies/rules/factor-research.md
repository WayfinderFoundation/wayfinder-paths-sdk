# Factor research in jobs_v1

Use this page when the hypothesis ranks a multi-asset universe with continuous
scores. A factor is an ordering hypothesis, not a strategy or a binary trigger.
Keep cross-sectional factors distinct from time-series signals on one asset.

## Build a causal panel

Start with a liquid, explicitly declared universe and benchmark. Prefer at
least eight synchronized assets and enough history to include several regimes;
record listing dates, missing bars, venue, cadence, and whether the universe is
current-membership (survivorship biased). The last in-progress candle is never
a feature row.

Reuse `wayfinder_paths.jobs.factors`:

- `cross_sectional_rank`: symmetric `[-1, 1]` ranks with eligibility masks.
- `cross_sectional_robust_zscore`: timestamp-local median/MAD normalization.
- `rolling_beta` and `residual_return`: benchmark-relative transforms.
- `blend_factor_scores`: aligned, normalized blends with optional final rank.

Reuse `trailing_return`, `realized_volatility`, and `panel_breadth` from
`wayfinder_paths.jobs.indicators`. Do not add local copies of these transforms.
Compute panels once in `precompute(frames)` and attach each score back to its
symbol frame. `decide(ctx)` should only read the latest synchronized scores,
apply cadence/risk gates, and emit target-weight intents.

Useful starting families are residual momentum/reversal, funding carry,
participation (price move confirmed by relative volume), catch-up, and low
residual volatility. Positive perp funding means longs pay shorts, so a pure
carry score is normally the negative of smoothed funding. Funding missingness
is not zero carry.

## Treat the blend as one declared test family

Define a small set of economically distinct single-factor controls and blends
before looking at forward returns. Run:

```text
wayfinder job factor-scan <id> --columns factor_momentum,factor_carry,blend_core
```

The scan evaluates completed-bar scores from the next open, applies Newey-West
standard errors, pools every column x horizon under Benjamini-Hochberg, checks
four chronological fold signs, and leaves the final 15% unopened. A negative IC
is an observed orientation, not permission to flip the sign after seeing the
tail. Freeze at most three candidates, then spend the reserved tail once with
`factor-holdout` using the scan's column, horizon, and orientation.

Rank IC is only admission evidence. It says the score orders future relative
returns; it does not show that a tradable tail clears turnover, fees, slippage,
funding, borrow, or capacity. After a tail confirmation, build the minimal
basket and use the exact jobs_v1 simulator plus walk-forward and robustness
checks. Keep single-factor controls beside the blend so attribution stays
legible.

## Regimes are exposure controls

Predeclare causal states from benchmark trend/volatility, breadth, dispersion,
and median benchmark correlation. Name every consumed condition `gate_*` so the
trace audits the exact gate. Prefer scaling gross/net exposure or selecting a
predeclared sleeve inside a regime; do not mine a different factor sign in each
cell. A regime with no observed activations is unvalidated.

Report results by regime and fold, but select on the training prefix. A recent
run-up used to invent a gate is development evidence, not a holdout. If the
frozen tail rejects the factor family, close it: do not tune weights or regimes
against the opened tail. Shipping no new starter is better than cataloging an
unstable backtest.
