# Thesis portfolio backtests

This is the simulation boundary for the thesis-portfolio dev feature. It builds
on a selective extraction of `wayfinder-jobs-v1`; it does not grant
permission to trade. The application orchestration and funding/execution
flows remain outside this SDK module.

## Contract

`wayfinder_paths.core.theses.models.Proposal` accepts exactly four independently
specified variants: $100, $1,000, $10,000 and $100,000. Allocated capital plus
cash must equal 10,000 basis points. Gross exposure cannot exceed 2× capital.
The v1 model remains readable for historical partial-cash portfolios. New invested
targets additionally pass `validate_full_allocation`: positions allocate all
10,000 bps and cash is zero. Empty variants represent **Not constructed**, never
a successful cash recommendation. Capital/collateral is distinct from notional;
fees and gas are costs, not idle-cash sleeves.
Spot and prediction shares are fully funded; only perps may be short or levered.
Prediction instruments identify the purchased outcome token; NO is not a short
of the YES token. Each position belongs to a stated thesis component.

Proposals contain intent, assumptions, evidence, counterarguments and
invalidation conditions, never executable transactions. Callers must resolve
instrument identities against their venue, including verifying prediction
outcomes, before constructing `MarketHistory`.

Research produces portfolio loadings: verified assets, directions and weights,
supported by economic comparisons and historical diagnostics. Stop-loss and
take-profit fields remain optional for compatibility; new research leaves them
null. Publication does not require exit policies, sized execution quotes or
wallet/margin setup. Those belong to the subsequent execution-planning step,
not portfolio selection or review.

## Research progress and execution readiness

`research_thesis_checkpoint` validates a compact interpretation, entity-level
candidate ledger and optional provisional proposal. It returns only a receipt;
the existing OpenCode transcript stores the input. The parent alone has permission
to use it in the thesis workflow. Checkpoint assertions never enter
`RESEARCH_EVIDENCE_TOOLS` and cannot certify identity, source reads or quantification.

V3 researchers use `research_thesis_discovery`, whose input cannot contain a
portfolio or parent judgment. They record the broad discovery inbox plus up to
ten ranked `research_cases` per call, without a minimum count. Cases identify an
economic, narrative, mixed, hedge or event basis. The parent must assess every
ranked case and final holding; other leads remain explicitly unassessed.
Category membership is a lead, not a selection reason. Narrative cases require
dated attention/capital-flow or catalyst evidence, comparison and invalidation.

The harness's `thesis_notebook` tool reads only its runtime-derived parent/child
session tree and invokes the SDK's `research_notebook` projection. It returns a
paginated compact index or original cases by exact entity keys, preserving
implementations and provenance. It writes no second ledger or trace files.
Legacy v1/v2 checkpoint receipts remain readable with their original semantics.

New-target validation calls `validate_market_capacity(..., screen_capacity=False)`:
exact identity, duplicate-alias and outcome checks remain hard requirements.
Selected-pool reserves/turnover and compact order-book percentages are screening
heuristics, not whole-asset capacity or hard target weights. `execution_readiness`
reports their warnings separately at each budget, including capital and notional.
Every invested target remains execution-pending, even when its screen passes.
Fresh size-specific route/quote, costs, eligibility, wallet and approval checks
are still required. This report **never** authorizes execution. Legacy callers
retain conservative screening by default; no signing boundary is relaxed.

Discovery uses existing tools: DeFiLlama protocol search now filters by category
before pagination and retains descriptions/provider IDs; Hyperliquid browsing
pages each filtered/ranked bucket with `offset`. No new catalog service is added.

## Lookup IDs and pre-publication quantification

Research can use a successfully resolved chain-scoped lookup ID, such as
`aerodrome-finance-base`, without copying a contract address. The resolver returns
`lookup_id` alongside the resolved `token_id`; observed evidence joins both to
the same identity. Protected native/stablecoin registry membership and literal
contract text on a project website are not general-project admission requirements.
Explicitly suspicious identities still fail. Economic claims still require a
relevant source read, and aliases cannot split one holding to bypass capacity.
`onchain_list_tokens(token_id=..., chain_code=...)` resolves the pool address
internally, including registered native/wrapped pricing proxies.

Before final publication, `research_quantify_portfolio(variants=...)` reads daily
spot/perp/outcome history and seven days of perp funding using existing clients.
It fetches each finalist once across budgets, with four reads in flight at most,
then returns per-asset and fixed-initial-notional portfolio diagnostics: observed
returns/drawdowns, daily volatility, timestamp-aligned correlations, cash and
gross exposure, funding coverage/cost and prediction entry payoffs. Submit one
variant per distinct allocation; the backend checks a deterministic allocation
key so changed weights must be measured again. Missing history is reported, not
fabricated or treated as zero risk. No forecast or historical-return optimizer
is introduced.

Diagnostics use up to 90 completed UTC days and require 14 overlapping daily
returns for volatility/correlation. Gaps are not filled; coverage and staleness
are explicit. Gross price diagnostics exclude fees, funding, stops and liquidation
and are **not** the execution-aware simulation below. Prediction book summaries
report break-even probability and winning/losing returns at the best ask, before
costs—not an independent probability forecast or a sized fill guarantee. NO uses
its own outcome price history, not a negated YES series.

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
event loop. The fixed `EntryAndHold` strategy runs through the extracted
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

## Extraction provenance

This branch starts at SDK main `3c31c14c6c4b0fb4044c9e77926c84d07fff0ecc`.
`core/backtesting/execution` selectively ports the primitives, ledger, engine,
simulator, venue contracts, purity checks and trace validation from jobs-v1
`11bc2848`, with the thesis fixes in `353c92b7`. It has no jobs imports,
scheduler, workspace persistence, evolution, live venue adapters, dynamic
script loading, grid optimization, defense overlays or regime classifiers.
Pass an in-process trusted strategy factory and prepared history. The purity
checks catch accidental nondeterminism; they are not a security sandbox for
untrusted Python. Run simulations in a bounded dedicated process.

The Hyperliquid prepared-order/preflight/receipt helpers are preserved from
backend's existing SDK pin `2a03a08f2e7c1d5f8d9da709155dc81a4e06d352`.
Only that pin's Hyperliquid adapter changes are carried across, not its other
branch history. This keeps the backend's existing signal-trade imports intact.
