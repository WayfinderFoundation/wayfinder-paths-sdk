"""Long-only momentum rotation with an optional defensive asset relay."""

from __future__ import annotations

from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.primitives import ExecutionContext
from wayfinder_paths.jobs.indicators import bounded_span, trailing_return
from wayfinder_paths.jobs.strategies._starter_utils import (
    RANKING_STOP_DEFAULTS,
    add_stop_atr,
    current_feature_values,
    merge_params,
    stop_brackets,
)
from wayfinder_paths.jobs.strategies.portfolio import target_weights_to_intents


class RegimeRotationStrategy:
    """Own the strongest confirmed uptrend or relay to a defensive asset.

    The strategy is intentionally long-only.  When too few risk assets have
    positive momentum it either holds cash or, when configured, owns the
    defensive symbol only while that symbol has positive momentum itself.
    """

    default_params: dict[str, Any] = {
        "symbols": ["BNB", "PAXG", "HYPE", "ZEC", "MORPHO"],
        "risk_symbols": ["BNB", "PAXG", "HYPE", "ZEC", "MORPHO"],
        "defensive_symbol": None,
        "venue": "hyperliquid",
        "momentum_bars": 288,
        "fast_sma_bars": 96,
        "slow_sma_bars": 480,
        "require_trend_alignment": True,
        "minimum_breadth": 0.5,
        "top_n": 1,
        "gross_exposure": 0.4,
        "rebalance_bars": 96,
        "rebalance_offset": 48,
        "rebalance_threshold": 0.10,
        "min_trade_notional": 25.0,
        **RANKING_STOP_DEFAULTS,
        "stop_atr_period": 96,
    }

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = merge_params(self.default_params, params)
        self.risk_symbols = [
            str(symbol) for symbol in self.params.get("risk_symbols") or []
        ]
        self.defensive_symbol = (
            str(self.params["defensive_symbol"])
            if self.params.get("defensive_symbol")
            else None
        )
        symbols = list(self.params["symbols"])
        if not self.risk_symbols or not set(self.risk_symbols).issubset(symbols):
            raise ValueError("risk_symbols must be a non-empty subset of symbols")
        if self.defensive_symbol and self.defensive_symbol not in symbols:
            raise ValueError("defensive_symbol must be present in symbols")
        for name in (
            "momentum_bars",
            "fast_sma_bars",
            "slow_sma_bars",
            "rebalance_bars",
            "stop_atr_period",
        ):
            if int(self.params[name]) <= 0:
                raise ValueError(f"{name} must be positive")
        top_n = int(self.params["top_n"])
        if top_n <= 0 or top_n > len(self.risk_symbols):
            raise ValueError("top_n must fit inside risk_symbols")
        breadth = float(self.params["minimum_breadth"])
        if not 0 < breadth <= 1:
            raise ValueError("minimum_breadth must be in (0, 1]")
        gross = float(self.params["gross_exposure"])
        if not 0 < gross <= 1:
            raise ValueError("gross_exposure must be in (0, 1]")
        indicator_bars = [
            int(self.params["momentum_bars"]),
            bounded_span(int(self.params["stop_atr_period"])),
        ]
        if bool(self.params["require_trend_alignment"]):
            indicator_bars.extend(
                (
                    int(self.params["fast_sma_bars"]),
                    int(self.params["slow_sma_bars"]),
                )
            )
        self.warmup_bars = max(indicator_bars) + 4

    def precompute(self, frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        momentum_bars = int(self.params["momentum_bars"])
        fast_bars = int(self.params["fast_sma_bars"])
        slow_bars = int(self.params["slow_sma_bars"])
        derived: dict[str, pd.DataFrame] = {}
        for symbol, frame in frames.items():
            close = pd.to_numeric(frame["close"], errors="coerce")
            features = {"starter_momentum": trailing_return(close, momentum_bars)}
            if bool(self.params["require_trend_alignment"]):
                features.update(
                    {
                        "starter_fast_sma": close.rolling(
                            fast_bars, min_periods=fast_bars
                        ).mean(),
                        "starter_slow_sma": close.rolling(
                            slow_bars, min_periods=slow_bars
                        ).mean(),
                    }
                )
            derived[symbol] = pd.DataFrame(features)
        return add_stop_atr(derived, frames, period=int(self.params["stop_atr_period"]))

    def decide(self, ctx: ExecutionContext) -> list[dict[str, Any]]:
        if ctx.bar_index < self.warmup_bars or not ctx.every_n_bars(
            int(self.params["rebalance_bars"]),
            offset=int(self.params["rebalance_offset"]),
        ):
            return []
        symbols = list(self.params["symbols"])
        momentum = current_feature_values(ctx, symbols, "starter_momentum")
        closes = current_feature_values(ctx, symbols, "close")
        if momentum is None or closes is None:
            return []
        require_alignment = bool(self.params["require_trend_alignment"])
        fast_sma = (
            current_feature_values(ctx, symbols, "starter_fast_sma")
            if require_alignment
            else None
        )
        slow_sma = (
            current_feature_values(ctx, symbols, "starter_slow_sma")
            if require_alignment
            else None
        )
        if require_alignment and (fast_sma is None or slow_sma is None):
            return []
        eligible: dict[str, float] = {}
        for symbol in self.risk_symbols:
            aligned = (
                fast_sma is not None
                and slow_sma is not None
                and fast_sma[symbol] > slow_sma[symbol]
                and closes[symbol] > slow_sma[symbol]
            )
            if momentum[symbol] > 0 and (aligned or not require_alignment):
                eligible[symbol] = momentum[symbol]

        breadth = len(eligible) / len(self.risk_symbols)
        weights = dict.fromkeys(symbols, 0.0)
        if breadth >= float(self.params["minimum_breadth"]):
            selected = sorted(
                eligible, key=lambda symbol: (eligible[symbol], symbol), reverse=True
            )[: int(self.params["top_n"])]
            for symbol in selected:
                weights[symbol] = float(self.params["gross_exposure"]) / len(selected)
        elif self.defensive_symbol:
            defensive_momentum = momentum[self.defensive_symbol]
            if defensive_momentum > 0:
                weights[self.defensive_symbol] = float(self.params["gross_exposure"])

        return target_weights_to_intents(
            ctx,
            weights,
            venue=str(self.params["venue"]),
            rebalance_threshold=float(self.params["rebalance_threshold"]),
            min_trade_notional=float(self.params["min_trade_notional"]),
            brackets=stop_brackets(ctx, symbols, self.params),
        )


def build_strategy(params: dict[str, Any] | None = None) -> RegimeRotationStrategy:
    return RegimeRotationStrategy(params)
