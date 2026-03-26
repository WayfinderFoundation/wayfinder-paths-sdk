# ProjectX THBILL/USDC Strategy

Concentrated-liquidity market making on ProjectX (HyperEVM) for the THBILL/USDC stable pair.

- Pulls USDC (HyperEVM) from `main` wallet into the strategy wallet
- Swaps to the optimal THBILL/USDC split
- Mints or adds liquidity to a tight band around the current tick
- Periodically collects fees, compounds them back into liquidity, and recenters if out of range
- Surfaces ProjectX/Theo points (when available) in `status`

## Backtesting

### Why this strategy is hard to backtest

- **THBILL is not in Delta Lab**: THBILL (`theo-short-duration-us-treasury-fund-hyperevm`) is a HyperEVM-native T-bill token. `fetch_prices(["THBILL"])` will not return data.
- **IL is essentially zero**: THBILL ≈ $1 and never deviates far from USDC. The ±0.20% band (`band_bps = 40.0`) is rarely exited.
- **The V2 full-range LP model is a very conservative lower bound**: Because the price never leaves the tight band, fee capture per unit of TVL in V3 is far higher than V2 full-range would model.

The dominant unknown is **fee income**, which depends on trading volume in the pool.

### Step 1: Estimate fee_income_rate from live pool data

The strategy computes fee APY from `pool_overview()` (fee tier) and `fetch_swaps()` (24h subgraph volume).
There is no single `get_pool_info()` method — use the two calls below:

```python
import time
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.projectx_adapter.adapter import ProjectXLiquidityAdapter

# Pool address is already wired into the strategy wallet config; pass it explicitly for standalone use
from wayfinder_paths.core.constants.projectx import THBILL_USDC_POOL
adapter = await get_adapter(ProjectXLiquidityAdapter)
adapter.pool_address = THBILL_USDC_POOL  # set if not already configured

# 1. Fee tier
ok, overview = await adapter.pool_overview()
if not ok:
    raise RuntimeError(f"pool_overview: {overview}")
fee_rate = int(overview["fee"]) / 1_000_000  # e.g. fee=500 → 0.0005 (0.05%)

# 2. 24h swap volume via subgraph (up to 1000 swaps, same window as live strategy)
end_ts = int(time.time())
swaps_ok, swaps = await adapter.fetch_swaps(limit=1000, start_timestamp=end_ts - 86400, end_timestamp=end_ts)
if not swaps_ok:
    raise RuntimeError(f"fetch_swaps: {swaps}")
volume_24h = sum(abs(float(s.get("amount_usd") or 0)) for s in swaps if s.get("amount_usd"))

# 3. Pool TVL — not returned by pool_overview(); read it from strategy status output
#    or supply manually (check `run_strategy status` → strategy_status.pool_tvl_usd)
pool_tvl_usd = 500_000  # <-- replace with actual TVL from status
fee_income_rate = volume_24h * fee_rate * 365 / max(pool_tvl_usd, 1)
print(f"Fee rate:         {fee_rate:.4%}")
print(f"Volume 24h:       ${volume_24h:,.0f}")
print(f"Pool TVL (est.):  ${pool_tvl_usd:,.0f}")
print(f"Estimated APY:    {fee_income_rate:.2%}")
```

### Step 2: Compute stats from constant hourly return

Since THBILL ≈ $1 and is not in Delta Lab, IL is negligible and there is no price history to simulate against. Compute stats directly from a constant hourly return:

```python
import numpy as np
import pandas as pd

# Replace fee_income_rate with the value from Step 1
PERIODS_PER_YEAR = 8760
start, end = "2025-08-13", "2026-03-01"  # oldest safe Delta Lab date

idx = pd.date_range(start, end, freq="1h")
hourly_ret = pd.Series(fee_income_rate / PERIODS_PER_YEAR, index=idx)

cumret = (1 + hourly_ret).cumprod()
total = float(cumret.iloc[-1]) - 1.0
years = len(hourly_ret) / PERIODS_PER_YEAR
cagr = float((1 + total) ** (1 / years) - 1)
vol = float(hourly_ret.std(ddof=0)) * np.sqrt(PERIODS_PER_YEAR)
sharpe = cagr / vol if vol > 0 else float("inf")  # vol ≈ 0 for constant APY
mdd = 0.0  # no drawdown for constant positive return

print(f"LP return (ann.): {cagr:.2%}")
print(f"Total return:     {total:.2%}")
print(f"Max drawdown:     {mdd:.2%}")
```

**Known gaps vs. live strategy**:
- This model distributes fee income uniformly over time; the live V3 strategy only earns fees while price is inside the ±0.20% band (which is most of the time for a stable pair)
- Theo points (from THBILL volume) are not captured in the return
- Recentering events (when price exits the band) briefly take the position out of fee-earning range — this is rare but not modeled
