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
| `MIN_USDC_DEPOSIT` | 10 USDC | Minimum initial collateral |
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

## Backtesting

### How the position generates returns

The strategy holds a delta-neutral loop: wstETH as collateral + USDC as seed collateral, WETH as debt. Because the collateral and debt are both ETH-denominated (wstETH ≈ ETH), price moves largely cancel. Net PnL comes from the **spread**:

```
net hourly return ≈ (wstETH_staking_yield × leverage - WETH_borrow_rate × (leverage - 1)) / 8760
```

The live leverage varies — the strategy targets `TARGET_HEALTH_FACTOR = 1.25` and rebalances when HF leaves the range [1.2, 1.5]. Approximate leverage ≈ 2–3x depending on Moonwell's collateral factors for wstETH and USDC on Base.

### Starter backtest

Compute stats directly from the hourly returns series. **Do not** use `run_backtest` with
`target=1.0` — that produces NaN stats for synthetic-price strategies.

```python
import asyncio
import numpy as np
import pandas as pd
from datetime import datetime
from wayfinder_paths.core.clients.DeltaLabClient import DELTA_LAB_CLIENT
from wayfinder_paths.core.backtesting.data import fetch_lending_rates

LEVERAGE = 2.5          # mid-range estimate; live leverage is HF-controlled (~2-3x)
START = "2025-08-13"    # oldest safe Delta Lab date; update as needed
END = "2026-03-01"
PERIODS_PER_YEAR = 8760

async def backtest() -> None:
    start_dt = datetime.fromisoformat(START)
    end_dt = datetime.fromisoformat(END)
    lookback = (end_dt - start_dt).days

    # --- wstETH staking yield ---
    # Use base_yield_apy from the wstETH lending timeseries.
    # This is the real Lido intrinsic APY baked into the wstETH/ETH exchange rate.
    # Do NOT use fetch_supply_rates("wstETH") — it returns the Moonwell lending APR
    # on top of staking yield, which is near-zero and not the staking yield itself.
    # Do NOT derive from hourly wstETH/ETH price ratio — too noisy at 1h granularity.
    data = await DELTA_LAB_CLIENT.get_asset_timeseries(
        symbol="wstETH", lookback_days=lookback, limit=10000, as_of=end_dt, series="lending"
    )
    mw = data["lending"]
    mw = mw[mw["venue"] == "moonwell-base"].copy()
    mw.index = pd.to_datetime(mw.index)
    lido_rate = mw["base_yield_apy"].resample("1h").last().ffill(limit=24).bfill()
    # During Aug 2025–Mar 2026, real Lido rate was ~2.3% ann (not the ~3.5% often assumed).

    # --- WETH borrow rate on Moonwell ---
    weth_rates = await fetch_lending_rates("WETH", START, END, venues=["moonwell-base"])
    borrow_rate = weth_rates["borrow"]["moonwell-base"].ffill().bfill().fillna(0)

    # Align to common index
    idx = lido_rate.index.intersection(borrow_rate.index)
    lido_h = lido_rate.reindex(idx).ffill() / PERIODS_PER_YEAR
    borrow_h = borrow_rate.reindex(idx).ffill() / PERIODS_PER_YEAR

    # Hourly returns: staking yield amplified by leverage, minus WETH borrow cost on debt
    hourly_ret = lido_h * LEVERAGE - borrow_h * (LEVERAGE - 1)

    # Stats
    cumret = (1 + hourly_ret).cumprod()
    total = float(cumret.iloc[-1]) - 1.0
    years = len(hourly_ret) / PERIODS_PER_YEAR
    cagr = float((1 + total) ** (1 / years) - 1)
    vol = float(hourly_ret.std(ddof=0)) * np.sqrt(PERIODS_PER_YEAR)
    sharpe = cagr / vol if vol > 0 else 0.0
    mdd = float((cumret / cumret.cummax() - 1).min())

    print(f"Total return : {total:.2%}")
    print(f"Ann. return  : {cagr:.2%}")
    print(f"Sharpe       : {sharpe:.2f}")
    print(f"Max drawdown : {mdd:.2%}")
    print(f"Lido median  : {lido_rate.reindex(idx).median():.2%}")
    print(f"Borrow median: {borrow_rate.reindex(idx).median():.2%}")

asyncio.run(backtest())
```

**WELL reward emissions**: `supply_reward_apr` is not populated in Delta Lab for Moonwell
wstETH — rewards are not tracked. Add a constant `well_apy / PERIODS_PER_YEAR` term to
`hourly_ret` to sensitivity-test (typical range: +0.5–2% ann depending on WELL price).

**Known gaps vs. live strategy**:
- Live leverage is dynamic (HF-controlled); this backtest uses a fixed 2.5x estimate
- WELL token reward emissions (claimed when > $0.30 threshold) not available in Delta Lab
- The live strategy's HF-based deleveraging during borrow rate spikes is not modeled
- ETH price moves are excluded under the delta-neutral assumption; a small residual exposure exists

**Key health check**: `lido_rate.median() > borrow_rate.median()` — if the spread inverts,
the strategy pays to be leveraged. Spread was positive ~96% of hours during Aug 2025–Mar 2026.

## Testing

```bash
poetry run pytest wayfinder_paths/strategies/moonwell_wsteth_loop_strategy/ -v
```
