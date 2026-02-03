# Moonwell wstETH Loop Strategy

Leveraged wstETH carry trade on Base via Moonwell.

- **Module**: `wayfinder_paths.strategies.moonwell_wsteth_loop_strategy.strategy.MoonwellWstethLoopStrategy`
- **Chain**: Base (8453)
- **Tokens**: USDC, WETH, wstETH

## Overview

This strategy creates a leveraged liquid-staking carry trade by:
1. Depositing USDC as initial collateral on Moonwell
2. Borrowing WETH against the USDC collateral
3. Swapping WETH to wstETH via Aerodrome/BRAP
4. Lending wstETH back to Moonwell as additional collateral
5. Repeating the loop until target leverage is reached

The position is **delta-neutral**: WETH debt offsets wstETH collateral, so PnL is driven by the spread between wstETH staking yield and WETH borrow cost.

## Key Parameters

| Parameter | Value | Description |
|-----------|-------|-------------|
| `MIN_GAS` | 0.002 ETH | Minimum gas buffer |
| `MIN_USDC_DEPOSIT` | 20 USDC | Minimum initial collateral |
| `MAX_DEPEG` | 0.01 (1%) | Max stETH/ETH depeg threshold |
| `MIN_HEALTH_FACTOR` | 1.2 | Triggers deleveraging if below |
| `MAX_HEALTH_FACTOR` | 1.5 | Triggers leverage loop if above |
| `leverage_limit` | 10 | Maximum leverage multiplier |
| `COLLATERAL_SAFETY_FACTOR` | 0.98 | 2% safety buffer on borrows |
| `MAX_SLIPPAGE_TOLERANCE` | 0.03 | 3% max slippage |

## Safety Features

- **Depeg guard**: Calculates leverage ceiling based on collateral factor and max depeg tolerance
- **Delta-neutrality**: Enforces wstETH collateral >= WETH debt
- **Swap retries**: Progressive slippage (0.5% -> 1% -> 1.5%) with exponential backoff
- **Health monitoring**: Automatic deleveraging when health factor drops
- **Deterministic reads**: Waits 2 blocks after receipts to avoid stale RPC data

## Adapters Used

- **BalanceAdapter**: Token balances, wallet transfers
- **TokenAdapter**: Token metadata, price feeds
- **LedgerAdapter**: Net deposit tracking
- **BRAPAdapter**: Swap quotes and execution
- **MoonwellAdapter**: Lending, borrowing, collateral management

## Actions

### Deposit

```bash
poetry run python -m wayfinder_paths.run_strategy moonwell_wsteth_loop_strategy \
    --action deposit --main-token-amount 100 --gas-token-amount 0.01 --config config.json
```

- Validates USDC and ETH balances
- Transfers ETH gas buffer if needed
- Moves USDC to strategy wallet
- Lends USDC on Moonwell and enables as collateral
- Executes leverage loop (borrow WETH -> swap to wstETH -> lend)

### Update

```bash
poetry run python -m wayfinder_paths.run_strategy moonwell_wsteth_loop_strategy \
    --action update --config config.json
```

- Checks gas balance meets threshold
- Reconciles wallet leftovers into position
- Computes health factor/LTV/delta
- If HF < MIN: triggers deleveraging
- If HF > MAX: executes additional leverage loops
- Claims WELL rewards if above threshold

### Status

```bash
poetry run python -m wayfinder_paths.run_strategy moonwell_wsteth_loop_strategy \
    --action status --config config.json
```

Returns:
- `portfolio_value`: USDC lent + wstETH lent - WETH debt
- `net_deposit`: From LedgerAdapter
- `strategy_status`: Leverage, health factor, LTV, peg diff, credit remaining

### Withdraw

```bash
poetry run python -m wayfinder_paths.run_strategy moonwell_wsteth_loop_strategy \
    --action withdraw --config config.json
```

- Sweeps miscellaneous token balances to WETH
- Repays all WETH debt
- Unlends wstETH, swaps to USDC
- Unlends USDC collateral
- Returns USDC and remaining ETH to main wallet

## Testing

```bash
poetry run pytest wayfinder_paths/strategies/moonwell_wsteth_loop_strategy/ -v
```
