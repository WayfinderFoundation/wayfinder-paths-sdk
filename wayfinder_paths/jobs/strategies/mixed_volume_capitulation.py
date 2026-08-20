"""One-hour volume-confirmed RSI capitulation across mixed perp markets."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.primitives import (
    ExecutionContext,
    mark_to_market_equity,
)
from wayfinder_paths.jobs.indicators import wilder_rsi
from wayfinder_paths.jobs.strategies._starter_utils import (
    MEAN_REVERSION_STOP_DEFAULTS,
    add_stop_atr,
    current_rows,
    merge_params,
    protection_cooldown_active,
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
        "symbol_weights": {},
        "rebalance_threshold": 0.10,
        "min_trade_notional": 25.0,
        "entry_order_type": "market",
        "entry_offset_atr": 0.0,
        "entry_ttl_bars": 1,
        "maker_fee_bps": 1.5,
        "maker_trade_through_bps": 1.0,
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
        if str(self.params["entry_order_type"]) not in {"market", "maker"}:
            raise ValueError("entry_order_type must be 'market' or 'maker'")
        configured_weights = self.params.get("symbol_weights") or {}
        if not isinstance(configured_weights, Mapping):
            raise ValueError("symbol_weights must be a mapping")
        unknown_symbols = set(configured_weights) - set(self.params["symbols"])
        if unknown_symbols:
            names = ", ".join(sorted(str(symbol) for symbol in unknown_symbols))
            raise ValueError(f"symbol_weights contains unknown symbols: {names}")
        self.symbol_weights = {
            symbol: float(configured_weights.get(symbol, self.params["weight_per_leg"]))
            for symbol in self.params["symbols"]
        }
        if any(
            not math.isfinite(weight) or weight < 0
            for weight in self.symbol_weights.values()
        ):
            raise ValueError("symbol_weights must contain finite non-negative values")
        gross = sum(self.symbol_weights.values())
        if gross > 1.0:
            self.symbol_weights = {
                symbol: weight / gross for symbol, weight in self.symbol_weights.items()
            }

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
        maker_entries: list[str] = []
        exit_symbols: list[str] = []
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
            if should_enter and str(self.params["entry_order_type"]) == "maker":
                maker_entries.append(symbol)
            if should_exit:
                exit_symbols.append(symbol)
            weights[symbol] = (
                0.0
                if should_exit
                else self.symbol_weights[symbol]
                if position is not None
                or (should_enter and str(self.params["entry_order_type"]) == "market")
                else 0.0
            )

        brackets = stop_brackets(ctx, symbols, self.params)
        if str(self.params["entry_order_type"]) == "maker":
            intents = [self._market_close(ctx, symbol) for symbol in exit_symbols]
            intents.extend(self._maker_entries(ctx, rows, maker_entries, brackets))
            return intents
        return target_weights_to_intents(
            ctx,
            weights,
            venue=str(self.params["venue"]),
            rebalance_threshold=float(self.params["rebalance_threshold"]),
            min_trade_notional=float(self.params["min_trade_notional"]),
            brackets=brackets,
        )

    def _market_close(self, ctx: ExecutionContext, symbol: str) -> dict[str, Any]:
        position = ctx.ledger.positions[symbol]
        return {
            "action": "CLOSE",
            "venue": str(self.params["venue"]),
            "symbol": symbol,
            "side": "sell" if position.side == "long" else "buy",
            "size": float(position.size),
            "reduce_only": True,
            "metadata": {"exit_reason": "rsi_recovery_or_max_hold"},
        }

    def _maker_entries(
        self,
        ctx: ExecutionContext,
        rows: dict[str, pd.Series],
        symbols: list[str],
        brackets: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        resting_symbols = {
            order.intent.symbol
            for order in ctx.resting_orders
            if not order.intent.reduce_only
        }
        equity = mark_to_market_equity(ctx)
        if equity <= 0:
            return []
        intents: list[dict[str, Any]] = []
        for symbol in symbols:
            if (
                self.symbol_weights[symbol] <= 0
                or symbol in resting_symbols
                or protection_cooldown_active(ctx, symbol)
            ):
                continue
            row = rows[symbol]
            close = float(row["close"])
            atr_value = float(row["starter_stop_atr"])
            limit_price = close - float(self.params["entry_offset_atr"]) * atr_value
            if not pd.notna(limit_price) or limit_price <= 0:
                continue
            intent: dict[str, Any] = {
                "action": "OPEN",
                "venue": str(self.params["venue"]),
                "symbol": symbol,
                "side": "buy",
                "notional": self.symbol_weights[symbol] * equity,
                "limit_price": limit_price,
                "time_in_force": "ALO",
                "expires_after_bars": int(self.params["entry_ttl_bars"]),
                "metadata": {
                    "entry_reason": "volume_confirmed_capitulation",
                    "signal_rsi": float(row["starter_capitulation_rsi"]),
                },
            }
            if symbol in brackets:
                intent["bracket"] = brackets[symbol]
            intents.append(intent)
        return intents


def build_strategy(
    params: dict[str, Any] | None = None,
) -> MixedVolumeCapitulationStrategy:
    return MixedVolumeCapitulationStrategy(params)
