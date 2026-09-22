# Pattern Match research core

The pure modules in `wayfinder_paths.quant.pattern_match_universe` and
`pattern_match_outcomes` are shared with the backend's centrally scheduled
Pattern Match harness. They accept hydrated market data and do not import the
retired jobs runtime, call providers, schedule jobs or place orders.

The scorer retains the shelved research's 15-minute OHLC/funding/premium policy
and frozen calibration. Optional chart paths do not affect scoring. The caller
owns cadence, data freshness, volume eligibility, durable lane history and
publication provenance.

The calibration contains thresholds and costs only, not the old hourly lane
seeds. A new backend cadence must bootstrap its own chronological evidence.

Outcomes use next-bar open, stop-first ambiguous candles, symmetric research
brackets, a 96-bar horizon, frozen round-trip cost and signed funding payments.
Missing price/funding coverage stays unresolved. These are modeled returns,
not actual account PnL or gap-aware execution fills.

Regression fixtures lock the original 139 selected trade identities (87 wins),
two raw 10,000-bar BTC forecasts, lane causality, selector equivalence and
outcome/funding behavior. The fixture manifest records source hashes.
Original fold 3 overlaps calibration development: the 62.59% win rate is a
retrospective regression reference, not a clean out-of-sample claim.

The extraction does not restore #738's SDK job, runner or CLI and does not bump
the package version.

## Positioning trade support (in progress)

`pattern_match_positioning_trade` sizes a user-selected gross-notional budget
across the asset and BTC with the same signed weights as the outcome resolver.
It reuses the Hyperliquid adapter's size rounding and minimum-order constant.
Negative beta puts both legs in the same direction; exactly zero beta omits BTC.
A nonzero hedge below the minimum after rounding rejects the whole plan rather
than silently turning it into an outright trade.

Its pure basket-exit decision accepts user-selected stop/take return fractions
on gross entry notional, not margin. A time limit or a reduced leg requests a
whole-basket exit even when PnL is unavailable. These are execution helpers, not
an order coordinator: they neither submit orders nor run while a browser is
closed. The backend must own authorization, durable orders/fills, retries and
the exit worker. The original positioning research remains a fixed 24-hour
markout; adding protective exits does not inherit its reported performance.
