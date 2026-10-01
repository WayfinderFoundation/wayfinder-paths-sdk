"""Inverse-relative-volatility sizing of independent momentum sleeves."""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.primitives import (
    ExecutionContext,
    mark_to_market_equity,
)
from wayfinder_paths.jobs.indicators import realized_volatility
from wayfinder_paths.jobs.strategies._starter_utils import (
    current_feature_values,
    sleeve_weights,
    stop_brackets,
)
from wayfinder_paths.jobs.strategies.mixed_sleeve_momentum import (
    MixedSleeveMomentumStrategy,
)
from wayfinder_paths.jobs.strategies.portfolio import target_weights_to_intents


class RiskBalancedSleeves(MixedSleeveMomentumStrategy):
    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(
            {"risk_window_bars": 1920, "max_sleeve_gross": 0.35, **(params or {})}
        )
        self.risk_window = int(self.params["risk_window_bars"])
        self.max_sleeve_gross = float(self.params["max_sleeve_gross"])
        if self.risk_window < 2 or not 0 < self.max_sleeve_gross <= 1:
            raise ValueError("Invalid risk window or sleeve cap")
        self.warmup_bars = max(self.warmup_bars, self.risk_window + 4)

    def precompute(self, frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        derived = super().precompute(frames)
        for left, right in self.params["sleeves"]:
            if left not in frames or right not in frames:
                continue
            panel = pd.concat(
                {
                    symbol: frames[symbol].set_index("timestamp")["close"].astype(float)
                    for symbol in (left, right)
                },
                axis=1,
            ).sort_index()
            ratio = panel[left] / panel[right]
            risk = realized_volatility(ratio, self.risk_window)
            # No fabricated returns from the helper's legacy pct_change fill:
            # risk is usable only on a full contiguous window of observed ratios.
            valid = (
                ratio.notna()
                .rolling(self.risk_window + 1)
                .sum()
                .eq(self.risk_window + 1)
            )
            risk = risk.where(valid)
            for symbol in (left, right):
                derived[symbol]["starter_sleeve_risk"] = frames[symbol][
                    "timestamp"
                ].map(risk)
        return derived

    def risk_weights(
        self, scores: dict[str, float], risks: dict[str, float]
    ) -> dict[str, float]:
        pair_risks = [risks[pair[0]] for pair in self.params["sleeves"]]
        if any(not math.isfinite(value) or value <= 0 for value in pair_risks):
            return {}
        inverse = [1 / value for value in pair_risks]
        total = sum(inverse)
        signs = sleeve_weights(scores, self.params["sleeves"], weight_per_leg=1)
        weights: dict[str, float] = {}
        for pair, value in zip(self.params["sleeves"], inverse, strict=True):
            gross = min(self.max_sleeve_gross, value / total)
            for symbol in pair:
                weights[symbol] = signs[symbol] * gross / 2
        return weights

    def decide(self, ctx: ExecutionContext) -> list[dict[str, Any]]:
        if ctx.bar_index < self.warmup_bars or not ctx.every_n_bars(
            int(self.params["rebalance_bars"]),
            offset=int(self.params["rebalance_offset"]),
        ):
            return []
        symbols = list(self.params["symbols"])
        scores = current_feature_values(ctx, symbols, "starter_momentum")
        risks = current_feature_values(ctx, symbols, "starter_sleeve_risk")
        if scores is None or risks is None:
            return []
        weights = self.risk_weights(scores, risks)
        if not weights:
            return []
        leverage = float(ctx.params.get("leverage") or 1)
        if not math.isfinite(leverage) or leverage <= 0:
            leverage = 1
        equity = mark_to_market_equity(ctx) * leverage
        if equity <= 0:
            return []
        # Preserve the parent's deadband / leg-weight ratio. A fixed 10%-of-
        # equity band would otherwise suppress all openings in smaller sleeves.
        band_ratio = float(self.params["rebalance_threshold"]) / float(
            self.params["weight_per_leg"]
        )
        eligible = set()
        for symbol, target in weights.items():
            position = ctx.ledger.positions.get(symbol)
            held = 0.0
            if position is not None:
                price = float(ctx.view.symbol_frame(symbol)["close"].iloc[-1])
                held = (
                    position.size
                    * price
                    / equity
                    * (1 if position.side == "long" else -1)
                )
            if target == 0 or abs(target - held) >= band_ratio * abs(target):
                eligible.add(symbol)
        return [
            intent
            for intent in target_weights_to_intents(
                ctx,
                weights,
                venue=str(self.params["venue"]),
                sizing_equity=equity,
                min_trade_notional=float(self.params["min_trade_notional"]),
                brackets=stop_brackets(ctx, symbols, self.params),
            )
            if intent["symbol"] in eligible
        ]


def build_strategy(params: dict[str, Any] | None = None) -> RiskBalancedSleeves:
    return RiskBalancedSleeves(params)
