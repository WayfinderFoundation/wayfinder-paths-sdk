"""One-hour volume-confirmed RSI capitulation across mixed perp markets."""

from __future__ import annotations

from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.primitives import ExecutionContext
from wayfinder_paths.jobs.indicators import wilder_rsi
from wayfinder_paths.jobs.strategies._starter_utils import (
    MEAN_REVERSION_STOP_DEFAULTS,
    add_stop_atr,
    current_rows,
    merge_params,
    stop_brackets,
)
from wayfinder_paths.jobs.strategies.portfolio import target_weights_to_intents


class MixedVolumeCapitulationStrategy:
    default_params: dict[str, Any] = {
        "symbols": ["BTC", "HYPE", "xyz:COIN", "xyz:TSLA"],
        "venue": "hyperliquid",
        "rsi_period": 7,
        "entry_rsi": 20.0,
        "exit_rsi": 50.0,
        "trend_sma_period": 200,
        "volume_median_bars": 24,
        "volume_multiple": 1.0,
        "max_hold_bars": 72,
        "weight_per_leg": 0.25,
        "rebalance_threshold": 0.10,
        "min_trade_notional": 25.0,
        **MEAN_REVERSION_STOP_DEFAULTS,
    }

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = merge_params(self.default_params, params)
        self.warmup_bars = (
            max(
                int(self.params["trend_sma_period"]),
                int(self.params["volume_median_bars"]),
            )
            + 4
        )

    def precompute(self, frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        rsi_period = int(self.params["rsi_period"])
        trend_bars = int(self.params["trend_sma_period"])
        volume_bars = int(self.params["volume_median_bars"])
        derived: dict[str, pd.DataFrame] = {}
        for symbol, frame in frames.items():
            close = pd.to_numeric(frame["close"], errors="coerce")
            volume = pd.to_numeric(frame["volume"], errors="coerce")
            derived[symbol] = pd.DataFrame(
                {
                    "starter_capitulation_rsi": wilder_rsi(close, rsi_period),
                    "starter_trend_sma": close.rolling(
                        trend_bars, min_periods=trend_bars
                    ).mean(),
                    "starter_volume_median": volume.rolling(
                        volume_bars, min_periods=volume_bars
                    ).median(),
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
                "starter_capitulation_rsi",
                "starter_trend_sma",
                "starter_volume_median",
            ),
        )
        if rows is None:
            return []

        weights: dict[str, float] = {}
        for symbol, row in rows.items():
            rsi = float(row["starter_capitulation_rsi"])
            close = float(row["close"])
            trend_sma = float(row["starter_trend_sma"])
            volume = float(row["volume"])
            volume_median = float(row["starter_volume_median"])
            position = ctx.ledger.positions.get(symbol)
            should_exit = position is not None and (
                rsi > float(self.params["exit_rsi"])
                or position.bars_held >= int(self.params["max_hold_bars"]) - 1
            )
            should_enter = (
                position is None
                and rsi < float(self.params["entry_rsi"])
                and close > trend_sma
                and volume > volume_median * float(self.params["volume_multiple"])
            )
            weights[symbol] = (
                0.0
                if should_exit
                else float(self.params["weight_per_leg"])
                if position is not None or should_enter
                else 0.0
            )

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
) -> MixedVolumeCapitulationStrategy:
    return MixedVolumeCapitulationStrategy(params)
