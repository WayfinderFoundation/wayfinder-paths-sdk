# Pattern Match research core

The pure modules in `wayfinder_paths.quant.pattern_match_universe` and
`pattern_match_outcomes` are shared with the backend's centrally scheduled
Pattern Match harness. They accept hydrated market data and do not import the
retired jobs runtime, call providers, schedule jobs or place orders.

The scorer retains the shelved research's 15-minute OHLC/funding/premium policy
and frozen calibration. Optional chart paths do not affect scoring. The caller
owns cadence, data freshness, volume eligibility, durable lane history and
publication provenance.

Outcomes use next-bar open, stop-first ambiguous candles, symmetric research
brackets, a 96-bar horizon, frozen round-trip cost and signed funding payments.
Missing price/funding coverage stays unresolved. These are modeled returns,
not actual account PnL or gap-aware execution fills.

Regression fixtures lock the original 139 selected trade identities (87 wins),
two raw 10,000-bar BTC forecasts, lane causality, selector equivalence and
outcome/funding behavior. The fixture manifest records source hashes.
Original fold 3 overlaps calibration development: the 62.59% win rate is a
retrospective regression reference, not a clean out-of-sample claim.

This PR extracts only the reusable research core from #738 onto current main.
It does not restore that PR's SDK job, runner, CLI or execution changes and
does not bump the package version.
