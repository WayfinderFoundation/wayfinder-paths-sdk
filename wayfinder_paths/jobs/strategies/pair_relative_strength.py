"""Volatility-targeted relative-strength trade for one crypto pair."""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.primitives import ExecutionContext
from wayfinder_paths.jobs.strategies._starter_utils import (
    PAIR_PROTECTION_DEFAULTS,
    add_stop_atr,
    bounded_span,
    current_rows,
    merge_params,
    stop_brackets,
)
from wayfinder_paths.jobs.strategies.portfolio import target_weights_to_intents


class PairRelativeStrengthStrategy:
    """Long the stronger leg and short the weaker leg at equal dollar risk.

    The pair is selected for economic relatedness and liquidity. This is a
    cross-sectional momentum trade, not a claim that the price ratio is
    cointegrated. Realized spread volatility scales gross exposure so a
    volatile relative move automatically reduces risk.
    """

    default_params: dict[str, Any] = {
        "symbols": ["BTC", "ETH"],
        "venue": "hyperliquid",
        "momentum_bars": 90,
        "volatility_bars": 28,
        "bars_per_year": 365,
        "target_volatility": 0.10,
        "min_gross_exposure": 0.15,
        "max_gross_exposure": 1.0,
        "rebalance_bars": 7,
        "rebalance_offset": 4,
        "rebalance_threshold": 0.0,
        "min_trade_notional": 25.0,
        **PAIR_PROTECTION_DEFAULTS,
    }

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = merge_params(self.default_params, params)
        if len(self.params["symbols"]) != 2:
            raise ValueError("pair relative strength requires exactly two symbols")
        self.warmup_bars = (
            max(
                int(self.params["momentum_bars"]),
                int(self.params["volatility_bars"]),
                bounded_span(int(self.params["stop_atr_period"])),
            )
            + 4
        )

    def precompute(self, frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        symbol_a, symbol_b = self.params["symbols"]
        frame_a = frames[symbol_a]
        frame_b = frames[symbol_b]
        close_a = _timestamped_close(frame_a)
        close_b = _timestamped_close(frame_b)
        aligned = pd.concat(
            [close_a.rename("a"), close_b.rename("b")], axis=1, join="inner"
        ).dropna()

        formation = int(self.params["momentum_bars"])
        volatility_bars = int(self.params["volatility_bars"])
        log_a = aligned["a"].map(math.log)
        log_b = aligned["b"].map(math.log)
        relative_momentum = (log_a - log_a.shift(formation)) - (
            log_b - log_b.shift(formation)
        )
        relative_return = log_a.diff() - log_b.diff()
        spread_volatility = (
            0.5
            * relative_return.rolling(volatility_bars).std()
            * math.sqrt(float(self.params["bars_per_year"]))
        )

        timestamps = pd.to_datetime(frame_a["timestamp"], utc=True)
        features = pd.DataFrame(index=frame_a.index)
        features["pair_relative_momentum"] = timestamps.map(relative_momentum)
        features["pair_spread_volatility"] = timestamps.map(spread_volatility)
        return add_stop_atr(
            {symbol_a: features},
            frames,
            period=int(self.params["stop_atr_period"]),
        )

    def decide(self, ctx: ExecutionContext) -> list[dict[str, Any]]:
        symbol_a, symbol_b = self.params["symbols"]
        held = [
            symbol
            for symbol in (symbol_a, symbol_b)
            if ctx.ledger.positions.get(symbol) is not None
        ]
        if len(held) == 1:
            # A pair is one risk unit. Never leave a directional orphan after
            # a rejected/partial companion order.
            position = ctx.ledger.positions[held[0]]
            return [
                {
                    "action": "CLOSE",
                    "venue": str(self.params["venue"]),
                    "symbol": held[0],
                    "side": "sell" if position.side == "long" else "buy",
                    "size": position.size,
                    "reduce_only": True,
                    "metadata": {"exit_reason": "orphan_leg_guard"},
                }
            ]

        if ctx.bar_index < self.warmup_bars or not ctx.every_n_bars(
            int(self.params["rebalance_bars"]),
            offset=int(self.params["rebalance_offset"]),
        ):
            return []

        rows = current_rows(ctx, (symbol_a, symbol_b))
        if rows is None:
            return []
        row = rows[symbol_a]
        momentum = _finite_float(row.get("pair_relative_momentum"))
        spread_volatility = _finite_float(row.get("pair_spread_volatility"))
        if momentum is None or spread_volatility is None or spread_volatility <= 0:
            return []

        if momentum == 0:
            weights = {symbol_a: 0.0, symbol_b: 0.0}
        else:
            gross = float(self.params["target_volatility"]) / spread_volatility
            gross = min(
                float(self.params["max_gross_exposure"]),
                max(float(self.params["min_gross_exposure"]), gross),
            )
            direction = 1.0 if momentum > 0 else -1.0
            weights = {
                symbol_a: 0.5 * gross * direction,
                symbol_b: -0.5 * gross * direction,
            }

        intents = target_weights_to_intents(
            ctx,
            weights,
            venue=str(self.params["venue"]),
            rebalance_threshold=float(self.params["rebalance_threshold"]),
            min_trade_notional=float(self.params["min_trade_notional"]),
            brackets=stop_brackets(
                ctx,
                (symbol_a, symbol_b),
                self.params,
                protection_group={
                    "id": "starter_pair",
                    "symbols": [symbol_a, symbol_b],
                    "max_entry_equity_loss_pct": float(
                        self.params["pair_max_entry_equity_loss_pct"]
                    ),
                    "max_entry_gross_loss_pct": float(
                        self.params["pair_max_entry_gross_loss_pct"]
                    ),
                    "halt_after_exit": True,
                },
            ),
        )
        for intent in intents:
            if str(intent.get("action", "")).upper() == "CLOSE":
                intent.setdefault("metadata", {})["exit_reason"] = "weekly_rebalance"
        return intents


def _timestamped_close(frame: pd.DataFrame) -> pd.Series:
    return pd.Series(
        pd.to_numeric(frame["close"], errors="coerce").to_numpy(),
        index=pd.to_datetime(frame["timestamp"], utc=True),
        dtype=float,
    )


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def build_strategy(
    params: dict[str, Any] | None = None,
) -> PairRelativeStrengthStrategy:
    return PairRelativeStrengthStrategy(params)
