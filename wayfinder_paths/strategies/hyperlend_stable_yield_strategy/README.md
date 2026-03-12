# HyperLend Stable Yield Strategy

Stablecoin yield optimization on HyperLend (HyperEVM).

- **Module**: `wayfinder_paths.strategies.hyperlend_stable_yield_strategy.strategy.HyperlendStableYieldStrategy`
- **Chain**: HyperEVM
- **Token**: USDT0

## Overview

This strategy allocates USDT0 across HyperLend stablecoin markets by:
1. Transferring USDT0 (plus HYPE gas buffer) from main wallet to strategy wallet
2. Sampling HyperLend hourly rate history
3. Running bootstrap tournament analysis to identify best-performing stablecoin
4. Swapping and supplying to HyperLend
5. Enforcing hysteresis rotation policy to prevent excessive churn

## How selection works

On every `update`, the strategy:
1. Fetches 7-day hourly rate history from the **HyperLend API** for each candidate stablecoin (up to `MAX_CANDIDATES = 5`)
2. Runs a joint block-bootstrap tournament: samples 4000 6-hour horizons (6-hour blocks, recency-weighted with 7-day half-life) and records which asset had the highest cumulative log-return in each trial
3. Computes `p_best[i]` = fraction of trials where asset `i` won
4. Sorts candidates by `[p_best, q05, mean]` descending
5. Rotates to the challenger only if all pass:
   - `p_best[challenger] > max(p_best[current], P_BEST_ROTATION_THRESHOLD=0.4)`
   - The hysteresis band clears: `edge_cum_log > amortized_cost + HYSTERESIS_Z * sigma_delta`
     where `edge_cum_log` = difference in 6h cumulative log-returns, `amortized_cost` = rotation cost amortized over `HYSTERESIS_DWELL_HOURS`, and `sigma_delta` = combined std of both legs

**Note**: `ROTATION_POLICY = "hysteresis"` is the default. The legacy `APY_REBALANCE_THRESHOLD` (35 bps check) only applies if `HYPERLEND_ROTATION_POLICY=cooldown` is set — it is NOT used in normal operation.

## Key Parameters

| Parameter | Value | Description |
|-----------|-------|-------------|
| `MIN_USDT0_DEPOSIT_AMOUNT` | 1 | Minimum deposit amount |
| `DEFAULT_LOOKBACK_HOURS` | 168 (7 days) | Rate history window fed to tournament |
| `HORIZON_HOURS` | 6 | Simulated horizon per bootstrap trial |
| `BLOCK_LEN` | 6 | Block size for block-bootstrap resampling |
| `TRIALS` | 4000 | Bootstrap simulation trials |
| `HALFLIFE_DAYS` | 7 | Recency weighting half-life |
| `P_BEST_ROTATION_THRESHOLD` | 0.4 | Minimum `p_best` for challenger to trigger rotation |
| `HYSTERESIS_DWELL_HOURS` | 168 | Minimum hours between rotations |
| `HYSTERESIS_Z` | 1.15 | z-score gap threshold for hysteresis rotation |
| `APY_REBALANCE_THRESHOLD` | 0.0035 | 35 bps edge check used in legacy `cooldown` policy only (not the default hysteresis mode) |
| `ROTATION_TX_COST` | 0.002 | Cost estimate per rotation (0.2%) |
| `MAX_CANDIDATES` | 5 | Maximum stablecoin candidates considered |
| `GAS_MAXIMUM` | 0.1 HYPE | Maximum gas per deposit |

## Adapters Used

- **BalanceAdapter**: Token/pool balances, wallet transfers
- **TokenAdapter**: Token metadata (USDT0, HYPE)
- **LedgerAdapter**: Net deposit, rotation history
- **BRAPAdapter**: Swap quotes and execution
- **HyperlendAdapter**: Asset views, lend/withdraw operations

## Actions

### Deposit

```bash
poetry run python -m wayfinder_paths.run_strategy hyperlend_stable_yield_strategy \
    --action deposit --main-token-amount 25 --gas-token-amount 0.02 --config config.json
```

- Validates USDT0 and HYPE balances in main wallet
- Transfers HYPE for gas buffer
- Moves USDT0 to strategy wallet
- Clears cached asset snapshots

### Update

```bash
poetry run python -m wayfinder_paths.run_strategy hyperlend_stable_yield_strategy \
    --action update --config config.json
```

- Refreshes HyperLend asset snapshots
- Runs tournament analysis to find winner
- Enforces cooldown (unless short-circuit triggered)
- Executes rotation via BRAP if new asset wins
- Sweeps residual balances and lends via HyperlendAdapter

### Status

```bash
poetry run python -m wayfinder_paths.run_strategy hyperlend_stable_yield_strategy \
    --action status --config config.json
```

