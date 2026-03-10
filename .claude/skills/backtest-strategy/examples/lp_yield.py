"""
LP / AMM yield strategy example.

Models a 50/50 ETH/USDC liquidity position. Shows whether fee income
outweighs impermanent loss over the backtest period.
"""

from __future__ import annotations

from wayfinder_paths.core.backtesting import (
    backtest_lp_position,
    fetch_prices,
    simulate_il,
)


async def main() -> None:
    # First, inspect the raw impermanent loss to calibrate fee expectations
    prices = await fetch_prices(["ETH", "USDC"], "2025-08-01", "2026-01-01")
    il = simulate_il(prices, ("ETH", "USDC"))
    print(f"Max IL point:  {il.min():.2%}")
    print(f"End IL:        {il.iloc[-1]:.2%}")

    # Estimate break-even fee rate
    years = len(prices) / 8760
    breakeven_fee = (-float(il.iloc[-1])) / years
    print(f"Break-even fee APY: {breakeven_fee:.2%}  (need at least this from fees)")

    # Backtest with assumed fee income rate
    # (Estimate from pool analytics: 24h_volume × fee_tier / TVL × 365)
    fee_income_rate = 0.25  # 25% APY from trading fees

    result = await backtest_lp_position(
        pool_assets=("ETH", "USDC"),
        start_date="2025-08-01",
        end_date="2026-01-01",
        fee_income_rate=fee_income_rate,
        interval="1h",
    )

    print("\n=== ETH/USDC LP Position ===")
    print(f"LP return:     {result.stats['total_return']:.2%}")
    print(f"Hold return:   {result.stats['buy_hold_return']:.2%}")
    print(f"Sharpe:        {result.stats['sharpe']:.2f}")
    print(f"Max drawdown:  {result.stats['max_drawdown']:.2%}")

    if result.stats["total_return"] > 0:
        print("\n✓ Fees outweighed impermanent loss")
    else:
        print("\n✗ Impermanent loss dominated — would have been better to hold")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
