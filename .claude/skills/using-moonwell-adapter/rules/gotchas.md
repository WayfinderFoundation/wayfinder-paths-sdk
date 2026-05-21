# Moonwell Gotchas

## Core Versus Morpho

`MoonwellAdapter` owns Moonwell Core mToken markets only. Moonwell Morpho Vaults and
isolated markets are Morpho contracts; use `MorphoAdapter` for those fund-moving
flows.

## Always Set The Chain

The adapter defaults to Base (`8453`). Pass `chain_id=` for OP Mainnet, Moonbeam,
or Moonriver. Do not mix mToken and underlying addresses across chains.

| Network | chain_id | Reward claim support |
|---------|----------|----------------------|
| Base | `8453` | Yes |
| OP Mainnet | `10` | Yes |
| Moonbeam | `1284` | No Multi-Reward Distributor configured |
| Moonriver | `1285` | No Multi-Reward Distributor configured |

## mToken Addresses Are Required

Write methods take mToken addresses. Passing an underlying token address as `mtoken`
will target the wrong contract and revert.

Base examples:

| Asset | mToken | Underlying |
|-------|--------|------------|
| USDC | `0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22` | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` |
| WETH | `0x628ff693426583D9a7FB391E54366292F509D457` | `0x4200000000000000000000000000000000000006` |
| wstETH | `0x627Fe393Bc6EdDA28e99AE648fD6fF362514304b` | `0xc1CBa3fCea344f92D9239c08C0568f6F2F0ee452` |

## Units Are Raw Integers

All amount parameters are raw integers:

| Token | Decimals | Example |
|-------|----------|---------|
| USDC | 6 | `10 * 10**6` for 10 USDC |
| WETH | 18 | `10**16` for 0.01 WETH |
| mTokens | 8 | Use `max_withdrawable_mtoken()["cTokens_raw"]` |

## Approvals

- `lend()` approves the mToken to pull underlying before `mint()`.
- `repay()` approves the mToken to pull underlying before `repayBorrow()`.
- `repay_full=True` uses `MAX_UINT256` for the repay call; make sure the wallet has enough underlying for accrued interest.
- Collateral and reward calls do not use ERC-20 approval, but they still broadcast transactions.

## Collateral Is Explicit

Supplying does not enable collateral. Call `set_collateral()` and verify with
`is_market_entered()` before borrowing.

## Withdraw Amount Is mToken Amount

`unlend()` calls `redeem()` and expects mToken units, not underlying units:

```python
ok, info = await adapter.max_withdrawable_mtoken(chain_id=8453, mtoken=M_USDC)
ok, tx = await adapter.unlend(chain_id=8453, mtoken=M_USDC, amount=info["cTokens_raw"])
```

## Deprecated And Bad-Debt Markets

The market list includes metadata from the official Moonwell SDK source:

- Base has deprecated USDbC.
- Moonbeam includes bad-debt and deprecated markets.
- Moonriver Core markets are deprecated.

Inspect `deprecated` and `badDebt` before using a market for a new fund-moving flow.

## Transaction Receipts

A transaction hash only means a transaction was broadcast. The SDK waits for the
receipt and raises on `status=0`. Stop a multi-step flow after any failed or
reverted Moonwell transaction.

## Gorlami

For fund-moving EVM flows, prefer a Gorlami fork dry run before live execution.
`test_gorlami_simulation.py` exercises Base supply, collateral, borrow, repay,
withdraw, and claim behavior when an API key is configured.
