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

## Key Parameters

| Parameter | Value | Description |
|-----------|-------|-------------|
| `MIN_AMOUNT_USDC` | 2 | Minimum deposit amount |
| `MIN_TVL` | 1,000,000 | Minimum pool TVL |
| `ROTATION_MIN_INTERVAL` | 14 days | Cooldown between rotations |
| `DUST_APY` | 0.01 (1%) | APY threshold below which pools are ignored |
| `SEARCH_DEPTH` | 10 | Number of pools to examine |
| `MIN_GAS` | 0.001 ETH | Minimum gas buffer |
| `GAS_MAXIMUM` | 0.02 ETH | Maximum gas per deposit |

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

## Testing

```bash
poetry run pytest wayfinder_paths/strategies/stablecoin_yield_strategy/ -v
```
