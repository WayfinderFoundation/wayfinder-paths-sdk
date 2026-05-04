"""
Carry trade strategy example.

Borrow USDC from the cheapest lending venue, supply to the most expensive.
Net carry = best_supply_APR - cheapest_borrow_APR.
Only enters when the spread exceeds a minimum threshold.
"""

from __future__ import annotations

from wayfinder_paths.core.backtesting.yield_strategies import backtest_carry_trade


async def main() -> None:
    # Venue keys include chain suffix — use fetch_lending_rates("USDC", start, end)
    # and print(rates["supply"].columns.tolist()) to discover available names.
    result = await backtest_carry_trade(
        symbol="USDC",
        start_date="2025-08-01",
        end_date="2026-01-01",
        venues=["aave-v3-base", "moonwell-base"],
        min_spread=0.01,  # Only enter when spread > 1% APR
        fee_rate=0.0005,
    )

    print("\n=== USDC Carry Trade ===")
    print(f"Total return:  {result.stats['total_return']:.2%}")
    print(f"Sharpe:        {result.stats['sharpe']:.2f}")
    print(f"Max drawdown:  {result.stats['max_drawdown']:.2%}")
    print(f"Active time:   {result.stats['exposure_time_pct']:.0%} of periods")

    # exposure_time_pct < 1.0 means the spread wasn't always positive
    # This is normal — lending rates converge during low-demand periods


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
