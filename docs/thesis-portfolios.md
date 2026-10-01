# Thesis portfolio backtests

This is the simulation boundary for the thesis-portfolio dev feature. It builds
on `wayfinder-jobs-v1`; it does not introduce a second execution engine or grant
permission to trade. The application, data acquisition and funding/execution
flows are not included in this SDK change.

## Contract

`wayfinder_paths.core.theses.models.Proposal` accepts exactly four independently
specified variants: $100, $1,000, $10,000 and $100,000. Allocated capital plus
cash must equal 10,000 basis points. Gross exposure cannot exceed 2× capital.
Spot and prediction shares are fully funded; only perps may be short or levered.
Prediction instruments identify the purchased outcome token; NO is not a short
of the YES token. Each position belongs to a stated thesis component.

Proposals contain intent, assumptions, evidence, counterarguments and
invalidation conditions, never executable transactions. Callers must resolve
instrument identities against their venue, including verifying prediction
outcomes, before constructing `MarketHistory`.

## Simulator bridge

```python
import pandas as pd

from wayfinder_paths.core.theses.backtest import backtest_variant
from wayfinder_paths.core.theses.models import Proposal

proposal = Proposal.model_validate(research_json)
result = backtest_variant(
    proposal.variants[0],
    histories_by_instrument_id,  # dict[str, MarketHistory], from trusted readers
    end=pd.Timestamp.now(tz="UTC"),
)
```

`backtest_variant` is synchronous. Use a bounded worker/process, not an async
event loop. The fixed `EntryAndHold` strategy runs through jobs-v1's
`simulate_execution`, strict trace validation, fee/funding accounting, bracket
logic and market events. Entry is decided after one completed bar and filled at
the next bar's open. A stop or liquidation never triggers re-entry.

Each position is a separately funded sleeve. Add sleeve equity to reserved cash
to obtain portfolio equity; do not deploy these results as cross-margin trades.
Entry fees, assumed slippage and routing costs fit inside the assigned capital.
Final positions are marked to market, without hypothetical exit fees.

## Data and limitations

- Request the preceding three calendar months; use only completed UTC hourly
  observations. Shorter common history is explicitly labeled partial. Internal
  gaps fail closed rather than being interpolated.
- Token/perp histories require real OHLC; perps also require hourly funding
  observations and an explicit maintenance-margin assumption. Missing funding
  is not zero funding.
- Prediction histories use observed midpoints, explicitly labeled as a fill
  proxy, never invented intrabar highs/lows. Protective brackets are prohibited.
  Settlement needs a verified timestamp and binary payout. A scheduled market
  end date, `closed` flag or today's timestamp is not settlement evidence.
- Cost, size/capacity and maintenance assumptions are supplied by the trusted
  caller and recorded with provenance. This module does not fetch historical
  order books or certify current executable liquidity.
- Intrabar liquidation is opt-in in the shared engine. It uses adverse OHLC
  extrema and assumes liquidation before a protective exit when ordering is
  unknowable; it wipes only that position's sleeve. Existing close-only engine
  consumers retain their previous behavior.
- The portfolio is designed using today's information and instrument universe.
  This is a hypothetical historical illustration, not out-of-sample evidence
  or a promise of returns. Do not rank proposals by the best backtest result.

The application must persist validated proposals immutably, distinguish missing
results from zero returns, and obtain fresh user approval before any funding or
execution. Neither research output nor a successful backtest is trade authority.
