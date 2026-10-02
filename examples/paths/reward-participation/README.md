# Reward participation (read-only preview)

An installed `path_v1` component, using the existing job runner, owner-approved
parameter proposals, activities and snapshots. Defaults: FLOP readiness, zero
spend, 15-minute deterministic ticks, weekly agent review. Nothing is published,
launched, registered, funded or traded by installing this source example.

The shared `paths.participation` tick supports owner-reviewed work items, cost
reservation before submission, durable operation IDs, reconciliation, and a
separate protection hook that runs even when reward activity is disabled.
Fixtures cover inference and trading-shaped requests; **no live submit adapter
is shipped**. Do not use this monitor to manage existing trading positions.

| Program | Implemented | Remaining before execution |
| --- | --- | --- |
| FLOP | Explicit readiness blocker; receipt/restart/budget workflow fixtures | Deployed API, model inventory, faucet, settlement, eligibility and acceptance contract |
| RISEx | Mainnet identity check, account-scoped points/history and actual fees | EIP-712 signer/nonce recovery, fill-driven Hyperliquid hedge, TP/SL and margin watchdog |
| PERPTools | Explicit attribution/readiness blocker | Arbitrum Orderly broker and EVM-to-points mapping; trading adapter and hedge recovery |
| IMD | Deployment, owned-seat work counts and earnings-page evidence | Earnings settlement schema; any worker execution remains a separate isolated integration |

Configure `params.participation` in `wfpath.yaml` before building, or use a
`params_update` proposal on an installed job. Replace the **whole** participation
object when proposing nested changes. Account examples are in the adapter's
`examples.json`; never put credentials in params. RISEx reads use `RISEX_JWT`
from the process environment. IMD optionally uses top-level `imd_seat_id` plus
`participation.account`. Switching account/program/unit requires a new job,
not clearing the old job's reconciliation ledger. Rule revisions are a reviewed
research version, not an automatically discovered protocol rule hash.

The example starts with `enabled: false`, but setting it true cannot enable a
write adapter. Dry-run never signs/submits or mutates the live operation ledger.
Protocol observations can still be read in dry-run; they are not simulated rewards.
Keep durable state backed up; deleting a ledger destroys idempotency evidence.
`cost_paid` covers this Path's operation ledger, not unrelated account activity.
RISEx point totals are explicitly lifetime totals, not estimated season earnings.
`enabled: false` pauses reward-generating work while the protection hook runs;
stopping the job stops its ticks entirely. A live trading rollout needs an
independent watchdog for runner failure and manual job halts.

Build/check locally:

```sh
wayfinder path doctor --check --path examples/paths/reward-participation
wayfinder path render-skill --path examples/paths/reward-participation
pytest wayfinder_paths/tests/test_path_participation.py wayfinder_paths/tests/test_participation_clients.py
```

Before a funded pilot: implement and independently test protocol writes, account
identity, expiring least-privilege credentials, all exposure/margin/loss/unhedged
duration limits, protective-exit recovery and privacy/resource controls. Obtain
owner approval separately. Observe two actual distributions before broadening;
this verifies accounting, not future airdrop value or ROI.

Primary sources reviewed 2026-10-01:
[FLOP agent draft](https://flop.finance/intro/agent/),
[RISEx API](https://developer.rise.trade/reference/general-information),
[PERPTools points](https://docs.perptools.ai/docs/points/overview),
[IMD API](https://imd.fun/docs/).
