# Stablecoin Yield Strategy

Automated USDC yield optimization on Base chain.

- **Module**: `wayfinder_paths.strategies.stablecoin_yield_strategy.strategy.StablecoinYieldStrategy`
- **Chain**: Base (8453)
- **Token**: USDC

## Overview

This strategy actively manages USDC deposits by:
1. Transferring USDC (plus ETH gas buffer) from main wallet to strategy wallet
2. Searching Base-native pools for the best USD-denominated APY
3. Monitoring DeFi Llama feeds and Wayfinder pool analytics
4. Rebalancing to higher-yield pools when APY improvements exceed thresholds
5. Respecting rotation cooldowns to avoid excessive churn

## How selection works

On every `update`, the strategy:
1. Queries DeFi Llama (via POOL adapter) for all Base stablecoin pools with no IL risk (`stablecoin=True`, `ilRisk="no"`)
2. Filters to pools with `tvlUsd > $1M` and `combined_apy_pct > 1%`
3. Sorts by `combined_apy_pct` (base APY + reward APR) descending
4. Iterates through the top `SEARCH_DEPTH = 10` candidates and selects the first one that passes:
   - `new_apy - current_apy >= MINIMUM_APY_IMPROVEMENT` (1% absolute improvement required)
   - Break-even check: `estimated_profit = 7 * (delta_apy * balance / 365) - fee_cost > 0` (must recover rotation cost within 7 days)
   - 14-day cooldown has passed since last rotation

The combined APY from DeFi Llama includes protocol reward emissions, not just the base supply rate.

## Key Parameters

| Parameter | Value | Description |
|-----------|-------|-------------|
| `MIN_AMOUNT_USDC` | 2 | Minimum deposit amount |
| `MIN_TVL` | 1,000,000 | Minimum pool TVL ($) |
| `ROTATION_MIN_INTERVAL` | 14 days | Cooldown between rotations |
| `MINIMUM_APY_IMPROVEMENT` | 0.01 (1%) | Minimum APY edge required to rotate |
| `MINIMUM_DAYS_UNTIL_PROFIT` | 7 | Break-even payback window (days) |
| `DUST_APY` | 0.01 (1%) | APY floor below which pools are ignored |
| `SEARCH_DEPTH` | 10 | Number of candidate pools examined per update |
| `MIN_GAS` | 0.001 ETH | Minimum gas buffer |
| `GAS_MAXIMUM` | 0.001 ETH | Maximum gas per deposit |

## Adapters Used

- **BalanceAdapter**: Wallet/pool balances, cross-wallet transfers
- **PoolAdapter**: Pool metadata, yield analytics
- **BRAPAdapter**: Swap quotes and execution
- **TokenAdapter**: Token metadata (gas token, USDC info)
- **LedgerAdapter**: Net deposit tracking, cooldown enforcement

## Actions

### Deposit

```bash
poetry run python -m wayfinder_paths.run_strategy stablecoin_yield_strategy \
    --action deposit --main-token-amount 60 --gas-token-amount 0.001 --config config.json
```

- Validates `main_token_amount >= MIN_AMOUNT_USDC`
- Validates `gas_token_amount <= GAS_MAXIMUM`
- Transfers ETH and USDC to strategy wallet
- Initializes position tracking

### Update

```bash
poetry run python -m wayfinder_paths.run_strategy stablecoin_yield_strategy \
    --action update --config config.json
```

- Fetches current balances and active pool
- Runs `_find_best_pool()` to score candidate pools
- Checks rotation cooldown via LedgerAdapter
- Executes rotation if APY improvement threshold met
- Sweeps idle balances into target token

### Status

```bash
poetry run python -m wayfinder_paths.run_strategy stablecoin_yield_strategy \
    --action status --config config.json
```

Returns:
- `portfolio_value`: Current pool balance
- `net_deposit`: From LedgerAdapter
- `strategy_status`: Active pool, APY, wallet balances

