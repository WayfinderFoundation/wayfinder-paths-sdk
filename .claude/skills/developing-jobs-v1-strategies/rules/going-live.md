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

1. **Promote** the validated params: `core_jobs(action="promote_params", …)`
   + one full-history confirmation backtest.
2. **Sizing math BEFORE funding** (see §3) — confirm every leg clears venue
   minimums at the user's capital and leverage.
3. **Strategy wallet**: `core_wallets(action="create", label="<job-id>",
   wallet_type="strategy", remote=True)` — remote wallets sign through the
   platform; scripts get a signer via
   `wayfinder_paths.core.utils.wallets.get_remote_sign_callback(address)`.
4. **Fund it** (§2).
5. **Point the job at its wallet**: set `wallet_label: <job-id>` under
   `execution_params` in `job.yaml` — the live driver defaults to the "main"
   wallet and will trade the wrong account without it.
6. **Switch the job live**: edit `job.yaml` runner `mode: paper` → `live`,
   then `core_jobs(action="sync")` — sync also recompiles the runner
   wrappers, which heals any stale wrapper from create time.
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

## 3. Sizing minimums (do this math out loud)

`per_leg = capital × leverage / n_legs` must clear **$10**. A $30 deposit on
a 6-leg market-neutral basket is $5/leg — every order silently skipped; at
5× leverage it's $25/leg — fine. Set leverage via the strategy's params, and
remember the $5 bridge-deposit minimum on top.

## 4. Troubleshooting

- **Runner job FAILED with a missing-file path** (`…/strategy.py not
  found`): stale wrapper from before the job was jobs_v1. Run
  `core_jobs(action="sync")` — it recompiles wrappers; jobs_v1 wrappers call
  the SDK tick driver and need no standalone file.
- **Orders not appearing live**: check per-leg notional ≥ $10, and that the
  runner job is resumed (`core_runner_status(action="status")`).
- **New jobs**: `core_jobs(action="create")` defaults to
  `execution_contract="jobs_v1"` — never hand-edit job.yaml for this on new
  jobs; only legacy standalone scripts need `execution_contract="legacy"`.
