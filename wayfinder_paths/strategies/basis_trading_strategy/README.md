# Basis Trading Strategy

Delta-neutral basis trading on Hyperliquid for funding rate capture.

- **Module**: `wayfinder_paths.strategies.basis_trading_strategy.strategy.BasisTradingStrategy`
- **Platform**: Hyperliquid
- **Token**: USDC

## Overview

This strategy captures funding rate payments through matched positions:
- **Long Spot**: Buy the underlying asset (e.g., HYPE)
- **Short Perp**: Short the perpetual contract for the same asset

Price movements cancel out, and profit comes from collecting funding payments when longs pay shorts.

## How It Works

### Position Sizing

Given deposit `D` USDC and leverage `L`:
- **Order Size**: `D * (L / (L + 1))`
- **Margin Reserved**: `D / (L + 1)`

Example with $100 deposit at 2x leverage:
- Order size: $66.67 per leg
- Margin: $33.33

### Opportunity Selection

1. **Discovery**: Fetch all Hyperliquid spot and perp metadata; cross-reference to find every asset that has both a spot pair and a perp contract
2. **Liquidity filter**: Drop any coin with open interest < $100k (`DEFAULT_OI_FLOOR`) or 24h notional volume < $100k (`DEFAULT_DAY_VLM_FLOOR`)
3. **Per-coin scoring**: For each liquid candidate and for each leverage level 1–`max_leverage`, run a barrier backtest over the last 30 days of hourly funding + OHLC candle data:
   - Simulates holding the position hour-by-hour, accumulating funding income
   - Uses intrabar **high** (not close) to detect potential stop-outs at 75% of the liquidation distance
   - Accounts for tiered maintenance margin, entry/exit costs, and fee buffer
   - Runs 50 block-bootstrap paths (48h blocks) to stress-test the result
4. **Ranking**: Coins are ranked by `net_apy = net_pnl / deposit / years` across leverage levels; the (coin, leverage) pair with the highest net_apy wins
5. **Rotation cooldown**: Once in a position, won't rotate to a different coin for 14 days (`ROTATION_MIN_INTERVAL_DAYS`)

## Key Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `DEFAULT_MAX_LEVERAGE` | 2 | Maximum leverage allowed |
| `DEFAULT_LOOKBACK_DAYS` | 30 | Days of historical data for scoring |
| `DEFAULT_OI_FLOOR` | $100k | Minimum open interest to consider a coin |
| `DEFAULT_DAY_VLM_FLOOR` | $100k | Minimum 24h notional volume |
| `DEFAULT_FEE_EPS` | 0.003 | Fee buffer (0.3%) added to cost model |
| `DEFAULT_BOOTSTRAP_SIMS` | 50 | Block-bootstrap simulation paths |
| `DEFAULT_BOOTSTRAP_BLOCK_HOURS` | 48 | Block size for bootstrap resampling |
| `LIQUIDATION_REBALANCE_THRESHOLD` | 0.75 | Stop-out at 75% of distance to liquidation |
| `ROTATION_MIN_INTERVAL_DAYS` | 14 | Minimum days between coin rotations |
| `MIN_DEPOSIT_USDC` | 25 | Minimum deposit |

## Adapters Used

- **BalanceAdapter**: Wallet balances, ERC20 transfers
- **LedgerAdapter**: Deposit/withdraw tracking
- **TokenAdapter**: Token metadata
- **HyperliquidAdapter**: Market data, order execution, account state

## Actions

### Analyze

```bash
poetry run python -m wayfinder_paths.run_strategy basis_trading_strategy \
    --action analyze --amount 1000 --config config.json
```

Analyzes opportunities without opening positions.

### Deposit

```bash
poetry run python -m wayfinder_paths.run_strategy basis_trading_strategy \
    --action deposit --main-token-amount 100 --config config.json
```

- Transfers USDC from main wallet to strategy wallet
- Bridges USDC to Hyperliquid via Arbitrum
- Splits between perp margin and spot
- Uses PairedFiller for atomic execution (buy spot + sell perp)
- Places protective orders (stop-loss, limit sell)

### Update

```bash
poetry run python -m wayfinder_paths.run_strategy basis_trading_strategy \
    --action update --config config.json
```

- Checks if position needs rebalancing
- Deploys idle capital via scale-up
- Verifies leg balance (spot ≈ perp)
- Updates stop-loss/limit orders if needed

### Status

```bash
poetry run python -m wayfinder_paths.run_strategy basis_trading_strategy \
    --action status --config config.json
```