Returns:
- `portfolio_value`: Active lend balance
- `net_deposit`: From LedgerAdapter
- `strategy_status`: Current asset, APY, balances, tournament projections

### Withdraw

```bash
poetry run python -m wayfinder_paths.run_strategy hyperlend_stable_yield_strategy \
    --action withdraw --config config.json
```

- Unwinds HyperLend positions
- Swaps back to USDT0 if needed
- Returns USDT0 and residual HYPE to main wallet

## Backtesting

### Data availability caveat

The live strategy fetches rate history directly from the **HyperLend API**, not Delta Lab. Before attempting a backtest, verify whether Delta Lab has HyperLend supply rate data:

```python
from wayfinder_paths.core.backtesting import fetch_lending_rates

rates = await fetch_lending_rates("USDT0", "2025-08-01", "2026-02-01")
print(rates["supply"].columns.tolist())  # check for any 'hyperlend-hyperevm' or similar
```

If no HyperLend venues appear, the data is not yet available in Delta Lab and a historical backtest cannot be run from this tooling alone.

### What the live strategy actually does (and what can be approximated)

The live selection uses a **joint block-bootstrap tournament** that cannot be directly replicated with the standard backtesting helpers:
- Runs 4000 trials of a 6-hour simulated horizon, sampling blocks from the recency-weighted (7-day half-life) 168-hour history
- Ranks by `p_best` (win rate across trials), then `q05` (5th-percentile log return), then `mean`
- Rotation requires `p_best[challenger] > max(p_best[current], 0.4)` AND the hysteresis z=1.15 dwell band

The approximation below uses rolling mean supply APY as a proxy for `p_best` and a simple cooldown for the hysteresis band.

### Simplified backtest (if Delta Lab has HyperLend data)

```python
import pandas as pd
from wayfinder_paths.core.backtesting import (
    build_yield_index, fetch_lending_rates, run_backtest, BacktestConfig,
)

# --- Parameters from strategy source ---
LOOKBACK_HOURS = 168              # DEFAULT_LOOKBACK_HOURS (7 days of rate history)
ROTATION_COOLDOWN_HOURS = 168     # HYSTERESIS_DWELL_HOURS
APY_REBALANCE_THRESHOLD = 0.0035  # 35 bps edge (proxy for p_best gap)

start, end = "2025-08-01", "2026-02-01"

rates = await fetch_lending_rates("USDT0", start, end)
supply_rates = rates["supply"].ffill().bfill().fillna(0)
# Keep only HyperLend venues (the live strategy only considers HyperLend assets)
venues = [v for v in supply_rates.columns if "hyperlend" in v.lower()]
if not venues:
    raise RuntimeError("No HyperLend venue data found in Delta Lab for USDT0")
supply_rates = supply_rates[venues]

prices = build_yield_index(supply_rates, periods_per_year=8760)

# Rolling mean APY as proxy for the tournament's p_best ranking
rolling_avg = supply_rates.rolling(LOOKBACK_HOURS, min_periods=1).mean()
target = pd.DataFrame(0.0, index=prices.index, columns=venues)

current_venue = None
last_switch_idx = -ROTATION_COOLDOWN_HOURS

for i, ts in enumerate(prices.index):
    best = rolling_avg.loc[ts].idxmax()
    best_rate = rolling_avg.loc[ts, best]

    if current_venue is None:
        current_venue = best
        last_switch_idx = i
    elif best != current_venue and (i - last_switch_idx) >= ROTATION_COOLDOWN_HOURS:
        current_rate = rolling_avg.loc[ts, current_venue]
        if best_rate - current_rate >= APY_REBALANCE_THRESHOLD:
            current_venue = best
            last_switch_idx = i

    target.loc[ts, current_venue] = 1.0

config = BacktestConfig(
    fee_rate=0.0, slippage_rate=0.0,
    enable_liquidation=False, periods_per_year=8760,
)
result = run_backtest(prices, target, config)
print(f"Total return: {result.stats['total_return']:.2%}")
print(f"Sharpe: {result.stats['sharpe']:.2f}")
print(f"Trade count: {result.stats['trade_count']}")
```

**Known gaps vs. live strategy**:
- Live strategy uses `p_best` from a 4000-trial bootstrap tournament (recency-weighted); this approximation uses rolling mean APY
- Live rotation also checks a z-score hysteresis band, not just a fixed APY threshold
- If HyperLend only has one stablecoin with meaningful history, the backtest will trivially hold that venue — add the `APY_REBALANCE_THRESHOLD` check to avoid false rotation signals

**Key health checks**: low `trade_count` (hysteresis working), `max_drawdown` near 0, `sharpe > 2.0`

## Testing

```bash
poetry run pytest wayfinder_paths/strategies/hyperlend_stable_yield_strategy/ -v
```
