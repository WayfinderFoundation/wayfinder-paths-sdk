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

`pattern_match_positioning_venue` provides the hosted execution reads: live
two-leg capacity, bounded prices and inventory attribution from confirmed fills.
It requires the account and builder approval to be set up already; it never
changes account mode, leverage or signing policy. Manual reductions consume
tracked inventory; a later manual addition does not recreate it. Missing funding
disables return-based triggers without preventing position/time-based cleanup.

The Hyperliquid adapter's `prepared_orders` signs and sends one persisted IOC
batch, with a signed expiry, fixed builder attribution and no hidden retries.
An unknown response stays unresolved until order status and complete fill
history agree. This is not an atomic two-leg order. The host must persist IDs
before sending, unwind incomplete entries, enforce authorization on each sign,
and monitor the basket independently of the browser. These primitives do not
enable a deployed trade endpoint or schedule by themselves.

## Native bracket execution boundary (in progress)

`submit_prepared_bracket` adds the original strategy's native `normalTpsl`
entry/TP/SL group to the same prepared-order boundary. It signs three persisted
client IDs, uses fixed-size reduce-only market exits, and supports core and HIP-3
perps without accepting spot or outcome IDs. Market identity, ticks, capacity and
current entry eligibility must still be resolved by the caller from metadata.
Persisted decimal terms that would change during wire encoding are rejected
before signing rather than silently rounded.

The submission is single-attempt, not an execution coordinator. A lost response
is uncertain, and an acknowledged entry does not prove both exits survived.
In particular, cancelling a partially filled IOC can cancel its child exits;
the host must reconcile the fill and each child before repairing protection or
closing the confirmed remaining position. The signed submission expiry does not
cancel active exits or implement the research's 24-hour time exit. Durable
backend recovery and timed-close integration for this strategy remain pending.

`reconcile_bracket_exit` uses the same order-identity and complete-fill checks
as IOC reconciliation. An open exit counts as protection only when its exact
trigger price, side, fixed size, market-trigger type and reduce-only flag match
the persisted plan. `unknownOid` is unresolved, not proof that an exit is absent;
`triggered` is not a final fill. Terminal trigger orders may be reported as
ordinary Market orders, so their fills are reconciled without requiring live
trigger metadata. The host must compare entry and exit receipts together before
showing a protected position or releasing a reservation.

`submit_prepared_cancels` signs a bounded, single-attempt cancellation for only
the persisted client IDs of one bracket. It never schedules account-wide
cancellation, and it shares order submission's exact signing/expiry boundary.
A cancellation acknowledgement is not a position-close receipt; subsequent
order/fill reconciliation is still required, including when a trigger races
the cancellation. These helpers do not enable or deploy the backend worker.
