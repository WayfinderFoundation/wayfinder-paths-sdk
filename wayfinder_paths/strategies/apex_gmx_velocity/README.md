# APEX/GMX Pair Velocity

Market-neutral pair-mean-reversion on Hyperliquid perps. Trades the
log-spread between APEX and GMX with a velocity-confirmed z-score
entry and zero-cross exit.

## Performance (200d, with 25 bps slippage + 4.5 bps fee + funding)

| Window | Sharpe | Return | Trades |
|---|---:|---:|---:|
| 30d | 3.25 | +8.77% | 27 |
| 60d | 4.07 | +26.54% | 55 |
| 90d | 3.78 | +50.14% | 83 |
| 120d | 3.74 | +64.07% | 104 |

Activity at $46 NAV: ~0.9 trades/day, ~$25 daily volume. Per-leg
notional ~$34 — well above HL's $10 minimum at lev=1.5.

## Logic

For each hourly bar:
- `z = (log(APEX/GMX) - rolling_mean) / rolling_std` over 72 bars
- `dz = z[t] - z[t-6]` (6-bar velocity)
- LONG APEX / SHORT GMX when `z < -2.0` AND `dz > 0`
- SHORT APEX / LONG GMX when `z > +2.0` AND `dz < 0`
- Exit when z crosses zero

Each side gets `target_leverage / 2 = 0.75` of NAV. Total gross
exposure when entered: 1.5× NAV.

## Funding economics

Both legs have positive funding (longs pay shorts). Annualized:
- APEX: +11.96%
- GMX: +8.47%

Net funding for a paired position alternates between paying ~4% and
receiving ~4% depending on direction. Over reasonable windows the
two cancel — funding is **net neutral** in the audit. Watch for
funding spikes on either leg (HIP-3 funding can move fast).

## Deploy

1. Set `name` in `strategy.py` to a wallet label that exists in
   `config.json` and is funded on HL with USDC margin.
2. Verify HL has APEX and GMX listed (both currently are).
3. Add the runner job:
   ```bash
   poetry run wayfinder runner add-job \
       --name apex-gmx-update \
       --type strategy \
       --strategy apex_gmx_velocity \
       --action update \
       --interval 3600 \
       --timeout 1800
   ```
4. Add reconciliation:
   ```bash
   poetry run wayfinder runner add-job \
       --name apex-gmx-reconcile \
       --type strategy \
       --strategy apex_gmx_velocity \
       --action reconcile \
       --interval 86400 \
       --timeout 300
   ```

## Files

- `signal.py` — pure pair-velocity signal returning a SignalFrame
- `decide.py` — per-bar order placement with szDecimals rounding
- `strategy.py` — `ApexGmxVelocityStrategy(ActivePerpsStrategy)` declaration
- `manifest.yaml` — adapter requirements + params + risk limits
- `backtest_ref.json` — frozen reference: code SHAs, params, performance,
  drift tolerances. Used by reconcile to detect drift.
- `examples.json` — test-data spec for smoke tests
- `test_strategy.py` — smoke test