### Withdraw

```bash
poetry run python -m wayfinder_paths.run_strategy stablecoin_yield_strategy \
    --action withdraw --config config.json
```

- Unwinds current position via BRAP swaps
- Converts all holdings back to USDC
- Transfers USDC to main wallet
- Clears cached pool state

## Backtesting

### Data availability caveat

The live strategy selects pools using **DeFi Llama data via the POOL adapter** — `combined_apy_pct` which includes base APY plus reward token emissions. Delta Lab's `fetch_lending_rates` only covers a subset of Base lending venues and does not include reward APRs. This means:

- The backtest will underestimate yield (no reward emissions)
- The backtest universe is smaller (only known lending venues vs. any Base stable pool)
- The backtest uses the simpler "point-in-time supply rate" rather than the rolling pool score

### Simplified backtest

```python
import pandas as pd
from wayfinder_paths.core.backtesting.backtester import run_backtest
from wayfinder_paths.core.backtesting.data import fetch_lending_rates
from wayfinder_paths.core.backtesting.types import BacktestConfig
from wayfinder_paths.core.backtesting.yield_strategies import build_yield_index

# --- Parameters from strategy source ---
ROTATION_COOLDOWN_HOURS = 14 * 24   # ROTATION_MIN_INTERVAL
MINIMUM_APY_IMPROVEMENT = 0.01      # 1% absolute improvement required to rotate
DUST_APY = 0.01                     # ignore venues below 1% APY

# Discover available Base USDC venues in Delta Lab
start, end = "2025-08-01", "2026-02-01"
rates = await fetch_lending_rates("USDC", start, end)
print("Available venues:", rates["supply"].columns.tolist())

supply_rates = rates["supply"].ffill().bfill().fillna(0)
# Apply DUST_APY filter to match the live strategy's pool filter
supply_rates = supply_rates.loc[:, supply_rates.mean() >= DUST_APY]
venues = supply_rates.columns.tolist()

prices = build_yield_index(supply_rates, periods_per_year=8760)

# Use current supply rate (not rolling average) as signal — live strategy uses
# the live combined_apy_pct from DeFi Llama at the time of the update call
target = pd.DataFrame(0.0, index=prices.index, columns=venues)
current_venue = None
last_switch_idx = -ROTATION_COOLDOWN_HOURS

for i, ts in enumerate(prices.index):
    row = supply_rates.loc[ts]
    best = row.idxmax()

    if current_venue is None:
        current_venue = best
        last_switch_idx = i
    elif best != current_venue and (i - last_switch_idx) >= ROTATION_COOLDOWN_HOURS:
        # Enforce MINIMUM_APY_IMPROVEMENT (1% gap required)
        if row[best] - row[current_venue] >= MINIMUM_APY_IMPROVEMENT:
            current_venue = best
            last_switch_idx = i

    target.loc[ts, current_venue] = 1.0

config = BacktestConfig(
    fee_rate=0.0, slippage_rate=0.0,
    enable_liquidation=False, periods_per_year=8760,
)
result = run_backtest(prices, target, config)
print(f"Total return: {result.stats['total_return']:.2%}")
print(f"Sharpe:       {result.stats['sharpe']:.2f}")
print(f"Trade count:  {result.stats['trade_count']}")
```

**Known gaps vs. live strategy**:
- Live strategy uses `combined_apy_pct` (base + reward emissions); this backtest uses base supply rate only
- Live universe is all Base stablecoin pools with >$1M TVL (DeFi Llama); this backtest is limited to Delta Lab venues
- Live strategy also applies a 7-day break-even profitability check per candidate (accounting for gas cost); this backtest does not

**Key health checks**: `trade_count` should be very low (1% APY gap + 14-day cooldown means infrequent rotation), `sharpe > 2.0`, `max_drawdown` near 0

## Testing

```bash
poetry run pytest wayfinder_paths/strategies/stablecoin_yield_strategy/ -v
```
