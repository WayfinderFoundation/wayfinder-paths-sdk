"""
Delta-neutral basis carry strategy example.

Short perps (collect positive funding) + long spot (hedge price exposure).
Net delta ≈ 0; profit source is the funding rate paid by perp longs.
"""

from __future__ import annotations

from wayfinder_paths.core.backtesting import backtest_delta_neutral


async def main() -> None:
    # One-liner: fetch data, build delta-neutral positions, run backtest
    result = await backtest_delta_neutral(
        symbols=["BTC", "ETH"],
        start_date="2025-08-01",
        end_date="2026-01-01",
        funding_threshold=0.0001,  # Enter when funding > 0.01% per hour
        leverage=1.5,
        interval="1h",
    )

    print("\n=== Delta-Neutral Basis Carry ===")
    print(f"Total return:    {result.stats['total_return']:.2%}")
    print(f"Sharpe:          {result.stats['sharpe']:.2f}")
    print(f"Max drawdown:    {result.stats['max_drawdown']:.2%}")
    print(f"Ann. volatility: {result.stats['volatility_ann']:.2%}")
    print(
        f"Funding income:  {result.stats['total_funding']:.4f}  (negative = received)"
    )

    # Delta-neutral health check:
    # - volatility_ann should be very low (<5%)
    # - total_funding should be negative (we received funding)
    # - max_drawdown should be minimal
    assert result.stats["volatility_ann"] < 0.10, (
        "Volatility too high — hedge may be off"
    )
    assert result.stats["total_funding"] <= 0, "Paying funding — check sign convention"


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
