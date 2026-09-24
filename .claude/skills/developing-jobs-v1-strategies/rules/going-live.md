# Going live — funding and launching a jobs_v1 strategy

The complete last mile, in order. A live session burned ~25 tool calls
reverse-engineering this from adapter source; it is all here.

## 0. Gas is sponsored — do NOT manage gas

Remote-wallet transactions are **gas-sponsored** (account abstraction) on
Ethereum, Base, Arbitrum, Polygon, BSC, Monad, MegaEth, Plasma, and
Robinhood — every chain this runbook touches. Never check native gas
balances, never bridge gas, never use `gas_token_amount`. Funding is about
ONE asset: the collateral (USDC). (Only unsponsored chains outside that
list, or a sponsorship outage falling back to normal broadcasts, ever need
native gas.)

## 1. The go-live checklist

0. **Strategy source lives in the workspace**: the entrypoint must be
   `.wayfinder/jobs/<id>/workspace/src/<file>.py` — `validate_job` fails
   `entrypoint_inside_workspace` otherwise (code elsewhere can't be
   versioned or proposed).
1. **Promote** the validated params: `core_jobs(action="promote_params", …)`
   + one full-history confirmation backtest.
2. **Sizing math BEFORE funding** (see §3) — confirm every leg clears venue
   minimums at the user's capital and leverage.
3. **Strategy wallet**: `core_wallets(action="create", label="<job-id>",
   wallet_type="strategy", remote=True)` — remote wallets sign through the
   platform; scripts get a signer via
   `wayfinder_paths.core.utils.wallets.get_remote_sign_callback(address)`.
4. **Fund it** (§2).
4b. **Schedule at the bar close, not "24h from now"**: set
   `script_loop.cron_expr` in `job.yaml` (e.g. `"10 0 * * *"` UTC for daily
   bars — close +10min) instead of an interval anchored at resume time,
   which acts on each bar ~a day late. Schedule/mode/contract changes are
   ALWAYS made by editing `job.yaml` and running `wayfinder job compile
   <id>` (or `core_jobs(action="sync")`) — never by updating the runner
   job directly (a direct update drops the env that carries live mode).
5. **Point the job at its wallet**: set `wallet_label: <job-id>` under
   `execution_params` in `job.yaml` — the live driver defaults to the "main"
   wallet and will trade the wrong account without it.
6. **Switch the job live**: `core_jobs(action="set_script_mode", job_id=…,
   script_mode="live")`. This is the ONLY correct switch — it edits
   `script_loop.mode` in `job.yaml`, recompiles (re-baking the runner env),
   and re-syncs. It runs the live gate first (fresh validation/backtest/
   preflight) and refuses with a named blocker if `wallet_label` (step 5) is
   missing or the gate isn't ready. **Never** flip mode by patching the runner
   env (`WAYFINDER_JOB_MODE` via `core_runner(update_job)`) — the compiler owns
   that value, so the next recompile silently reverts the patch and leaves a
   paper/live split-brain (a job once ran live while the UI read "paper").
7. **Resume the runner**: `core_runner(action="resume_job",
   name="<job-id>-script")`.
8. **Set the watch level** the user chose (see `deploy-and-agent-loop.md`):
   `core_jobs(action="set_agent_mode", job_id=…, mode="monitor")`.
9. **Verify the first tick**: `core_runner_status(action="job_runs",
   name="<job-id>-script", limit=3)` — a `FAILED` run here means fix it now,
   not at the next schedule.

## 2. Funding path (any chain → strategy wallet → Hyperliquid)

Hyperliquid deposits are USDC on **Arbitrum** sent to the HL bridge.
Minimums: **deposit ≥ $5** (below is LOST), **order ≥ $10 notional**.

BRAP bridges/swaps straight into the strategy wallet — `to_wallet` on the
quote routes the output there, so no second hop:

```python
from wayfinder_paths.adapters.brap_adapter import BRAPAdapter
from wayfinder_paths.adapters.hyperliquid_adapter.adapter import HyperliquidAdapter
from wayfinder_paths.core.clients.BRAPClient import BRAP_CLIENT
from wayfinder_paths.core.utils.wallets import get_remote_sign_callback

# 1. Quote: source wallet's USDC (any chain) -> Arbitrum USDC, delivered
#    directly to the strategy wallet.
quote = await BRAP_CLIENT.get_quote(
    from_token=USDC_ON_SOURCE_CHAIN, to_token=USDC_ARBITRUM,
    from_chain=SOURCE_CHAIN_ID, to_chain=42161,
    from_wallet=FUNDED_WALLET, from_amount=str(int(amount_usd * 1e6)),
    to_wallet=STRATEGY_WALLET,
)
best = quote["best_quote"]          # inspect output_amount + fee before executing

# 2. Execute with the FUNDED wallet's remote signer.
brap = BRAPAdapter(sign_callback=get_remote_sign_callback(FUNDED_WALLET),
                   wallet_address=FUNDED_WALLET)
ok, result = await brap.swap_from_quote(from_token, to_token,
                                        FUNDED_WALLET, best)

# 3. Deposit from the strategy wallet to the HL bridge (waits for the bridged
#    USDC to land first — poll the balance).
hl = HyperliquidAdapter(sign_callback=get_remote_sign_callback(STRATEGY_WALLET),
                        wallet_address=STRATEGY_WALLET)
ok, msg = await hl.send_usdc_to_bridge(amount_usd)   # token_id usd-coin-arbitrum
```

