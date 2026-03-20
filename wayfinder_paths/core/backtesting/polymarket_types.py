"""Types for Polymarket event-driven copy-trade backtesting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, TypedDict

import pandas as pd

from wayfinder_paths.strategies.polymarket_copy_strategy.parser import TradeSignal


class PolymarketBacktestStats(TypedDict, total=False):
    """Statistics from a Polymarket copy-trade backtest.

    All monetary values are in USDC.  Rates are decimals (0.1 = 10%).
    NaN values indicate a metric was not computable (e.g. brier_score with no
    resolved markets).
    """

    # Equity
    equity_final: float
    total_return: float  # (equity_final - initial_capital) / initial_capital

    # Costs
    total_fees: float

    # Trade activity
    trade_count: int  # executed trades (skipped trades not counted)
    avg_position_size_usdc: float  # mean USDC per executed trade

    # Market-level outcomes (based on resolution_prices)
    markets_traded: int
    markets_won: int
    markets_lost: int
    markets_voided: int  # unresolved or ambiguous
    market_win_rate: float  # markets_won / (markets_won + markets_lost), NaN if none

    # Forecast quality
    brier_score: float  # mean((resolution - entry_price)^2), NaN if no resolutions


@dataclass
class PolymarketBacktestConfig:
    initial_capital: float = 100.0
    fee_rate: float = 0.02
    slippage_rate: float = 0.0
    min_order_usdc: float = 10.0
    equity_interval: str = "1h"
    assume_resolution_at_end: bool = False
    max_price_gap_hours: int | None = None
    resolution_threshold: float = 0.99  # price >= threshold → resolved YES
    void_threshold: float = 0.05  # |price - 0.5| < void_threshold at end → voided


@dataclass
class PolymarketBacktestState:
    usdc_balance: float
    positions: dict[str, float] = field(default_factory=dict)  # token_id → shares
    woi_portfolio_usdc: float | None = None


@dataclass
class PolymarketBacktestResult:
    equity_curve: pd.Series
    stats: PolymarketBacktestStats
    trades: list[dict[str, Any]]
    positions_over_time: pd.DataFrame


# (signal, state) → sized USDC amount to allocate
SizingFn = Callable[[TradeSignal, PolymarketBacktestState], float]
