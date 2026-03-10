"""
LP (liquidity provider) strategy helpers for backtesting.

Models 50/50 constant-product AMM liquidity provision (Uniswap V2 style)
by combining impermanent loss and fee income into a synthetic price index
that can be fed directly to run_backtest().

Key insight: LP P&L = hold_value * IL_factor * cumulative_fee_multiplier
where IL_factor <= 1.0 always (IL is always a loss vs holding).

Limitations:
- Models 50/50 constant-product pools only (not Uniswap V3 concentrated liquidity)
- Fee income rate must be estimated externally (e.g. from historical pool analytics)
- Does not model gas costs of entering/exiting liquidity positions
- Concentrated liquidity (V3) has amplified IL within range; use carefully
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from wayfinder_paths.core.backtesting.backtester import run_backtest
from wayfinder_paths.core.backtesting.types import BacktestConfig, BacktestResult


def simulate_il(
    prices: pd.DataFrame,
    pool_assets: tuple[str, str],
) -> pd.Series:
    """
    Calculate impermanent loss over time for a 50/50 constant-product AMM pool.

    IL formula: 2*sqrt(r)/(1+r) - 1
    where r = (P_a(t)/P_b(t)) / (P_a(0)/P_b(0))

    IL is zero at the initial price ratio and negative otherwise (it is always
    a loss relative to simply holding both tokens).

    Args:
        prices: DataFrame with columns for both pool assets
        pool_assets: (asset_a, asset_b) — column names in prices

    Returns:
        Series of IL values (always ≤ 0)
        IL of -0.05 means LP position is worth 5% less than holding both tokens.

    Notes:
        - IL is zero when the price ratio returns to its initial value
        - For stable/stable pools (e.g. USDC/USDT), IL is near zero (ratios barely move)
        - For volatile pairs (e.g. ETH/USDC), IL can be significant (10-30%+ over months)
        - Concentrated liquidity (Uniswap V3) has higher IL within range

    Example:
        >>> prices = await fetch_prices(["ETH", "USDC"], "2025-08-01", "2026-01-01")
        >>> il = simulate_il(prices, ("ETH", "USDC"))
        >>> print(f"Max IL: {il.min():.2%}")  # Worst IL point
        >>> print(f"Current IL: {il.iloc[-1]:.2%}")
    """
    asset_a, asset_b = pool_assets
    price_ratio = prices[asset_a] / prices[asset_b]
    initial_ratio = float(price_ratio.iloc[0])
    r = price_ratio / initial_ratio
    il = 2 * np.sqrt(r) / (1 + r) - 1
    return il.rename(f"IL_{asset_a}_{asset_b}")


def build_lp_price_index(
    prices: pd.DataFrame,
    pool_assets: tuple[str, str],
    fee_income_rate: float,
    periods_per_year: int = 8760,
) -> pd.DataFrame:
    """
    Build a synthetic LP position price index combining impermanent loss and fee income.

    LP position value = hold_value * (1 + IL) * cumulative_fee_multiplier

    where:
    - hold_value = equal-weight hold of both assets (normalized to 1.0 at start)
    - IL ≤ 0: IL factor reduces LP value relative to holding
    - cumulative_fee_multiplier: fee APY compounded each period

    The output is a single-column DataFrame usable as `prices` in run_backtest().
    Set target_positions to 1.0 (fully invested) and enable_liquidation=False.

    Args:
        prices: DataFrame containing both pool assets as columns
        pool_assets: (asset_a, asset_b) column names in prices
        fee_income_rate: Annualized fee income APY (e.g. 0.30 = 30% APY from fees).
                         Estimate from pool analytics (TVL, 24h volume → fee_rate * volume / TVL * 365)
        periods_per_year: Granularity of price data (8760 for 1h, 365 for 1d)

    Returns:
        DataFrame with single column "LP_{asset_a}_{asset_b}"

    Usage:
        Pass as `prices` to run_backtest() with a constant target weight of 1.0.
        The backtest will show whether fee income outweighs impermanent loss.

    Example:
        >>> prices = await fetch_prices(["ETH", "USDC"], "2025-08-01", "2026-01-01")
        >>> lp_prices = build_lp_price_index(prices, ("ETH", "USDC"), fee_income_rate=0.25)
        >>> target = pd.DataFrame({"LP_ETH_USDC": 1.0}, index=lp_prices.index)
        >>> config = BacktestConfig(enable_liquidation=False, periods_per_year=8760)
        >>> result = run_backtest(lp_prices, target, config)
        >>> print(f"Net LP return: {result.stats['total_return']:.2%}")
    """
    asset_a, asset_b = pool_assets

    # Normalize both assets to 1.0 at start
    p_a = prices[asset_a] / float(prices[asset_a].iloc[0])
    p_b = prices[asset_b] / float(prices[asset_b].iloc[0])

    # 50/50 hold value (normalized)
    hold_value = (p_a + p_b) / 2

    # IL factor: how much LP underperforms holding (1 + IL, always ≤ 1.0)
    il_series = simulate_il(prices, pool_assets)
    lp_vs_hold = 1 + il_series

    # Cumulative fee multiplier: fees compound each period
    fee_per_period = fee_income_rate / periods_per_year
    fee_multiplier = pd.Series(
        (1 + fee_per_period) ** np.arange(len(prices)),
        index=prices.index,
    )

    lp_value = hold_value * lp_vs_hold * fee_multiplier
    col_name = f"LP_{asset_a}_{asset_b}"
    return pd.DataFrame({col_name: lp_value})


async def backtest_lp_position(
    pool_assets: tuple[str, str],
    start_date: str,
    end_date: str,
    fee_income_rate: float,
    interval: str = "1h",
) -> BacktestResult:
    """
    End-to-end LP strategy backtest: hold a 50/50 AMM position for the full period.

    Args:
        pool_assets: (asset_a, asset_b) symbols (e.g. ("ETH", "USDC"))
        start_date: Start date ("YYYY-MM-DD", oldest ~Aug 2025)
        end_date: End date ("YYYY-MM-DD")
        fee_income_rate: Annualized fee APY (e.g. 0.25 = 25% from trading fees).
                         Look this up from pool analytics dashboards.
        interval: Price data interval (default "1h")

    Returns:
        BacktestResult. Key interpretation:
        - total_return > 0: fees more than compensated for IL
        - total_return < 0: IL exceeded fee income (net loss vs cash)
        - Compare against buy_hold_return to understand IL drag

    Example:
        >>> result = await backtest_lp_position(
        ...     ("ETH", "USDC"), "2025-08-01", "2026-01-01", fee_income_rate=0.25
        ... )
        >>> print(f"LP return: {result.stats['total_return']:.2%}")
        >>> print(f"Hold return: {result.stats['buy_hold_return']:.2%}")
    """
    from wayfinder_paths.core.backtesting.data import fetch_prices

    interval_to_periods = {
        "1m": 365 * 24 * 60,
        "5m": 365 * 24 * 12,
        "15m": 365 * 24 * 4,
        "1h": 365 * 24,
        "4h": 365 * 6,
        "1d": 365,
    }
    periods_per_year = interval_to_periods.get(interval, 8760)

    prices = await fetch_prices(list(pool_assets), start_date, end_date, interval)
    lp_prices = build_lp_price_index(
        prices, pool_assets, fee_income_rate, periods_per_year
    )

    col = f"LP_{pool_assets[0]}_{pool_assets[1]}"
    target = pd.DataFrame({col: 1.0}, index=lp_prices.index)

    config = BacktestConfig(
        fee_rate=0.0,  # Entry/exit cost modeled separately if needed
        enable_liquidation=False,
        periods_per_year=periods_per_year,
    )
    return run_backtest(lp_prices, target, config)