Rules: quote first and show the user output/fee; check each step's success
tuple before the next (a bridge takes minutes — poll the destination balance,
don't fire the deposit blind); a receipt with `status=0` is a FAILURE.

## 2b. Adding and withdrawing funds on a running job (owner only)

Moving money in or out of a live job is an **owner action** — the Fund /
Withdraw buttons on the job page, or the owner asking in chat and approving
the transfer. A job's agent never moves funds. Every path runs the same code
(`wayfinder job venue-deposit|venue-withdraw`, or `core_jobs` actions
`venue_deposit` / `venue_withdraw` / `cancel_withdrawal` when the owner
asks), bridging USDC between the job's bound wallet
(`execution_params.wallet_label`) and Hyperliquid, and recording the flow in
`state/capital_flows.jsonl`. The job then handles it mechanically, with no
agent wake and no strategy edit:

- **Sizing** follows the venue: live `mark_to_market_equity(ctx)` returns
  the reconciled account value each tick, so a deposit sizes up on the next
  tick.
- **Capital**: `initial_capital` grows or shrinks with each flow (a job's
  very first deposit replaces a paper placeholder; later ones add). Derived
  views (forward equity curve, regime health, probation) read capital *at
  each point in time* from the ledger, so a flow is a step, not a rebase of
  history. Don't set `initial_capital` by hand on a funded job — that
  rebases all of history.
- **Risk peak**: on the next live tick the drawdown peak is rescaled by the
  flow (withdrawing half the account halves the peak), so a withdrawal never
  reads as a drawdown or trips `max_drawdown` (journal `risk_peak_rebased`).
- **Withdrawal above free margin** (positions open): it is queued in
  `state/pending_withdrawal.json` (result `pending: true`). From the next
  tick the strategy sizes to the account value minus the pending amount, and
  the tick runs the transfer once enough margin is free (journal
  `withdrawal_executed`; a failed transfer stays queued and journals
  `withdrawal_failed` — never a halt). `venue-withdraw <job> --cancel` drops
  it. One queued withdrawal at a time.

Minimums: deposit ≥ $5, withdrawal ≥ $2 gross (Bridge2 nets $1 off).

## 3. Sizing minimums (do this math out loud)

`per_leg = capital × leverage / n_legs` must clear **$10**. A $30 deposit on
a 6-leg market-neutral basket is $5/leg — every order silently skipped; at
5× leverage it's $25/leg — fine. Set leverage via the strategy's params, and
remember the $5 bridge-deposit minimum on top.

## 4. Reverting, pausing, and stopping a live job

Every one of these is a `job.yaml`/state change through a `core_jobs` action —
never a runner env patch. Pick by how hard a stop the user wants:

- **Back to paper (keep it running, stop trading real funds)**:
  `core_jobs(action="set_script_mode", job_id=…, script_mode="paper")`. Same
  compiler-safe path as go-live; no gate needed to step down. The loop keeps
  ticking in paper. Open positions are NOT closed — this only stops NEW live
  orders; to flatten, halt with `flatten=True` (below).
- **Pause the whole job (stop the schedule entirely)**:
  `core_jobs(action="pause", job_id=…)` pauses both the script and agent loops;
  `core_jobs(action="resume", job_id=…)` restarts them. A paused live job keeps
  `mode: live` but does not tick.
- **Halt (emergency kill switch)**: `core_jobs(action="halt", job_id=…,
  reason="…")`, or `flatten=True` to also request position flattening. It
  blocks execution until `core_jobs(action="resume_from_halt", job_id=…)`. Use
  this when something is wrong and you want a hard stop that survives restarts.
- **Withdraw the funds**: an owner action (§2b) — the job page's Withdraw
  button, or `core_jobs(action="venue_withdraw", job_id=…, amount=…)` only
  when the owner asks and approves. Partial withdrawals keep the job running;
  to take everything out, halt with `flatten=True` first so positions
  close, then withdraw the full balance.

## 5. Troubleshooting

- **Runner job FAILED with a missing-file path** (`…/strategy.py not
  found`): stale wrapper from before the job was jobs_v1. Run
  `core_jobs(action="sync")` — it recompiles wrappers; jobs_v1 wrappers call
  the SDK tick driver and need no standalone file.
- **Orders not appearing live**: check per-leg notional ≥ $10, and that the
  runner job is resumed (`core_runner_status(action="status")`).
- **Switching paper→live**: the engine now archives and resets its state
  on a mode flip (paper test ticks no longer pollute live), adopting your
  real venue positions on the first live tick. Jobs whose state predates
  this fix need a one-time manual reset: delete `state/engine_state.json`
  before the first live tick.
- **New jobs**: `core_jobs(action="create")` defaults to
  `execution_contract="jobs_v1"` — never hand-edit job.yaml for this on new
  jobs; only legacy standalone scripts need `execution_contract="legacy"`.
