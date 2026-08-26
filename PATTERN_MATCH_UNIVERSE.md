# Pattern Match liquid-universe job

This package provides a shadow-first `jobs_v1` strategy that evaluates every
currently selected Hyperliquid native and HIP-3 perpetual market every 15
minutes. Calibrated markets receive a model forecast; the rest receive an
explicit `uncalibrated` abstention without an unnecessary 10,000-bar fetch.
New shadow candidates are admitted only on the hourly phase used by the frozen
research protocol. Real orders are disabled by default.

## Create the job

```bash
wayfinder job create-pattern-universe
```

The initializer discovers current, non-delisted native and HIP-3 perps above
the default $5 million 24-hour notional-volume floor, snapshots that symbol
list into the strategy revision, creates a paper job, and compiles it. Rerun
the initializer under a new job ID to review a changed listing universe; the
running revision never adds markets silently.

Useful options:

```bash
wayfinder job create-pattern-universe \
  --job-id pattern-match-universe-15m \
  --minimum-volume-usd 5000000 \
  --agent-mode intervene \
  --no-compile
```

## Runtime contract

- Completed 15-minute OHLCV bars only, with next-bar-open fills.
- A bounded 10,000-bar model history per market, with 12 extra fetch bars to
  absorb endpoint-boundary and forming-candle omissions.
- Price-shape windows of 12, 24, 48, and 96 bars, with funding and premium
  context used to rerank analogues.
- A decision record for every selected market every 15 minutes. Calibrated
  markets receive model scores; other markets remain visible as abstentions.
  Only the validated hourly phase can create a candidate, shadow position, or
  order proposal.
- Development-fold timing, vote, and steepness thresholds for 33 markets.
  Other liquid markets remain visible as `uncalibrated` abstentions.
- A causal market-by-direction feedback gate: at least 50 resolved shadow
  outcomes and a positive mean over the latest 10 net outcomes available at
  decision time.
- Symmetric stop and take distances, each equal to half of the analogue
  neighbors' median forecast 3-hour high/low range. The maximum hold is 96
  bars (24 hours), and same-bar stop/take collisions resolve to the stop.
- Missing price, funding, premium, calibration, or lane evidence fails closed.
  A shadow outcome with incomplete funding remains observable but cannot
  update the feedback gate.

The Wayfinder Hyperliquid funding API must return both `fundingRate` and
`premium`. Deploy the backend schema/cache support for `premium` before this
job; an older rate-only response produces `incomplete_funding` abstentions and
cannot place an order.

The durable strategy state is stored below
`strategy_state.pattern_match_universe_v1`. `latest` contains one score per
market, `pending_shadows` contains unresolved counterfactual positions,
`recent_resolutions` contains the latest 200 outcomes, and `lane_history`
contains the evidence used by the causal gate. This gives the agent the inputs
needed to diagnose an abstention or propose a revision without treating its
own report as authoritative execution approval.

## Evidence and limitations

The unrestricted broad-universe expansion was negative: 1,071 trades at
-6.53 bps per trade with profit factor 0.873. The frozen causal feedback gate
selected 139 trades across evaluation folds 3-5 at +7.33 bps per trade and
profit factor 1.186. The asset-week clustered 95% bootstrap interval was
-5.14 to +18.74 bps, so the positive result is promising but not statistically
conclusive.

A later Hydromancer holdout produced nine unrestricted shadow candidates (7
wins, 2 losses, +33.65 bps per trade); the frozen feedback gate selected zero.
That is evidence that the production gate abstains conservatively, not proof
of independent profitability.

Keep the job in paper/shadow mode until it has accumulated enough genuinely
forward outcomes. Enabling `execution_params.allow_orders` is an explicit
reviewed revision and still cannot bypass the calibration, liquidity,
confidence, feedback, position-count, or bracket gates.
