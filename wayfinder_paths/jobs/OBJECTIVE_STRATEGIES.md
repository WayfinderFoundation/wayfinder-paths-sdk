# Objective strategies

Normal `freestyle_v1` strategy jobs: `tick(ctx)`, the existing runner, validation,
owner-reviewed proposals and authenticated scorecard sync. No second scheduler.

`wayfinder job objective-strategies` lists the four observation-first templates.
`wayfinder job create-objective risex-participation --account 0x…` creates one
paused, with zero spend. Validate and launch through the normal job lifecycle.
MCP equivalents: `objective_strategies` and `create_objective` (`starter_id`).

Generic authors use `create_freestyle` with `execution_params.objective_strategy`:
`primary` and optional `secondary` metrics (unit and maximize/minimize direction),
plus named `activities` containing a reviewed `capability` and `limits`.
`ObjectiveStrategy.model_json_schema()` is the complete authoring contract.
`ctx.participate("main", work=[…])` selects bounded work; it cannot change its
capability or cost ceilings through the call. Trades still use `ctx.act`.
Non-trading templates set `trading_enabled: false`, which refuses new trade
entries and custom calls while preserving exits. Hybrid jobs retain trading
risk requirements. Optional `limits.constraints` bound named adapter metrics
by unit and minimum/maximum; absent or stale measurements block new activity.

These initial protocol capabilities are **read-only, not executable pilots**:

| Protocol | Available | Blocks live participation |
| --- | --- | --- |
| FLOP | Readiness reporting | Deployed inference API, settlement and eligibility verification |
| RISEx | Mainnet identity, own points/history and fee reads | Signed orders, durable nonce recovery, fills, hedging and independent protection |
| PERPTools | Explicit readiness reporting | EVM/broker attribution plus execution verification |
| IMD | Deployment, owned-seat counts, earnings evidence | Worker isolation/enrollment and reward settlement verification |

Setting `enabled: true` does not bypass these blockers. Dry-run validation does
not call external services or claim simulated rewards; paper execution observes
but never submits. `RISEX_JWT` is runtime-only, not a job parameter. IMD accepts
`options.seat_id`; ownership is checked against `limits.account`.

Costs in receipts cover this job's submitted operations, not unrelated wallet
activity. Rewards come from protocol evidence, never from completed work counts.
Unknown values remain unknown; locked and pending allocations are not cash.
The recorded rule revision is a reviewed research version, not a live rule hash.

## Paths as optional extensions

Reviewed Paths can contribute an `activity` component with `path` and
`capabilities` in `wfpath.yaml`. The Python module exports
`build_activity_adapter(config, options)` implementing `ParticipationPort`.
Attach it at creation through `create_freestyle(..., path_dependencies=[
{"alias": "research", "slug": "my-path", "component": "main"}])` and set the
binding's `extension` to that alias. Installed code is copied into the strategy
workspace, hashed and included in proposal revisions. Updates to the installed
Path do not update a running strategy. Changed copies refuse to load.

Pins prove identity, **not safety or sandboxing**. Review executable code before
attachment. Initial extensions also remain observation-only; their own
`supports_submit` flag cannot enable live execution. Do not attach untrusted
worker code to a wallet-bearing process.

The activity ledger reserves cost before submission and reconciles uncertain
requests by stable operation ID. Corruption fails closed. Disabling activity
does not skip the protection hook, but stopping a job stops its ticks: trading
activation needs independent protection, not just this hook.

Monitoring is weekly plus material rule, eligibility, reward and risk changes.
Routine request transitions are journaled without creating another LLM wake.
Changes to limits, code and capabilities use the existing approval flow.
