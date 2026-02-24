"""
Basic momentum strategy example for backtesting.

This demonstrates a simple cross-sectional momentum strategy that buys
recent winners and sells recent losers.
"""

from __future__ import annotations

import pandas as pd

from wayfinder_paths.core.backtesting import quick_backtest


def simple_momentum(prices: pd.DataFrame, ctx: dict) -> pd.DataFrame:
    """
    Simple momentum strategy: buy winners, sell losers.

    Args:
        prices: DataFrame with columns=symbols, index=timestamps
        ctx: Context dict with symbols, interval, etc.

    Returns:
        Target positions DataFrame (same shape as prices) with weights in [-1, 1]
    """
    lookback = 24  # 24 hour lookback

    returns = prices.pct_change(lookback)

    ranks = returns.rank(axis=1, pct=True)

    target = (ranks > 0.5).astype(float) - (ranks < 0.5).astype(float)

    target = target.div(target.abs().sum(axis=1), axis=0).fillna(0)

    return target


async def main():
    """Run backtest."""
    result = await quick_backtest(
        strategy_fn=simple_momentum,
        symbols=["BTC", "ETH", "SOL"],
        start_date="2025-01-01",
        end_date="2025-02-01",
        interval="1h",
        leverage=1.5,
        include_funding=True,
    )

    print("\n=== Backtest Results ===")
    print(f"Sharpe Ratio: {result.stats['sharpe']:.2f}")
    print(f"Sortino Ratio: {result.stats['sortino']:.2f}")
    print(f"CAGR: {result.stats['cagr']:.2f}%")
    print(f"Max Drawdown: {result.stats['max_drawdown']:.2f}%")
    print(f"Win Rate: {result.stats['win_rate']:.2f}%")
    print(f"Total Return: {result.stats['total_return']:.2f}%")
    print(f"Trade Count: {result.stats['trade_count']}")

    if result.liquidated:
        print(f"\n⚠️  LIQUIDATED at {result.liquidation_timestamp}")
    else:
        print("\n✓ Strategy survived without liquidation")

    print(f"\nFinal Equity: ${result.stats['final_equity']:.4f}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
