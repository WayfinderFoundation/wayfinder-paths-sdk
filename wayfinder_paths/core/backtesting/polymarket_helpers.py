"""Sizing functions for Polymarket copy-trade backtesting."""

from __future__ import annotations

from wayfinder_paths.core.backtesting.polymarket_types import (
    PolymarketBacktestState,
    SizingFn,
)
from wayfinder_paths.strategies.polymarket_copy_strategy.parser import TradeSignal


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
