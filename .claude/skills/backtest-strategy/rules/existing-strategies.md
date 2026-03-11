# Backtesting Existing Strategies

When backtesting a strategy that already exists in `wayfinder_paths/strategies/`, **do not guess parameters or use generic helpers with defaults**. Read the strategy source code first and faithfully reproduce its signal logic, thresholds, and risk parameters.

## Workflow

### Step 1: Read the strategy source

For every strategy you're backtesting, read these files:

```
wayfinder_paths/strategies/<name>/
├── strategy.py      # REQUIRED — the actual logic (signal generation, thresholds, cooldowns)
├── manifest.yaml    # REQUIRED — adapters, permissions, entrypoint
├── constants.py     # If exists — hardcoded parameters (leverage, thresholds, cooldowns)
├── types.py         # If exists — data structures, enums
├── examples.json    # Test data and expected parameter ranges
```

Extract from the source code:
- **Signal logic**: How does the strategy decide when to enter/exit/rotate? (e.g. bootstrap tournament, rolling average, funding threshold, health factor)
- **Cooldowns**: Minimum time between rotations/rebalances (e.g. `ROTATION_COOLDOWN_HOURS = 168`)
- **Thresholds**: Minimum improvement to trigger action (e.g. `MIN_APY_IMPROVEMENT = 0.01`, hysteresis z-scores)
- **Leverage**: Exact leverage used, health factor targets, max leverage limits
- **Symbols and venues**: Which tokens, which protocols, which chains
- **Filters**: TVL minimums, dust APY thresholds, liquidity requirements
- **Risk parameters**: Maintenance margin, liquidation buffers, max drawdown limits

### Step 2: Fetch real data from Delta Lab

**Never hardcode rate estimates.** Use the backtesting data helpers to get real historical data:

```python
# Discover available venues for the strategy's token
rates = await fetch_lending_rates("USDC", start, end)
print(rates["supply"].columns.tolist())  # Find the exact venue keys

# Fetch real supply/borrow rates for the strategy's specific venues
rates = await fetch_lending_rates("USDC", start, end, venues=["moonwell-base"])
supply_rates = rates["supply"]  # Real hourly APR data
borrow_rates = rates["borrow"]  # Real hourly APR data

# Fetch real prices
prices = await fetch_prices(["ETH", "HYPE", "wstETH"], start, end, "1h")

# Fetch real funding rates for perp strategies
funding = await fetch_funding_rates(["BTC", "ETH"], start, end)
```

If a specific data point isn't in Delta Lab (e.g. WELL reward emissions, staking yields for exotic tokens), say so explicitly and document the estimate with a comment explaining the source.

### Step 3: Reproduce the strategy's signal logic

Use `run_backtest` directly (not the simplified helpers) when the strategy has custom logic that the helpers don't capture.

**The generic helpers are shortcuts for generic strategies.** An existing strategy's backtest should mirror its actual decision function:

| Strategy pattern | Wrong approach | Right approach |
|---|---|---|
| Yield rotation with cooldown | `backtest_yield_rotation(lookback=7)` | Build target positions with cooldown enforcement |
| Delta-neutral with dynamic coin selection | `backtest_delta_neutral(["BTC", "ETH"])` | Implement the coin scoring/selection algorithm |
| Leveraged loop with health factor | Fixed leverage estimate | Bake dynamic leverage from real borrow rates into synthetic price |
| LP with recentering logic | `backtest_lp_position(fee=0.05)` | Model range exits and recentering events |

### Step 4: Configure BacktestConfig from strategy parameters

Map the strategy's risk parameters to `BacktestConfig`:

```python
# Read these from the strategy's constants.py / strategy.py, not defaults
config = BacktestConfig(
    fee_rate=0.0,              # 0.0 for yield strategies (silent-zero gotcha)
    slippage_rate=0.0,         # 0.0 for yield strategies
    leverage=1.0,              # Bake leverage into synthetic price for yield
    enable_liquidation=True,   # True for leveraged strategies
    maintenance_margin_rate=X, # From strategy's health factor target
    periods_per_year=8760,     # Hourly data
)
```

---

## Example: faithful yield rotation with cooldown

This pattern applies to strategies like `hyperlend_stable_yield_strategy` and `stablecoin_yield_strategy` that have rotation cooldowns and improvement thresholds.

```python
from wayfinder_paths.core.backtesting import (
    build_yield_index, fetch_lending_rates, run_backtest, BacktestConfig,
)

# --- Parameters extracted from strategy source code ---
LOOKBACK_HOURS = 7 * 24          # from strategy's signal window
ROTATION_COOLDOWN_HOURS = 168    # from constants.py
MIN_APY_IMPROVEMENT = 0.01       # from constants.py (1% minimum edge)
MIN_TVL = 1_000_000              # from strategy filtering logic
VENUES = ["moonwell-base", "aave-v3-base", "morpho_base"]  # from manifest/strategy

# --- Fetch real data ---
rates = await fetch_lending_rates("USDC", start, end, venues=VENUES)
supply_rates = rates["supply"].ffill().bfill().fillna(0)
supply_rates = supply_rates.loc[:, (supply_rates != 0).any()]

prices = build_yield_index(supply_rates, periods_per_year=8760)

# --- Reproduce signal logic with cooldown + improvement threshold ---
rolling_avg = supply_rates.rolling(LOOKBACK_HOURS, min_periods=1).mean()
target = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)

current_venue = None
last_switch_idx = -ROTATION_COOLDOWN_HOURS  # Allow first switch immediately

for i, ts in enumerate(prices.index):
    best = rolling_avg.loc[ts].idxmax()
    best_rate = rolling_avg.loc[ts, best]

    if current_venue is None:
        # First allocation
        current_venue = best
        last_switch_idx = i
    elif best != current_venue and (i - last_switch_idx) >= ROTATION_COOLDOWN_HOURS:
        # Check improvement threshold
        current_rate = rolling_avg.loc[ts, current_venue]
        if best_rate - current_rate >= MIN_APY_IMPROVEMENT:
            current_venue = best
            last_switch_idx = i

    target.loc[ts, current_venue] = 1.0

config = BacktestConfig(
    fee_rate=0.0, slippage_rate=0.0,
    enable_liquidation=False, periods_per_year=8760,
)
result = run_backtest(prices, target, config)
```

