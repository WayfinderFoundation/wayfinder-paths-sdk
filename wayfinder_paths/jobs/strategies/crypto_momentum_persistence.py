"""Risk-adjusted four-hour crypto momentum with a broad-rally overlay."""

from __future__ import annotations

from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.primitives import ExecutionContext
from wayfinder_paths.jobs.strategies._starter_utils import (
    RANKING_STOP_DEFAULTS,
    add_stop_atr,
    current_rows,
    merge_params,
    ranked_weights,
    stop_brackets,
)
from wayfinder_paths.jobs.strategies.portfolio import target_weights_to_intents


class CryptoMomentumPersistenceStrategy:
    default_params: dict[str, Any] = {
        "symbols": ["BTC", "ETH", "SOL", "HYPE"],
        "venue": "hyperliquid",
        "fast_momentum_bars": 42,
        "slow_momentum_bars": 168,
        "fast_momentum_weight": 0.5,
        "score_volatility_bars": 168,
        "rebalance_bars": 6,
        "rebalance_offset": 3,
        "weight_per_leg": 0.35,
        "broad_bull_momentum_threshold": 0.10,
        "broad_bull_weight_shift": 0.175,
        "rebalance_threshold": 0.10,
        "min_trade_notional": 25.0,
        **RANKING_STOP_DEFAULTS,
        "stop_atr_period": 12,
    }

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = merge_params(self.default_params, params)
        self.warmup_bars = (
            max(
                int(self.params["fast_momentum_bars"]),
                int(self.params["slow_momentum_bars"]),
                int(self.params["score_volatility_bars"]),
            )
            + 4
        )

    def precompute(self, frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        fast = int(self.params["fast_momentum_bars"])
        slow = int(self.params["slow_momentum_bars"])
        fast_weight = float(self.params["fast_momentum_weight"])
        volatility_bars = int(self.params["score_volatility_bars"])
        derived: dict[str, pd.DataFrame] = {}
        for symbol, frame in frames.items():
            close = pd.to_numeric(frame["close"], errors="coerce")
            raw_momentum = fast_weight * close.pct_change(fast) + (
                1.0 - fast_weight
            ) * close.pct_change(slow)
            volatility = (
                close.pct_change()
                .rolling(volatility_bars, min_periods=volatility_bars)
                .std()
            )
            score = raw_momentum / volatility.where(volatility.gt(0))
            derived[symbol] = pd.DataFrame(
                {
                    "starter_momentum": score,
                    "starter_raw_momentum": raw_momentum,
                }
            )
        return add_stop_atr(derived, frames, period=int(self.params["stop_atr_period"]))

    def decide(self, ctx: ExecutionContext) -> list[dict[str, Any]]:
        if ctx.bar_index < self.warmup_bars or not ctx.every_n_bars(
            int(self.params["rebalance_bars"]),
            offset=int(self.params["rebalance_offset"]),
        ):
            return []
        symbols = list(self.params["symbols"])
        rows = current_rows(
            ctx,
            symbols,
            required_columns=("starter_momentum", "starter_raw_momentum"),
        )
        if rows is None:
            return []
        scores = {symbol: float(rows[symbol]["starter_momentum"]) for symbol in symbols}
        raw_scores = {
            symbol: float(rows[symbol]["starter_raw_momentum"]) for symbol in symbols
        }
        if any(pd.isna(value) for value in (*scores.values(), *raw_scores.values())):
            return []
        ranked = sorted(scores, key=lambda symbol: (scores[symbol], symbol))
        extremes = {symbol: scores[symbol] for symbol in (ranked[0], ranked[-1])}
        weights = dict.fromkeys(symbols, 0.0)
        weights.update(
            ranked_weights(
                extremes, weight_per_leg=float(self.params["weight_per_leg"])
            )
        )
        if min(raw_scores.values()) >= float(
            self.params["broad_bull_momentum_threshold"]
        ):
            # Transfer exposure from the short to the long without increasing gross.
            shift = float(self.params["broad_bull_weight_shift"])
            weights[ranked[0]] += shift
            weights[ranked[-1]] += shift
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
) -> CryptoMomentumPersistenceStrategy:
    return CryptoMomentumPersistenceStrategy(params)