### Withdraw

```bash
poetry run python -m wayfinder_paths.run_strategy basis_trading_strategy \
    --action withdraw --config config.json
```

- Cancels all open orders
- Uses PairedFiller to close both legs (sell spot + buy perp)
- Withdraws USDC from Hyperliquid to Arbitrum
- Sends funds back to main wallet

## Risk Factors

1. **Funding Rate Flips**: Rates can turn negative
2. **Liquidation Risk**: High leverage + adverse price movement
3. **Execution Slippage**: Large orders may move the market
4. **Withdrawal Delays**: Hyperliquid withdrawals take ~15-30 minutes
5. **Smart Contract Risk**: Funds are held on Hyperliquid's L1

## Backtesting

### What the live strategy actually does (and what can be approximated)

The live selection pipeline cannot be fully reproduced with the standard backtesting helpers because it requires:
- The **full live Hyperliquid universe** (all spot-perp pairs, not a fixed list)
- **Intrabar high prices** to detect barrier stop-outs (the backtest framework uses close prices only)
- **Tiered maintenance margin tables** per coin from Hyperliquid metadata
- **Entry/exit cost modeling** based on live orderbook depth

The approximation below uses rolling mean funding as the selection signal and `fetch_funding_rates` for whatever symbols Delta Lab has. It captures the dominant driver (positive funding selection) but will overstate performance by not modeling stop-outs and understate the universe breadth.

### Simplified backtest

Two modelling choices keep this approximation realistic:

1. **Step-function target** — weights only change at rotation events (when the active coin switches), not every hour. Without this, the backtester continuously re-hedges the position as prices drift, generating thousands of spurious trades and drastically understating returns by over-charging fees.
2. **`rebalance_threshold=0.10`** — only re-hedge if a weight drifts more than 10% from target. This approximates the real strategy's "open-and-hold" behaviour within a rotation period.