## Example: faithful leveraged yield loop

For strategies like `moonwell_wsteth_loop_strategy` that use collateral looping with real borrow rates:

```python
# --- Parameters from strategy source ---
LEVERAGE = 2.0                   # from health factor target (e.g. HF=1.25 → ~2x)
HEALTH_FACTOR_TARGET = 1.25      # from constants
MAINTENANCE_MARGIN = 1.0 / HEALTH_FACTOR_TARGET  # ≈ 0.80

# --- Fetch real data (not estimates) ---
eth_prices = await fetch_prices(["ETH"], start, end, "1h")
rates = await fetch_lending_rates("USDC", start, end, venues=["moonwell-base"])
borrow_rates = rates["borrow"]["moonwell-base"].ffill().bfill().fillna(0)

# For staking yield: fetch from Delta Lab if available
# If not available, document the source of the estimate
wsteth_rates = await fetch_supply_rates(["wstETH"], start, end)
staking_yield = wsteth_rates.get("wstETH", pd.Series(0.035, index=eth_prices.index))

eth_prices, borrow_df = await align_dataframes(eth_prices, pd.DataFrame({"borrow": borrow_rates}))

# --- Build synthetic leveraged price (bake leverage in, don't use config.leverage) ---
strategy_returns = (
    eth_prices["ETH"].pct_change().fillna(0) * LEVERAGE
    + (staking_yield / 8760) * LEVERAGE        # staking yield on full position
    - (borrow_df["borrow"] / 8760) * (LEVERAGE - 1)  # real borrow cost
)

prices = pd.DataFrame({"wstETH_LOOP": (1 + strategy_returns).cumprod()})
target = pd.DataFrame({"wstETH_LOOP": 1.0}, index=prices.index)

config = BacktestConfig(
    fee_rate=0.0, slippage_rate=0.0,
    leverage=1.0,  # Already baked into synthetic price
    enable_liquidation=True,
    maintenance_margin_rate=MAINTENANCE_MARGIN,
    periods_per_year=8760,
)
result = run_backtest(prices, target, config)
```

## Example: faithful delta-neutral with dynamic coin selection

For strategies like `basis_trading_strategy` that dynamically select coins based on funding analysis:

```python
# --- Parameters from strategy source ---
MAX_LEVERAGE = 2.0
LOOKBACK_DAYS = 30
FUNDING_THRESHOLD = 0.0001  # From strategy's entry criteria

# --- Fetch data for all candidate symbols ---
symbols = ["BTC", "ETH", "SOL", "DOGE", "HYPE"]  # From strategy's universe
prices = await fetch_prices(symbols, start, end, "1h")
funding = await fetch_funding_rates(symbols, start, end)
prices, funding = await align_dataframes(prices, funding, method="ffill")

# --- Reproduce coin selection logic ---
# e.g. rolling funding analysis, bootstrap scoring, volatility filter
rolling_funding = funding.rolling(LOOKBACK_DAYS * 24, min_periods=24).mean()

# Select top N coins by trailing funding (strategy-specific selection logic)
target_perp = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
target_spot = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)

for ts in prices.index:
    candidates = rolling_funding.loc[ts].dropna()
    # Only enter coins with positive funding above threshold
    good = candidates[candidates > FUNDING_THRESHOLD].nlargest(3)
    if len(good) > 0:
        weight = -0.5 / len(good)  # Equal weight short perp
        for sym in good.index:
            target_perp.loc[ts, sym] = weight
            target_spot.loc[ts, sym] = -weight  # Long spot hedge

# ... then build combined prices/targets and run_backtest
```

---

## Checklist

Before running the backtest, verify:

- [ ] Read `strategy.py`, `constants.py`, `manifest.yaml` for the strategy
- [ ] All rates/yields fetched from Delta Lab (not hardcoded estimates)
- [ ] Signal logic matches the strategy's actual decision function
- [ ] Cooldowns/hysteresis/thresholds enforced in target position generation
- [ ] Leverage baked into synthetic price (not `config.leverage`) for yield strategies
- [ ] `fee_rate=0.0` and `slippage_rate=0.0` for yield/lending strategies
- [ ] `enable_liquidation` matches strategy type (True for leveraged, False for supply-only)
- [ ] Venue keys include chain suffix (e.g. `moonwell-base`, not `moonwell`)
- [ ] Any estimated parameters clearly documented with source/reasoning
- [ ] After entry, portfolio value ≈ initial deposit minus fees (no phantom PnL from bookkeeping errors)
