"""Five-minute HYPE snapback with passive entries and maker exits."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.primitives import (
    ExecutionContext,
    mark_to_market_equity,
)
from wayfinder_paths.jobs.indicators import atr, wilder_rsi
from wayfinder_paths.jobs.strategies._starter_utils import current_rows, merge_params


class HypePassiveRsiStrategy:
    """Rest a deep post-only bid after an oversold HYPE close.

    The full and staged catalog starters share this implementation. ``exit_mode``
    selects either one full maker take-profit or a two-level sell ladder. The
    ladder can optionally move the remaining stop to break-even after stage one.
    """

    default_params: dict[str, Any] = {
        "symbols": ["HYPE"],
        "venue": "hyperliquid",
        "rsi_period": 14,
        "entry_rsi": 30.0,
        "entry_offset_atr": 2.0,
        "entry_ttl_bars": 1,
        "exit_mode": "full",
        "take_profit_atr": 1.5,
        "take_profit_one_atr": 1.0,
        "take_profit_two_atr": 1.5,
        "take_profit_one_fraction": 0.5,
        "move_stop_to_break_even": False,
        "max_hold_bars": 4,
        "stop_atr_period": 14,
        "stop_atr_multiple": 3.0,
        "stop_min_pct": 0.001,
        "stop_max_pct": 0.20,
        "native_stop_required": True,
        "maker_fee_bps": 1.5,
        "maker_trade_through_bps": 1.0,
    }

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = merge_params(self.default_params, params)
        self.symbol = str(self.params["symbols"][0])
        self.warmup_bars = max(
            int(self.params["rsi_period"]), int(self.params["stop_atr_period"])
        ) + 4
        exit_mode = str(self.params["exit_mode"])
        if exit_mode not in {"full", "staged"}:
            raise ValueError("exit_mode must be 'full' or 'staged'")
        fraction = float(self.params["take_profit_one_fraction"])
        if not 0 < fraction < 1:
            raise ValueError("take_profit_one_fraction must be between 0 and 1")

    def precompute(self, frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        derived: dict[str, pd.DataFrame] = {}
        for symbol, frame in frames.items():
            close = pd.to_numeric(frame["close"], errors="coerce")
            derived[symbol] = pd.DataFrame(
                {
                    "maker_rsi": wilder_rsi(close, int(self.params["rsi_period"])),
                    "maker_atr": atr(frame, int(self.params["stop_atr_period"])),
                }
            )
        return derived

    def decide(self, ctx: ExecutionContext) -> list[dict[str, Any]]:
        if ctx.bar_index < self.warmup_bars:
            return []
        rows = current_rows(
            ctx, [self.symbol], required_columns=("maker_rsi", "maker_atr")
        )
        if rows is None:
            return []
        row = rows[self.symbol]
        rsi = float(row["maker_rsi"])
        atr_value = float(row["maker_atr"])
        close = float(row["close"])
        if not all(pd.notna(value) for value in (rsi, atr_value, close)):
            return []
        if atr_value <= 0 or close <= 0:
            return []

        position = ctx.ledger.positions.get(self.symbol)
        resting = [
            order for order in ctx.resting_orders if order.intent.symbol == self.symbol
        ]
        if position is not None:
            if position.side != "long":
                return [self._market_close(position, "unexpected_short")]
            if position.bars_held >= int(self.params["max_hold_bars"]) - 1:
                return [self._market_close(position, "max_hold")]
            if any(order.intent.reduce_only for order in resting):
                return []
            entry_atr = self._entry_atr(ctx, atr_value)
            return self._take_profit_orders(position, entry_atr)

        if any(not order.intent.reduce_only for order in resting):
            return []
        if rsi > float(self.params["entry_rsi"]):
            ctx.strategy_state.pop("maker_entry", None)
            return []

        limit_price = close - float(self.params["entry_offset_atr"]) * atr_value
        if limit_price <= 0:
            return []
        ctx.strategy_state["maker_entry"] = {
            "signal_atr": atr_value,
            "signal_timestamp": ctx.timestamp,
            "limit_price": limit_price,
        }
        stop_pct = float(self.params["stop_atr_multiple"]) * atr_value / limit_price
        stop_pct = min(
            float(self.params["stop_max_pct"]),
            max(float(self.params["stop_min_pct"]), stop_pct),
        )
        return [
            {
                "action": "OPEN",
                "venue": str(self.params["venue"]),
                "symbol": self.symbol,
                "side": "buy",
                "notional": mark_to_market_equity(ctx),
                "limit_price": limit_price,
                "time_in_force": "ALO",
                "expires_after_bars": int(self.params["entry_ttl_bars"]),
                "bracket": {
                    "stop_loss_pct": stop_pct,
                    "policy": "conservative",
                    "native_required": bool(self.params["native_stop_required"]),
                },
                "metadata": {
                    "entry_reason": "rsi_oversold_passive_bid",
                    "signal_rsi": rsi,
                    "signal_atr": atr_value,
                },
            }
        ]

    def _entry_atr(self, ctx: ExecutionContext, fallback: float) -> float:
        entry = ctx.strategy_state.get("maker_entry")
        if isinstance(entry, Mapping):
            value = entry.get("signal_atr")
            if isinstance(value, (int, float)) and float(value) > 0:
                return float(value)
        return fallback

    def _take_profit_orders(self, position: Any, entry_atr: float) -> list[dict[str, Any]]:
        if str(self.params["exit_mode"]) == "full":
            return [
                self._take_profit(
                    position,
                    size=float(position.size),
                    target_atr=float(self.params["take_profit_atr"]),
                    entry_atr=entry_atr,
                    stage="full",
                )
            ]

        first_fraction = float(self.params["take_profit_one_fraction"])
        first_size = float(position.size) * first_fraction
        return [
            self._take_profit(
                position,
                size=first_size,
                target_atr=float(self.params["take_profit_one_atr"]),
                entry_atr=entry_atr,
                stage="one",
                move_stop_to_break_even=bool(
                    self.params["move_stop_to_break_even"]
                ),
            ),
            self._take_profit(
                position,
                size=float(position.size) - first_size,
                target_atr=float(self.params["take_profit_two_atr"]),
                entry_atr=entry_atr,
                stage="two",
            ),
        ]

    def _take_profit(
        self,
        position: Any,
        *,
        size: float,
        target_atr: float,
        entry_atr: float,
        stage: str,
        move_stop_to_break_even: bool = False,
    ) -> dict[str, Any]:
        return {
            "action": "TAKE_PROFIT",
            "venue": str(self.params["venue"]),
            "symbol": self.symbol,
            "side": "sell",
            "size": size,
            "reduce_only": True,
            "limit_price": float(position.avg_price) + target_atr * entry_atr,
            "time_in_force": "ALO",
            "metadata": {
                "exit_stage": stage,
                "move_stop_to_break_even": move_stop_to_break_even,
            },
        }

    def _market_close(self, position: Any, reason: str) -> dict[str, Any]:
        return {
            "action": "CLOSE",
            "venue": str(self.params["venue"]),
            "symbol": self.symbol,
            "side": "sell" if position.side == "long" else "buy",
            "size": float(position.size),
            "reduce_only": True,
            "metadata": {"exit_reason": reason},
        }


def build_strategy(params: dict[str, Any] | None = None) -> HypePassiveRsiStrategy:
    return HypePassiveRsiStrategy(params)