```python
import pandas as pd
from wayfinder_paths.core.backtesting.backtester import run_backtest
from wayfinder_paths.core.backtesting.data import fetch_funding_rates, fetch_prices
from wayfinder_paths.core.backtesting.types import BacktestConfig
from wayfinder_paths.core.backtesting.data import convert_to_spot

# --- Parameters from strategy source ---
LOOKBACK_DAYS = 30              # DEFAULT_LOOKBACK_DAYS
ROTATION_COOLDOWN_HOURS = 14 * 24  # ROTATION_MIN_INTERVAL_DAYS = 14
FUNDING_THRESHOLD = 0.0         # proxy for net_apy > 0; live uses barrier-sim score
MAX_LEVERAGE = 2                # DEFAULT_MAX_LEVERAGE
# Position sizing: order_size = D * L/(L+1) — matches live strategy leg sizing
LEG_WEIGHT = MAX_LEVERAGE / (MAX_LEVERAGE + 1)  # = 2/3 ≈ 0.667

# Fetch symbols individually — fetch_funding_rates requires an explicit list.
# Use the oldest available start date (retention window is ~211 days).
# align_dataframes can fail with many symbols of varying date ranges; use manual reindex.
start, end = "2025-08-13", "2026-03-01"
candidate_symbols = [
    # Large-cap / highest liquidity
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "DOT", "LINK",
    # Mid-cap perps common on HL
    "SUI", "APT", "INJ", "TIA", "ATOM", "NEAR", "ARB", "OP", "WLD",
    "LDO", "IMX", "RNDR", "FIL", "UNI", "AAVE", "MKR", "SNX", "CRV",
    # HL-native / meme
    "HYPE", "PURR",
    # Alts with historically elevated funding on HL
    "DYDX", "BLUR", "WIF", "JUP", "SEI", "ORDI",
    "ETHFI", "PEOPLE", "LIT", "FARTCOIN", "GRIFFAIN", "XMR",
]
series_list = []
symbols = []
for sym in candidate_symbols:
    try:
        df = await fetch_funding_rates([sym], start, end)
        if not df.empty and sym in df.columns:
            series_list.append(df[sym])
            symbols.append(sym)
    except Exception:
        pass

perp_funding = pd.concat(series_list, axis=1)
perp_prices = await fetch_prices(symbols, start, end, "1h")

# Use price index as master; reindex funding to it (avoids align_dataframes failures
# with many symbols of varying date ranges).
perp_prices = perp_prices.sort_index()
perp_funding = perp_funding.reindex(perp_prices.index).ffill().bfill()

# Drop symbols missing from price data or with entirely NaN prices.
valid_symbols = [s for s in symbols if s in perp_prices.columns and perp_prices[s].notna().any()]
perp_prices = perp_prices[valid_symbols].ffill().bfill()
perp_funding = perp_funding[valid_symbols].fillna(0.0)
symbols = valid_symbols

# Build spot leg (same prices, zero funding — price P&L cancels exactly)
spot_prices, spot_funding_zeros = convert_to_spot(perp_prices)
all_prices = pd.concat(
    [perp_prices.add_suffix("_PERP"), spot_prices.add_suffix("_SPOT")], axis=1
)
all_funding = pd.concat(
    [perp_funding.add_suffix("_PERP"), spot_funding_zeros.add_suffix("_SPOT")], axis=1
).fillna(0.0)

rolling_funding = perp_funding.rolling(LOOKBACK_DAYS * 24, min_periods=24).mean()

# --- Build rotation events (step-function target) ---
# Record the timestamp each time the active coin changes. Forward-filling these
# events gives a target that is constant between rotations, so the backtester
# does not re-hedge intra-period.
rotation_events: dict[pd.Timestamp, str | None] = {}
current_coin: str | None = None
last_switch_idx = -ROTATION_COOLDOWN_HOURS

for i, ts in enumerate(all_prices.index):
    row = rolling_funding.loc[ts].dropna()
    best = str(row.idxmax()) if not row.empty else None

    if best is None:
        if current_coin is not None:
            rotation_events[ts] = None   # go flat
            current_coin = None
        continue

    if current_coin is None:
        current_coin = best
        last_switch_idx = i
        rotation_events[ts] = best
    elif best != current_coin and (i - last_switch_idx) >= ROTATION_COOLDOWN_HOURS:
        if row[best] > FUNDING_THRESHOLD:
            current_coin = best
            last_switch_idx = i
            rotation_events[ts] = best

coin_at_ts: pd.Series = pd.Series(None, index=all_prices.index, dtype=object)
for ts, coin in rotation_events.items():
    coin_at_ts.loc[ts] = coin
coin_at_ts = coin_at_ts.ffill()

target = pd.DataFrame(0.0, index=all_prices.index, columns=all_prices.columns)
for ts in all_prices.index:
    coin = coin_at_ts.loc[ts]
    if pd.notna(coin):
        target.loc[ts, f"{coin}_SPOT"] = LEG_WEIGHT
        target.loc[ts, f"{coin}_PERP"] = -LEG_WEIGHT

config = BacktestConfig(
    # leverage=1.0: LEG_WEIGHT already encodes the L/(L+1) position sizing.
    leverage=1.0,
    fee_rate=0.0004,            # ~4bps round-trip
    slippage_rate=0.0002,
    funding_rates=all_funding,
    enable_liquidation=False,   # delta-neutral; price P&L cancels
    rebalance_threshold=0.10,   # only re-hedge if weight drifts >10%
    periods_per_year=8760,
)
result = run_backtest(all_prices, target, config)
print(f"Total return:   {result.stats['total_return']:.2%}")
print(f"Sharpe:         {result.stats['sharpe']:.2f}")
print(f"Max drawdown:   {result.stats['max_drawdown']:.2%}")
print(f"Funding income: {result.stats.get('total_funding', 0):.2%}")
print(f"Trade count:    {result.stats.get('trade_count', 0)}")
```

**Sample results** (2025-08-13 → 2026-03-01, 41 symbols, 9 rotations):

| Metric | Value |
|--------|-------|
| Total return | 6.31% |
| Sharpe | 14.36 |
| Max drawdown | -0.68% |
| Funding income | -8.03% (received) |
| Trade count | 84 |

**Known gaps vs. live strategy**:
- Live strategy uses intrabar **high** to check for stop-outs at 75% of liquidation distance; this backtest uses close-price-only checks
- Live strategy ranks by `net_apy` from a full barrier simulation; this backtest uses simple rolling mean funding as a proxy
- Live universe is wider (all HL spot-perp pairs); Delta Lab may not have every coin
- Entry/exit costs in the live strategy are modeled from live orderbook depth per coin

**Key health checks**: `total_funding <= 0` (income received), `liquidated=False`, `max_drawdown > -0.20`, `trade_count` ≈ 2× number of rotations

## Testing

```bash
poetry run pytest wayfinder_paths/strategies/basis_trading_strategy/ -v
```
