# Basis Trading Strategy

Delta-neutral basis trading on Hyperliquid for funding rate capture.

- **Module**: `wayfinder_paths.strategies.basis_trading_strategy.strategy.BasisTradingStrategy`
- **Platform**: Hyperliquid
- **Token**: USDC

## Overview

This strategy captures funding rate payments through matched positions:
- **Long Spot**: Buy the underlying asset (e.g., HYPE)
- **Short Perp**: Short the perpetual contract for the same asset

Price movements cancel out, and profit comes from collecting funding payments when longs pay shorts.

## How It Works

### Position Sizing

Given deposit `D` USDC and leverage `L`:
- **Order Size**: `D * (L / (L + 1))`
- **Margin Reserved**: `D / (L + 1)`

Example with $100 deposit at 2x leverage:
- Order size: $66.67 per leg
- Margin: $33.33

### Opportunity Selection

1. **Discovery**: Scan Hyperliquid markets for spot-perp pairs
2. **Historical Analysis**: Fetch up to 180 days of hourly funding/price data
3. **Safe Leverage Calculation**: Stress test over rolling windows
4. **Ranking**: Sort by expected APY = mean_funding * 24 * 365 * safe_leverage

## Key Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_leverage` | 3 | Maximum leverage allowed |
| `lookback_days` | 180 | Days of historical data |
| `confidence` | 0.975 | VaR confidence level (97.5%) |
| `fee_eps` | 0.003 | Fee buffer (0.3%) |
| `bootstrap_sims` | 50 | Monte Carlo simulations |
| `MIN_DEPOSIT_USDC` | 50 | Minimum deposit |

## Adapters Used

- **BalanceAdapter**: Wallet balances, ERC20 transfers
- **LedgerAdapter**: Deposit/withdraw tracking
- **TokenAdapter**: Token metadata
- **HyperliquidAdapter**: Market data, order execution, account state

## Actions

### Analyze

```bash
poetry run python -m wayfinder_paths.run_strategy basis_trading_strategy \
    --action analyze --amount 1000 --config config.json
```

Analyzes opportunities without opening positions.

### Deposit

```bash
poetry run python -m wayfinder_paths.run_strategy basis_trading_strategy \
    --action deposit --main-token-amount 100 --config config.json
```

- Transfers USDC from main wallet to strategy wallet
- Bridges USDC to Hyperliquid via Arbitrum
- Splits between perp margin and spot
- Uses PairedFiller for atomic execution (buy spot + sell perp)
- Places protective orders (stop-loss, limit sell)

### Update

```bash
poetry run python -m wayfinder_paths.run_strategy basis_trading_strategy \
    --action update --config config.json
```

- Checks if position needs rebalancing
- Deploys idle capital via scale-up
- Verifies leg balance (spot ≈ perp)
- Updates stop-loss/limit orders if needed

### Status

```bash
poetry run python -m wayfinder_paths.run_strategy basis_trading_strategy \
    --action status --config config.json
```

### Withdraw

```bash
poetry run python -m wayfinder_paths.run_strategy basis_trading_strategy \
    --action withdraw --config config.json
```

- Cancels all open orders
- Uses PairedFiller to close both legs (sell spot + buy perp)
- Withdraws USDC from Hyperliquid to Arbitrum
- Sends funds back to main wallet

## Risk Factors

1. **Funding Rate Flips**: Rates can turn negative
2. **Liquidation Risk**: High leverage + adverse price movement
3. **Execution Slippage**: Large orders may move the market
4. **Withdrawal Delays**: Hyperliquid withdrawals take ~15-30 minutes
5. **Smart Contract Risk**: Funds are held on Hyperliquid's L1

## Testing

```bash
poetry run pytest wayfinder_paths/strategies/basis_trading_strategy/ -v
```
