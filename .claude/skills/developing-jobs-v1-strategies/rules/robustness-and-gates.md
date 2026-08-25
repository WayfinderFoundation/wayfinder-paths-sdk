# Robustness, gates, and funding

This page expands `jobs-research-contract-v1`. Load it when a strategy uses
perp funding, a regime/breadth filter, leverage, recent-event scenarios, or is
being considered for deployment.

## Observable gates

Name every decision gate `gate_*` in `precompute`, and have `decide()` consume
that exact column. This lets the execution trace report what the strategy knew
at each completed bar without rebuilding the condition elsewhere.

- A synchronized gate with one value across symbols is treated as a portfolio
  gate. Its active/inactive periods may receive conditional portfolio PnL and
  drawdown diagnostics.
- A symbol-specific gate receives activation and transition counts only.
  Portfolio PnL is not attributed to it because overlapping symbol states make
  that attribution ambiguous.
- Zero activations means the condition was not tested in that run. It is
  `gate_unobserved`, not evidence that the gate is safe.
- Report gate activations, transitions, and the gross/net notional entering an
  active state. A gate that improves returns only by eliminating nearly all
  exposure needs a larger or more appropriate test window.

For synchronized panels, compute breadth on aligned timestamps with an explicit
minimum asset count. Missing peers must not silently turn into bearish votes.

## Funding convention and coverage

Positive funding means longs pay shorts. For a position with direction `+1`
for long and `-1` for short:

```text
funding cashflow = -direction * size * reference_price * rate
```

The simulator applies funding at the first completed bar at or after the source
timestamp. Preserve the source timestamp in the event so delayed or coarse
alignment is auditable. Explicit venue-provided funding amounts remain the
source of truth when present.

Use `fetch_dataset(..., include_funding=true)` when candles and perp funding
come from the same venue. Per-symbol fetch failures should preserve the usable
history and list missing symbols; they must not discard the whole dataset.

Treat funding-dependent perp evidence as incomplete when coverage is materially
shorter than the candle span or required symbols are missing. The robustness
report uses `funding_incomplete`; do not describe such a run as fully
funding-adjusted.

## Robustness plan

Declare the plan in `execution_spec.json` under `robustness_plan`, or pass a
one-off plan to `core_jobs(action="robustness_check", ...)` / the matching CLI.
Use the smallest plan that tests the actual risks:

```json
{
  "neighbors": {"entry_threshold": [0.05, 0.10, 0.15]},
  "phase": {"param": "rebalance_phase", "values": [0, 1, 2, 3, 4, 5]},
  "leverage": [1, 2, 3, 4, 5],
  "walk_forward": {"train_bars": 1440, "test_bars": 360, "folds": 4},
  "scenarios": [
    {"name": "recent_7d", "lookback_days": 7, "role": "development"}
  ]
}
```

The check reuses existing execution-grid and walk-forward helpers. It stamps
the candidate revision, dataset hash, plan hash, and research-contract version;
only an exact completed match is reusable; partial lanes are retried. Results live under
`results/research/robustness/` and the candidate report links the latest valid
summary.

Before proposing deployment, read that summary and explicitly acknowledge each
material red warning by its exact code (`--ack-robustness-warning <code>` or
`robustness_warnings_acknowledged` in `core_jobs`). The approval gate rejects a
recommendation that carries an unacknowledged material warning.

Interpret warning codes as follows:

- `gate_unobserved`: at least one declared gate never activated.
- `funding_incomplete`: the funding-adjusted claim lacks the required coverage.
- `phase_sensitivity`: results depend heavily on rebalance/start offset.
- `isolated_neighbor`: the chosen threshold is not supported by nearby values.
- `oos_decay`: walk-forward performance materially decays out of sample.
- `leverage_risk`: leverage creates unacceptable drawdown/liquidation behavior.
- `scenario_loss`: a declared scenario exposes a material loss.
- `negative_paired_delta`: candidate underperforms the incumbent on paired data.
- `lane_failed` / `baseline_failed`: a requested lane or its reference run did
  not complete and the report is partial or failed.

Warnings are advisory in contract v1: they enrich the candidate report but do
not change or bypass the existing approval gate. Resolve material warnings or
explain their scope before a deploy recommendation.

## Development versus audit evidence

Label every scenario `development` or `audit` before running it.

- Use `development` for the recent squeeze/run-up, regime, or failure window
  that motivated the change.
- Use untouched chronological folds, a reserved holdout, or a predeclared
  audit scenario to decide whether the candidate generalizes.
- Never relabel a scenario after seeing its outcome.
- A favorable recent-run replay answers “would this have caught that move?” It
  does not by itself answer “will this work next time?”
