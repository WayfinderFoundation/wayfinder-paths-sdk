# PRJX kHYPE/WHYPE Concentrated LP Strategy

Concentrated liquidity strategy on PRJX (Uniswap V4 fork on HyperEVM) for the kHYPE/WHYPE pair.

- **Module**: `wayfinder_paths.strategies.prjx_khype_lp.strategy.PrjxKhypeLpStrategy`
- **Chain**: HyperEVM (999)
- **Token**: HYPE (deposit as native HYPE)

## How it works

1. User deposits native HYPE
2. Strategy wraps HYPE → WHYPE, splits into WHYPE + kHYPE
3. Mints a concentrated LP position on PRJX centered at the current tick
4. On `update()`, rebalances when price drifts out of range and compounds collected fees
5. On `withdraw()`, removes liquidity, swaps kHYPE → HYPE, unwraps WHYPE

## Yield sources

- LP swap fees from the kHYPE/WHYPE pool
- kHYPE staking yield (~12-18% APY) — kHYPE appreciates against HYPE

## Adapters Used

- BALANCE — wallet reads and transfers
- BRAP — token swaps (HYPE ↔ kHYPE)
- TOKEN — price lookups
- LEDGER — deposit/withdrawal tracking

## Testing

```bash
poetry run pytest wayfinder_paths/strategies/prjx_khype_lp/ -v
```
