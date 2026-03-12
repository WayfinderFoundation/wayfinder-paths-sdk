# Backtesting Yield & Lending Strategies

Lending/yield strategies use **synthetic price indices** built from compounded APR data.
The engine is agnostic — Sharpe/drawdown math applies the same way.

## Core primitive

```python
from wayfinder_paths.core.backtesting import build_yield_index, fetch_lending_rates

# Discover venue names first (keys include chain suffix: "moonwell-base", "aave-v3-base")
rates = await fetch_lending_rates("USDC", start, end)  # no venues filter
venues = rates["supply"].columns.tolist()              # e.g. ['aave-v3-base', 'moonwell-base', ...]

# Then filter to the ones you want:
rates = await fetch_lending_rates("USDC", start, end, venues=["aave-v3-base", "moonwell-base"])
# rates["supply"], rates["borrow"] — both DataFrame[timestamp × venue]

prices = build_yield_index(rates["supply"], periods_per_year=8760)
# starts at 1.0, grows with yield; 8760 because lending data is hourly
```

Use `fetch_lending_rates` (per-venue) for rotation/carry. Use `fetch_supply_rates` / `fetch_borrow_rates` for simple symbol-level averages.

## Pattern 1: Supply rate rotation → `backtest_yield_rotation()`

```python
# venues=None uses all available venues; filter after discovering names
result = await backtest_yield_rotation(
    "USDC", ["aave-v3-base", "moonwell-base"], start, end,
    lookback_signal_days=7,  # Higher = fewer switches
    fee_rate=0.0005,          # Gas per switch; ~0.002 on mainnet
)
# trade_count > 20 → increase lookback — gas will dominate
```

Manual (for custom signals):
```python
prices = build_yield_index(rates["supply"])
best = rates["supply"].rolling(7, min_periods=1).mean().idxmax(axis=1)
target = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
for v in prices.columns:
    target.loc[best == v, v] = 1.0
config = BacktestConfig(fee_rate=0.0005, slippage_rate=0.0, enable_liquidation=False, periods_per_year=8760)
result = run_backtest(prices, target, config)
```

## Pattern 2: Leveraged yield (collateral loop)

Bake leverage into the synthetic price series — don't use `config.leverage`:

```python
eth_prices, borrow_rates = await align_dataframes(
    await fetch_prices(["ETH"], start, end),
    await fetch_borrow_rates(["USDC"], start, end, protocol="aave"),
)
leverage = 2.0
strategy_returns = (
    eth_prices["ETH"].pct_change().fillna(0) * leverage
    - (borrow_rates["USDC"] / 365) * (leverage - 1)
)
prices = pd.DataFrame({"ETH_LOOP": (1 + strategy_returns).cumprod()})
target = pd.DataFrame({"ETH_LOOP": 1.0}, index=prices.index)
config = BacktestConfig(
    fee_rate=0.001, leverage=1.0, enable_liquidation=True,
    maintenance_margin_rate=0.15, periods_per_year=8760,
)
result = run_backtest(prices, target, config)
# result.liquidated → True means the strategy blew up
```

## Pattern 3: Carry trade → `backtest_carry_trade()`

```python
result = await backtest_carry_trade(
    "USDC", start, end, venues=["aave-v3-base", "moonwell-base"], min_spread=0.01,
)
# exposure_time_pct = fraction of time the spread exceeded min_spread
```

## Config for yield strategies

Always use: `slippage_rate=0.0`, `enable_liquidation=False` (supply-only), `periods_per_year=8760` (lending data is hourly)

## What doesn't work

- `funding_rates` config: bake borrow costs into synthetic price instead
- Negative weights: lending is long-only
- Reward token APR: may not be in Delta Lab historical data — add manually if material

## Carry trade accuracy caveat

`backtest_carry_trade` computes `best_supply_rate - cheapest_borrow_rate` across venues. This spread is **only achievable if you already have collateral deployed** — borrowing requires over-collateralization and the collateral's price risk is not modeled. Treat the result as an upper bound. For a realistic model, bake collateral price exposure into the synthetic price series using the leveraged loop pattern.
