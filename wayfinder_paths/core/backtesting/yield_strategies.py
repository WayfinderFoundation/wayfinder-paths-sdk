"""
Yield strategy helpers for backtesting.

Provides end-to-end helpers for:
- Building synthetic price indices from compounded APR rates
- Supply rate rotation (USDC across Aave/Moonwell/Morpho)
- Carry trade (borrow cheap, supply expensive)

All strategies use hourly data (periods_per_year=8760) and synthetic price encoding:
the raw APR is compounded into a cumulative price index, then run through the
standard backtesting engine. This gives meaningful Sharpe/drawdown metrics
for yield strategies that don't have "price" data in the traditional sense.
"""

from __future__ import annotations

import pandas as pd

from wayfinder_paths.core.backtesting.backtester import run_backtest
from wayfinder_paths.core.backtesting.data import fetch_lending_rates
from wayfinder_paths.core.backtesting.types import BacktestConfig, BacktestResult


def build_yield_index(
    rates_df: pd.DataFrame,
    periods_per_year: int = 365,
) -> pd.DataFrame:
    """
    Compound APR rates into a cumulative price index.

    This is the core primitive for yield strategy backtesting: converts a
    DataFrame of APR values (e.g. supply rates per venue) into synthetic
    "prices" whose returns represent the yield earned each period.

    Args:
        rates_df: DataFrame of decimal APR values (index=timestamps, columns=venues/symbols)
                  Values are annual rates: 0.05 = 5% APY, 0.20 = 20% APY
        periods_per_year: Periods in one year. Must match data frequency:
                          8760 for hourly (fetch_lending_rates output), 365 for daily.

    Returns:
        DataFrame of cumulative price indices starting at 1.0 (same shape as rates_df)

    Example:
        >>> rates = await fetch_lending_rates("USDC", "2025-08-01", "2026-01-01")
        >>> prices = build_yield_index(rates["supply"], periods_per_year=8760)
        >>> # prices now has the same shape but values start at 1.0 and grow with yield
    """
    return (1 + rates_df / periods_per_year).cumprod()


async def backtest_yield_rotation(
    symbol: str,
    venues: list[str],
    start_date: str,
    end_date: str,
    lookback_signal_days: int = 7,
    fee_rate: float = 0.0005,
) -> BacktestResult:
    """
    End-to-end yield rotation backtest: rotate capital to the highest-yielding venue.

    Strategy logic:
    - Fetches historical supply APRs per venue via Delta Lab
    - Each period, allocates 100% capital to the venue with the best trailing
      N-day average supply rate
    - Incurs fee_rate on each switch (models gas + any swap cost)

    Args:
        symbol: Asset to supply (e.g. "USDC", "ETH", "WBTC")
        venues: Lending venues to compare (e.g. ["aave", "moonwell", "morpho"])
        start_date: Start date ("YYYY-MM-DD", oldest ~Aug 2025)
        end_date: End date ("YYYY-MM-DD")
        lookback_signal_days: Rolling window for rate signal (default 7).
                              Higher = slower switching, lower = noisier.
        fee_rate: Transaction cost per venue switch (default 5bps = one-way gas+slippage).
                  Set higher for mainnet, lower for L2s.

    Returns:
        BacktestResult — use result.stats['trade_count'] to check switching frequency.
        High trade_count means the strategy is over-switching (gas will dominate).

    Notes:
        - slippage_rate=0.0 (stablecoin deposits have no price impact)
        - enable_liquidation=False (supply-only positions cannot be liquidated)
        - periods_per_year=8760 (lending rates are hourly)

    Example:
        >>> result = await backtest_yield_rotation(
        ...     "USDC", ["aave", "moonwell", "morpho"],
        ...     "2025-08-01", "2026-01-01", lookback_signal_days=7
        ... )
        >>> print(f"Return: {result.stats['total_return']:.2%}")
        >>> print(f"Sharpe: {result.stats['sharpe']:.2f}")
        >>> print(f"Switches: {result.stats['trade_count']}")
    """
    rates = await fetch_lending_rates(symbol, start_date, end_date, venues=venues)
    supply_rates = rates["supply"].reindex(columns=venues).ffill().dropna(how="all")

    prices = build_yield_index(supply_rates, periods_per_year=8760)

    # Data is hourly; convert lookback from days to hours for the rolling window
    rolling_hours = lookback_signal_days * 24
    rolling_avg = supply_rates.rolling(rolling_hours, min_periods=1).mean()
    best_venue = rolling_avg.idxmax(axis=1)

    target = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    for venue in prices.columns:
        target.loc[best_venue == venue, venue] = 1.0

    config = BacktestConfig(
        fee_rate=fee_rate,
        slippage_rate=0.0,
        leverage=1.0,
        enable_liquidation=False,
        periods_per_year=8760,
    )
    return run_backtest(prices, target, config)


async def backtest_carry_trade(
    symbol: str,
    start_date: str,
    end_date: str,
    venues: list[str] | None = None,
    min_spread: float = 0.01,
    fee_rate: float = 0.0005,
) -> BacktestResult:
    """
    End-to-end carry trade backtest: borrow from cheapest venue, supply to most expensive.

    Strategy logic:
    - Fetches supply AND borrow rates per venue
    - Net carry = best_supply_apr - cheapest_borrow_apr
    - Only enters when spread > min_spread (avoids entering for marginal/negative carry)
    - Net carry is baked into a synthetic price series

    Args:
        symbol: Asset to trade (e.g. "USDC", "ETH")
        start_date: Start date ("YYYY-MM-DD", oldest ~Aug 2025)
        end_date: End date ("YYYY-MM-DD")
        venues: Venue filter (None = all available venues in Delta Lab)
        min_spread: Minimum annualized spread to enter (default 0.01 = 1% APR minimum edge)
        fee_rate: Transaction cost when entering/exiting the carry (default 5bps)

    Returns:
        BacktestResult. Key stats:
        - total_return: cumulative net carry earned
        - trade_count: number of entry/exit events
        - exposure_time_pct: fraction of time active (spread was positive)

    Notes:
        - enable_liquidation=False (pure carry with no collateral loop)
        - For leveraged carry (collateral loop), build the synthetic price manually
          using the leveraged loop pattern (see yield-strategies skill rules)

    Example:
        >>> result = await backtest_carry_trade(
        ...     "USDC", "2025-08-01", "2026-01-01",
        ...     venues=["aave", "moonwell", "morpho"], min_spread=0.01
        ... )
        >>> print(f"Return: {result.stats['total_return']:.2%}")
        >>> print(f"Active: {result.stats['exposure_time_pct']:.0%} of periods")
    """
    rates = await fetch_lending_rates(symbol, start_date, end_date, venues=venues)
    supply_df = rates["supply"]
    borrow_df = rates["borrow"]

    common_idx = supply_df.index.intersection(borrow_df.index)
    supply_df = supply_df.loc[common_idx].ffill()
    borrow_df = borrow_df.loc[common_idx].ffill()

    best_supply = supply_df.max(axis=1)
    cheapest_borrow = borrow_df.min(axis=1)
    spread = best_supply - cheapest_borrow

    active = (spread > min_spread).astype(float)
    net_carry_per_hour = spread / 8760 * active
    strategy_price = (1 + net_carry_per_hour).cumprod()

    prices = pd.DataFrame({"carry": strategy_price})
    target = pd.DataFrame({"carry": active}, index=prices.index)

    config = BacktestConfig(
        fee_rate=fee_rate,
        slippage_rate=0.0,
        leverage=1.0,
        enable_liquidation=False,
        periods_per_year=8760,
    )
    return run_backtest(prices, target, config)
