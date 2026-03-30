"""Sizing functions for Polymarket copy-trade backtesting."""

from __future__ import annotations

from wayfinder_paths.core.backtesting.polymarket_parser import TradeSignal
from wayfinder_paths.core.backtesting.polymarket_types import (
    PolymarketBacktestState,
    SizingFn,
)


def flat_ratio_sizer(ratio: float, max_order: float) -> SizingFn:
    """Size at `ratio` × WOI's USDC amount, capped at max_order and current balance."""

    def fn(signal: TradeSignal, state: PolymarketBacktestState) -> float:
        return min(signal.usdc_amount * ratio, max_order, state.usdc_balance)

    return fn


def flat_dollar_sizer(amount: float) -> SizingFn:
    """Always allocate a fixed USDC amount, capped at current balance."""

    def fn(signal: TradeSignal, state: PolymarketBacktestState) -> float:
        return min(amount, state.usdc_balance)

    return fn


def proportional_sizer(fraction: float) -> SizingFn:
    """Allocate a fixed fraction of current balance per trade (compounds)."""

    def fn(signal: TradeSignal, state: PolymarketBacktestState) -> float:
        return state.usdc_balance * fraction

    return fn


def conviction_sizer(
    *,
    woi_median_bet: float,
    conviction_threshold: float = 1.5,
    base_amount: float = 20.0,
    scale_with_conviction: bool = False,
) -> SizingFn:
    """Only copy trades where WOI bet >= conviction_threshold × their median.

    If scale_with_conviction=True, bet scales linearly up to 2× base_amount
    at 3× median conviction.
    """

    def fn(signal: TradeSignal, state: PolymarketBacktestState) -> float:
        conviction = signal.usdc_amount / woi_median_bet if woi_median_bet > 0 else 0
        if conviction < conviction_threshold:
            return 0.0
        if scale_with_conviction:
            multiplier = (
                min(conviction / conviction_threshold, 3.0) / conviction_threshold
            )
            amount = base_amount * min(multiplier, 2.0)
        else:
            amount = base_amount
        return min(amount, state.usdc_balance)

    return fn


def kelly_conviction_sizer(
    *,
    woi_median_bet: float,
    conviction_threshold: float = 1.5,
    kelly_fraction: float = 0.25,
    max_bet_pct: float = 0.10,
) -> SizingFn:
    """Kelly-inspired sizing scaled by conviction and edge.

    Edge proxy: price distance from 0.5 × (conviction / threshold).
    Bet = kelly_fraction × balance × edge, capped at max_bet_pct of balance.
    """

    def fn(signal: TradeSignal, state: PolymarketBacktestState) -> float:
        conviction = signal.usdc_amount / woi_median_bet if woi_median_bet > 0 else 0
        if conviction < conviction_threshold:
            return 0.0
        price = signal.avg_price
        if price <= 0 or price >= 1:
            return 0.0
        edge = abs(price - 0.5) * conviction / conviction_threshold
        bet = state.usdc_balance * kelly_fraction * edge
        bet = min(bet, state.usdc_balance * max_bet_pct)
        return min(bet, state.usdc_balance)

    return fn
