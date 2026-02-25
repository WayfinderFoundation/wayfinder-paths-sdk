"""Multi-leverage backtesting utilities."""

from __future__ import annotations

import pandas as pd

from wayfinder_paths.core.backtesting.backtester import run_backtest
from wayfinder_paths.core.backtesting.types import BacktestConfig, BacktestResult


def run_multi_leverage_backtest(
    prices: pd.DataFrame,
    target_positions: pd.DataFrame,
    leverage_tiers: tuple[float, ...] = (1.0, 2.0, 3.0, 5.0),
    base_config: BacktestConfig | None = None,
) -> dict[str, BacktestResult]:
    """
    Run backtest across multiple leverage levels for comparison.

    Args:
        prices: Price DataFrame
        target_positions: Target position weights DataFrame
        leverage_tiers: Tuple of leverage levels to test
        base_config: Base configuration (leverage will be overridden)

    Returns:
        Dict mapping leverage labels (e.g., "2x") to BacktestResult objects
    """
    if base_config is None:
        base_config = BacktestConfig()

    results = {}
    for lev in leverage_tiers:
        config = BacktestConfig(
            fee_rate=base_config.fee_rate,
            slippage_rate=base_config.slippage_rate,
            holding_cost_rate=base_config.holding_cost_rate,
            min_trade_notional=base_config.min_trade_notional,
            rebalance_threshold=base_config.rebalance_threshold,
            leverage=lev,
            enable_liquidation=base_config.enable_liquidation,
            maintenance_margin_rate=base_config.maintenance_margin_rate,
            maintenance_margin_by_symbol=base_config.maintenance_margin_by_symbol,
            liquidation_buffer=base_config.liquidation_buffer,
            initial_capital=base_config.initial_capital,
            periods_per_year=base_config.periods_per_year,
            funding_rates=base_config.funding_rates,
        )
        result = run_backtest(prices, target_positions, config)
        label = f"{int(lev)}x" if float(lev).is_integer() else f"{lev:g}x"
        results[label] = result

    return results
