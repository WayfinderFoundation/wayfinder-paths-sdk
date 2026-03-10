"""
Lending yield rotation strategy example.

Rotates USDC capital across lending venues (Aave, Moonwell, Morpho) to always
supply at the highest available APR. Uses trailing 7-day average rate as signal.
"""

from __future__ import annotations

from wayfinder_paths.core.backtesting import backtest_yield_rotation


async def main() -> None:
    # Venue keys include chain suffix — discover with fetch_lending_rates("USDC", start, end)
    # then print(rates["supply"].columns.tolist()) to see available names.
    result = await backtest_yield_rotation(
        symbol="USDC",
        venues=["aave-v3-base", "moonwell-base"],
        start_date="2025-08-01",
        end_date="2026-01-01",
        lookback_signal_days=7,  # 7-day trailing avg for venue selection
        fee_rate=0.0005,  # 5bps = amortized gas cost per switch
    )

    print("\n=== USDC Yield Rotation ===")
    print(f"Total return:  {result.stats['total_return']:.2%}")
    print(f"Sharpe:        {result.stats['sharpe']:.2f}")
    print(f"Max drawdown:  {result.stats['max_drawdown']:.2%}")
    print(f"Venue switches: {result.stats['trade_count']}")

    # Health check: if trade_count is high, gas costs will dominate.
    # Increase lookback_signal_days to reduce switching frequency.
    if result.stats["trade_count"] > 20:
        print(
            "\n⚠  High switching frequency — consider increasing lookback_signal_days"
        )
    else:
        print("\n✓ Switching frequency is reasonable")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
