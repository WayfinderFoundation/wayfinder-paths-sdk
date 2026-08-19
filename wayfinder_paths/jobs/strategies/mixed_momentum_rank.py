"""One-hour cross-asset momentum rank, rebalanced daily at 12:00 UTC."""

from __future__ import annotations

from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.primitives import ExecutionContext
from wayfinder_paths.jobs.strategies._starter_utils import (
    RANKING_STOP_DEFAULTS,
    add_stop_atr,
    current_feature_values,
    merge_params,
    ranked_weights,
    stop_brackets,
    trailing_return_features,
)
from wayfinder_paths.jobs.strategies.portfolio import target_weights_to_intents


class MixedMomentumRankStrategy:
    default_params: dict[str, Any] = {
        "symbols": ["BTC", "SOL", "xyz:XYZ100", "xyz:TSLA"],
        "venue": "hyperliquid",
        "momentum_bars": 336,
        "rebalance_bars": 24,
        "rebalance_offset": 12,
        "weight_per_leg": 0.25,
        "rebalance_threshold": 0.10,
        "min_trade_notional": 25.0,
        **RANKING_STOP_DEFAULTS,
        "stop_atr_period": 24,
    }

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = merge_params(self.default_params, params)
        self.warmup_bars = int(self.params["momentum_bars"]) + 4

    def precompute(self, frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        lookback = int(self.params["momentum_bars"])
        derived = trailing_return_features(
            frames, lookback=lookback, column="starter_momentum"
        )
        return add_stop_atr(derived, frames, period=int(self.params["stop_atr_period"]))

    def decide(self, ctx: ExecutionContext) -> list[dict[str, Any]]:
        if ctx.bar_index < self.warmup_bars or not ctx.every_n_bars(
            int(self.params["rebalance_bars"]),
            offset=int(self.params["rebalance_offset"]),
        ):
            return []
        scores = current_feature_values(
            ctx, list(self.params["symbols"]), "starter_momentum"
        )
        if scores is None:
            return []
        weights = ranked_weights(
            scores, weight_per_leg=float(self.params["weight_per_leg"])
        )
        return target_weights_to_intents(
            ctx,
            weights,
            venue=str(self.params["venue"]),
            rebalance_threshold=float(self.params["rebalance_threshold"]),
            min_trade_notional=float(self.params["min_trade_notional"]),
            brackets=stop_brackets(ctx, list(self.params["symbols"]), self.params),
        )


def build_strategy(params: dict[str, Any] | None = None) -> MixedMomentumRankStrategy:
    return MixedMomentumRankStrategy(params)
