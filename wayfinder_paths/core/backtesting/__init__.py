"""
Backtesting framework for strategy development and validation.

Provides simple interfaces for running backtests with realistic costs,
automatic data fetching, and comprehensive performance metrics.

## Strategy types and which helpers to use

### Perp/spot momentum or trend-following
    >>> result = await quick_backtest(strategy_fn, symbols, start, end, leverage=2.0)

### Delta-neutral basis carry (long spot + short perp, harvest funding)
    >>> result = await backtest_delta_neutral(["BTC", "ETH"], start, end, funding_threshold=0.0001)

### Lending yield rotation (USDC across Aave/Moonwell/Morpho)
    >>> result = await backtest_yield_rotation("USDC", ["aave", "moonwell", "morpho"], start, end)

### Carry trade (borrow cheap, supply expensive)
    >>> result = await backtest_carry_trade("USDC", start, end, min_spread=0.01)

### LP / AMM yield (fee income vs impermanent loss)
    >>> result = await backtest_lp_position(("ETH", "USDC"), start, end, fee_income_rate=0.25)

### Manual workflow (full control over signals and config)
    >>> prices = await fetch_prices(["BTC", "ETH"], start, end)
    >>> funding = await fetch_funding_rates(["BTC", "ETH"], start, end)
    >>> config = BacktestConfig(leverage=2.0, funding_rates=funding)
    >>> result = run_backtest(prices, target_positions, config)

## Data availability
Oldest available: ~August 2025 (Delta Lab + Hyperliquid retain ~7 months).
"""

from wayfinder_paths.core.backtesting.backtester import run_backtest
from wayfinder_paths.core.backtesting.data import (
    align_dataframes,
    convert_to_spot,
    fetch_borrow_rates,
    fetch_funding_rates,
    fetch_lending_rates,
    fetch_prices,
    fetch_supply_rates,
    get_available_date_range,
    validate_date_range,
)
from wayfinder_paths.core.backtesting.helpers import (
    backtest_delta_neutral,
    backtest_with_rates,
    quick_backtest,
)
from wayfinder_paths.core.backtesting.lp_strategies import (
    backtest_lp_position,
    build_lp_price_index,
    simulate_il,
)
from wayfinder_paths.core.backtesting.multi import run_multi_leverage_backtest
from wayfinder_paths.core.backtesting.types import (
    BacktestConfig,
    BacktestResult,
    BacktestStats,
)
from wayfinder_paths.core.backtesting.yield_strategies import (
    backtest_carry_trade,
    backtest_yield_rotation,
    build_yield_index,
)

__all__ = [
    # Core engine
    "run_backtest",
    "run_multi_leverage_backtest",
    "BacktestConfig",
    "BacktestResult",
    "BacktestStats",
    # End-to-end strategy helpers
    "quick_backtest",  # Perp/spot momentum (auto data fetch)
    "backtest_with_rates",  # Perp/spot with explicit funding signal
    "backtest_delta_neutral",  # Long spot + short perp, harvest funding
    "backtest_yield_rotation",  # Rotate across lending venues by supply APR
    "backtest_carry_trade",  # Borrow cheap + supply expensive (net carry)
    "backtest_lp_position",  # 50/50 AMM LP (fee income vs IL)
    # Synthetic price primitives
    "build_yield_index",  # Compound APR rates into price index
    "build_lp_price_index",  # LP value = hold * IL_factor * fee_multiplier
    "simulate_il",  # Impermanent loss series for AMM pool
    # Data fetching
    "fetch_prices",
    "fetch_funding_rates",
    "fetch_borrow_rates",
    "fetch_supply_rates",  # Supply APRs averaged across venues
    "fetch_lending_rates",  # Supply + borrow APRs per venue (for rotation/carry)
    "get_available_date_range",
    "validate_date_range",
    # Utilities
    "convert_to_spot",  # Spot leg (same prices, zero funding)
    "align_dataframes",
]
