"""One-hour trend-aligned Bollinger pullbacks across mixed perp markets."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from wayfinder_paths.jobs.execution.primitives import ExecutionContext
from wayfinder_paths.jobs.strategies._starter_utils import (
    MEAN_REVERSION_STOP_DEFAULTS,
    add_stop_atr,
    bounded_span,
    current_rows,
    merge_params,
    stop_brackets,
)
from wayfinder_paths.jobs.strategies.portfolio import target_weights_to_intents


class MixedBollingerPullbackStrategy:
    default_params: dict[str, Any] = {
        "symbols": ["BTC", "SOL", "xyz:XYZ100", "xyz:TSLA"],
        "venue": "hyperliquid",
        "zscore_bars": 72,
        "entry_zscore": 2.0,
        "exit_zscore": 0.0,
        "trend_sma_period": 200,
        "max_hold_bars": 12,
        "weight_per_leg": 0.25,
        "rebalance_threshold": 0.10,
        "min_trade_notional": 25.0,
        **MEAN_REVERSION_STOP_DEFAULTS,
    }

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = merge_params(self.default_params, params)
        self.warmup_bars = (
            max(
                int(self.params["zscore_bars"]),
                int(self.params["trend_sma_period"]),
                bounded_span(int(self.params["stop_atr_period"])),
            )
            + 4
        )

    def precompute(self, frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        zscore_bars = int(self.params["zscore_bars"])
        trend_bars = int(self.params["trend_sma_period"])
        derived: dict[str, pd.DataFrame] = {}
        for symbol, frame in frames.items():
            close = pd.to_numeric(frame["close"], errors="coerce")
            log_close = np.log(close)
            rolling = log_close.rolling(zscore_bars, min_periods=zscore_bars)
            derived[symbol] = pd.DataFrame(
                {
                    "starter_pullback_zscore": (
                        (log_close - rolling.mean()) / rolling.std()
                    ),
                    "starter_trend_sma": close.rolling(
                        trend_bars, min_periods=trend_bars
                    ).mean(),
                }
            )
        return add_stop_atr(derived, frames, period=int(self.params["stop_atr_period"]))

    def decide(self, ctx: ExecutionContext) -> list[dict[str, Any]]:
        symbols = list(self.params["symbols"])
        if ctx.bar_index < self.warmup_bars:
            return []

        rows = current_rows(
            ctx,
            symbols,
            required_columns=(
                "starter_pullback_zscore",
                "starter_trend_sma",
            ),
        )
        if rows is None:
            return []

        weights: dict[str, float] = {}
        entry_zscore = float(self.params["entry_zscore"])
        exit_zscore = float(self.params["exit_zscore"])
        weight = float(self.params["weight_per_leg"])
        max_hold_bars = int(self.params["max_hold_bars"])

        for symbol, row in rows.items():
            zscore = float(row["starter_pullback_zscore"])
            close = float(row["close"])
            trend_sma = float(row["starter_trend_sma"])
            position = ctx.ledger.positions.get(symbol)

            if position is not None:
                crossed_mean = (position.side == "long" and zscore >= exit_zscore) or (
                    position.side == "short" and zscore <= -exit_zscore
                )
                timed_out = position.bars_held >= max_hold_bars - 1
                weights[symbol] = (
                    0.0
                    if crossed_mean or timed_out
                    else weight
                    if position.side == "long"
                    else -weight
                )
            elif zscore < -entry_zscore and close > trend_sma:
                weights[symbol] = weight
            elif zscore > entry_zscore and close < trend_sma:
                weights[symbol] = -weight
            else:
                weights[symbol] = 0.0

        return target_weights_to_intents(
            ctx,
            weights,
            venue=str(self.params["venue"]),
            rebalance_threshold=float(self.params["rebalance_threshold"]),
            min_trade_notional=float(self.params["min_trade_notional"]),
            brackets=stop_brackets(ctx, symbols, self.params),
        )


def build_strategy(
    params: dict[str, Any] | None = None,
) -> MixedBollingerPullbackStrategy:
    return MixedBollingerPullbackStrategy(params)
