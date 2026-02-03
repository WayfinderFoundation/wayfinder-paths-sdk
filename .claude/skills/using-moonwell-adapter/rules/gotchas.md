# Moonwell gotchas

## Transaction receipts (broadcast ≠ success)

- A tx hash / “broadcasted” log does **not** mean a Moonwell call succeeded.
- The SDK waits for the receipt and raises `TransactionRevertedError` when `status=0` (often includes `gasUsed`/`gasLimit` and may indicate out-of-gas).
- If a step reverts, stop and fix before proceeding (e.g., don’t borrow/repay assuming a prior lend/unlend worked).

## mToken addresses (not underlying!)

All adapter methods take **mToken addresses**, not underlying token addresses:

| Asset | mToken Address | Underlying Address |
|-------|----------------|-------------------|
| USDC | `0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22` | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` |
| WETH | `0x628ff693426583D9a7FB391E54366292F509D457` | `0x4200000000000000000000000000000000000006` |
| wstETH | `0x627Fe393Bc6EdDA28e99AE648fD6fF362514304b` | `0xc1CBa3fCea344f92D9239c08C0568f6F2F0ee452` |

**Wrong:** `adapter.lend("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", amount)` (underlying)
**Right:** `adapter.lend("0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22", amount)` (mToken)

## Units are raw ints

All `amount` parameters are **raw int units** (not human-readable floats):

| Token | Decimals | 1 unit | Example |
|-------|----------|--------|---------|
| USDC | 6 | `1_000_000` | 10 USDC = `10_000_000` |
| WETH | 18 | `10**18` | 0.01 WETH = `10**16` |
| mTokens | 8 | `10**8` | — |

## Two USDC markets

Moonwell has **two USDC markets** on Base:
- `0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22` - **Main market** (use this one)
- `0x703843C3379b52F9FF486c9f5892218d2a065cC8` - Secondary market

## Collateral must be explicitly enabled

Supplying tokens does NOT automatically enable them as collateral:

```python
# Supply doesn't enable collateral
await adapter.lend(mtoken=USDC_MTOKEN, amount=1_000_000)

# Must explicitly enable
await adapter.set_collateral(mtoken=USDC_MTOKEN)
```

## Check before borrow

Always check `get_borrowable_amount()` before borrowing to avoid reverts:

```python
# get_borrowable_amount returns account liquidity in USD (no mtoken param)
ok, liquidity_usd = await adapter.get_borrowable_amount()
if liquidity_usd <= 0:
    print("Insufficient collateral")
```

## unlend() takes mToken amount, not underlying

The `unlend()` method calls `redeem()` which expects **mToken amount**, not underlying:

```python
# WRONG - passing underlying amount
await adapter.unlend(mtoken=USDC_MTOKEN, amount=5_000_000)  # 5 USDC? No!

# RIGHT - get mToken amount first
ok, info = await adapter.max_withdrawable_mtoken(mtoken=USDC_MTOKEN)
await adapter.unlend(mtoken=USDC_MTOKEN, amount=info['cTokens_raw'])
```

## Exchange rate scaling

When manually converting between mTokens and underlying:
- `exchangeRate` from contract is scaled by 1e18
- `underlying = mTokenBalance * exchangeRate / 1e18`

## Script execution

Always run scripts via MCP tool with wallet tracking:

```
mcp__wayfinder__run_script(
    script_path=".wayfinder_runs/moonwell_lend.py",
    wallet_label="main"
)
```

This ensures the wallet profile tracks the Moonwell interaction for portfolio discovery.
