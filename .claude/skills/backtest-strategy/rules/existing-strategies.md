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

## Strategy-specific backtest guides

Each strategy README has a **Backtesting** section with concrete parameters, data fetching, and full code:

| Strategy | Pattern | README |
|---|---|---|
| `basis_trading_strategy` | Delta-neutral, dynamic coin selection | `wayfinder_paths/strategies/basis_trading_strategy/README.md` |
| `hyperlend_stable_yield_strategy` | Yield rotation + hysteresis cooldown | `wayfinder_paths/strategies/hyperlend_stable_yield_strategy/README.md` |
| `moonwell_wsteth_loop_strategy` | Leveraged loop (synthetic price) | `wayfinder_paths/strategies/moonwell_wsteth_loop_strategy/README.md` |
| `stablecoin_yield_strategy` | Yield rotation + cooldown | `wayfinder_paths/strategies/stablecoin_yield_strategy/README.md` |
| `boros_hype_strategy` | Basis component only (Boros rates not in Delta Lab) | `wayfinder_paths/strategies/boros_hype_strategy/README.md` |
| `projectx_thbill_usdc_strategy` | LP (V2 approximation for stable pair) | `wayfinder_paths/strategies/projectx_thbill_usdc_strategy/README.md` |

Read the strategy README before running any backtest — parameters, venue names, and data availability caveats are documented there.

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
