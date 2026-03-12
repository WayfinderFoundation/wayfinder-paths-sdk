# Multi Vault Split Strategy

Diversified USDC vault allocation across Hyperliquid HLP, Boros vaults, and Avantis avUSDC.

- **Module**: `wayfinder_paths.strategies.multi_vault_split_strategy.strategy.MultiVaultSplitStrategy`
- **Primary funding chain**: Arbitrum
- **Token**: USDC

## Overview

This strategy splits Arbitrum USDC across three market-neutral vault legs:
1. Hyperliquid HLP
2. Boros AMM vaults
3. Avantis avUSDC on Base

It deploys fresh capital, prefers high-yield Boros vaults that satisfy tenor/capacity checks, and best-effort unwinds back to Arbitrum USDC on withdraw.

## Key Parameters

| Parameter | Value | Description |
|-----------|-------|-------------|
| `MIN_NET_DEPOSIT` | 40 | Minimum deposit amount |
| `MIN_HLP_USD` | 11 | Minimum HLP leg size |
| `MIN_BOROS_USD` | 11 | Minimum Boros leg size |
| `allocation_mode` | `hybrid_apy` or `fixed` | Allocation policy |
| `weights` | `hlp`, `boros`, `avantis` | Target leg weights |
| `boros_token_id` | `3` | Default Boros collateral token (USDT) |
| `boros_allow_isolated_only_vaults` | `True` | Allows isolated-only Boros vaults |

## Adapters Used

- **BalanceAdapter**: Wallet balances and transfers
- **BRAPAdapter**: Swaps and bridges between legs/chains
- **HyperliquidAdapter**: HLP deposits, withdrawals, and status
- **BorosAdapter**: Vault discovery, deposits, withdrawals, and finalize flow
- **AvantisAdapter**: avUSDC deposit/withdraw
- **LedgerAdapter**: Net deposit tracking

## Actions

### Deposit

```bash
poetry run python -m wayfinder_paths.run_strategy multi_vault_split_strategy \
    --action deposit --main-token-amount 100 --gas-token-amount 0.001 --config config.json
```

- Transfers Arbitrum ETH gas and USDC from main wallet to the strategy wallet
- Deploys capital across enabled legs
- Uses Boros vault filtering by capacity, tenor, and isolated-only eligibility

### Update

```bash
poetry run python -m wayfinder_paths.run_strategy multi_vault_split_strategy \
    --action update --config config.json
```

- Deploys newly idle balances
- Rolls or finalizes Boros positions when needed
- Respects HLP withdrawal cooldown state
- Reuses current Boros vaults when possible before selecting a new best-yield vault

### Status

```bash
poetry run python -m wayfinder_paths.run_strategy multi_vault_split_strategy \
    --action status --config config.json
```

Returns:
- `portfolio_value`
- `net_deposit`
- per-leg balances and strategy summary

### Withdraw

```bash
poetry run python -m wayfinder_paths.run_strategy multi_vault_split_strategy \
    --action withdraw --config config.json
```

- Best-effort unwinds HLP, Boros, and Avantis legs
- Finalizes Boros withdrawal when available
- Returns proceeds toward Arbitrum USDC

## Risks

1. Smart contract and bridge risk across Hyperliquid, Boros, Avantis, and BRAP
2. HLP withdrawal cooldowns can delay capital exit
3. Boros withdrawals may require a later finalize step
4. Cross-chain routing can leave temporary idle balances in transit

## Testing

```bash
poetry run pytest wayfinder_paths/strategies/multi_vault_split_strategy/ -v
```
