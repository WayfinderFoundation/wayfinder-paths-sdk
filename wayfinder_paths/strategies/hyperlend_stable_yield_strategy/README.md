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

## Key Parameters

| Parameter | Value | Description |
|-----------|-------|-------------|
| `MIN_USDT0_DEPOSIT_AMOUNT` | 1 | Minimum deposit amount |
| `GAS_MAXIMUM` | 0.1 HYPE | Maximum gas per deposit |
| `HORIZON_HOURS` | 6 | Analysis horizon |
| `TRIALS` | 4000 | Bootstrap simulation trials |
| `HYSTERESIS_DWELL_HOURS` | 168 | Rotation cooldown |
| `HYSTERESIS_Z` | 1.15 | APY improvement threshold |
| `ROTATION_COOLDOWN` | 168 hours | Minimum time between rotations |
| `APY_REBALANCE_THRESHOLD` | 0.0035 | 35 bps edge required to rotate |

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

## Testing

```bash
poetry run pytest wayfinder_paths/strategies/hyperlend_stable_yield_strategy/ -v
```
