"""Liquidation flush across a broad perp panel: a large one-day move that
open interest did not survive.

The signal (per symbol, on completed bars): the trailing
``flush_return_bars`` return is at least ``flush_return_min`` in magnitude
while open interest fell by at least ``flush_oi_drop_min`` over
``flush_oi_bars``. Positions were force-closed into the move rather than
added to it, so the move is faded: buy a long liquidation flush, sell a short
squeeze flush. The reversion is short lived: a position rides while the
flush condition persists and is closed with a marketable order once
``hold_after_signal_bars`` completed bars have passed since the last flush
bar (or immediately when the flush flips side), or earlier on a
fill-relative catastrophe stop; ``sides`` limits the book to ``"long"``,
``"short"`` or ``"both"``.

Two execution styles share the implementation: ``entry_order_type="maker"``
rests a post-only order ``entry_offset_atr`` ATRs beyond the close for
``entry_ttl_bars`` bars; ``"market"`` takes the next open.

Inputs are the job's own bars plus the ``open_interest`` feature column
recorded by the wake refresh (as-of merged); the book stands down while the
column is missing or stale.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.primitives import (
    ExecutionContext,
    mark_to_market_equity,
)
from wayfinder_paths.jobs.indicators import (
    FEED_OPEN_INTEREST,
    FLUSH_SIDES,
    bars_since_signal,
    bounded_span,
    liquidation_flush_signal,
)
from wayfinder_paths.jobs.strategies._starter_utils import (
    RANKING_STOP_DEFAULTS,
    add_stop_atr,
    current_rows,
    merge_params,
    protection_cooldown_active,
    stop_brackets,
)

OPEN_INTEREST_COLUMN = FEED_OPEN_INTEREST
SIDE_MODES = FLUSH_SIDES


class MixedLiquidationFlushStrategy:
    default_params: dict[str, Any] = {
        "symbols": ["BTC", "ETH", "SOL", "XRP"],
        "venue": "hyperliquid",
        "flush_return_bars": 96,
        "flush_return_min": 0.06,
        "flush_oi_bars": 96,
        "flush_oi_drop_min": 0.10,
        "sides": "both",
        "hold_after_signal_bars": 24,
        "weight_per_leg": 0.05,
        "entry_order_type": "market",
        "entry_offset_atr": 0.5,
        "entry_ttl_bars": 1,
        # Cascade guards: skip entries while the latest bar's range is still
        # wider than this many ATRs (0 disables), and/or until a bar closes in
        # the fade direction (the cascade has at least paused).
        "entry_max_bar_range_atr": 0.0,
        "entry_confirm_bar": False,
        "min_trade_notional": 25.0,
        "maker_fee_bps": 1.5,
        "maker_trade_through_bps": 1.0,
        "stop_atr_period": 24,
        **RANKING_STOP_DEFAULTS,
    }

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = merge_params(self.default_params, params)
        if str(self.params["entry_order_type"]) not in {"market", "maker"}:
            raise ValueError("entry_order_type must be 'market' or 'maker'")
        if str(self.params["sides"]) not in SIDE_MODES:
            raise ValueError("sides must be one of " + ", ".join(sorted(SIDE_MODES)))
        if float(self.params["flush_return_min"]) <= 0:
            raise ValueError("flush_return_min must be positive")
        if float(self.params["flush_oi_drop_min"]) <= 0:
            raise ValueError("flush_oi_drop_min must be positive")
        if float(self.params["weight_per_leg"]) <= 0:
            raise ValueError("weight_per_leg must be positive")
        if int(self.params["hold_after_signal_bars"]) < 1:
            raise ValueError("hold_after_signal_bars must be at least 1")
        self.warmup_bars = (
            max(
                int(self.params["flush_return_bars"]),
                int(self.params["flush_oi_bars"]),
                bounded_span(int(self.params["stop_atr_period"])),
            )
            + 4
        )

    def precompute(self, frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        return_bars = int(self.params["flush_return_bars"])
        return_min = float(self.params["flush_return_min"])
        oi_bars = int(self.params["flush_oi_bars"])
        oi_drop_min = float(self.params["flush_oi_drop_min"])
        sides = str(self.params["sides"])
        derived: dict[str, pd.DataFrame] = {}
        for symbol, frame in frames.items():
            # The shared indicator (also behind the signal library's
            # positioning family and the flush/oichg chart specs).
            flush = liquidation_flush_signal(
                frame,
                return_bars=return_bars,
                return_min=return_min,
                oi_bars=oi_bars,
                oi_drop_min=oi_drop_min,
                sides=sides,
            )
            derived[symbol] = pd.DataFrame(
                {
                    "starter_flush_return": flush["flush_return"],
                    "starter_oi_change": flush["oi_change"],
                    "starter_signal": flush["signal"],
                    # Bars since the last flush bar of either side (0 on a
                    # flush bar): a pure function of the bar history, so
                    # restarts cannot lose it.
                    "starter_signal_age": bars_since_signal(flush["signal"]),
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
                "starter_signal",
                "starter_signal_age",
                "starter_stop_atr",
            ),
        )
        if rows is None:
            return []
        intents: list[dict[str, Any]] = []
        entries: list[tuple[str, int]] = []
        hold_after = int(self.params["hold_after_signal_bars"])
        for symbol, row in rows.items():
            signal = (
                int(row["starter_signal"]) if pd.notna(row["starter_signal"]) else 0
            )
            age = (
                int(row["starter_signal_age"])
                if pd.notna(row["starter_signal_age"])
                else hold_after
            )
            position = ctx.ledger.positions.get(symbol)
            if position is not None:
                position_side = 1 if position.side == "long" else -1
                if signal != 0 and signal != position_side:
                    intents.append(
                        self._market_close(position, symbol, "signal_flipped")
                    )
                elif signal == 0 and age >= hold_after:
                    intents.append(self._market_close(position, symbol, "hold_expired"))
                continue
            if signal != 0:
                entries.append((symbol, signal))
        if entries:
            intents.extend(self._entries(ctx, rows, entries))
        return intents

    def _market_close(self, position: Any, symbol: str, reason: str) -> dict[str, Any]:
        return {
            "action": "CLOSE",
            "venue": str(self.params["venue"]),
            "symbol": symbol,
            "side": "sell" if position.side == "long" else "buy",
            "size": float(position.size),
            "reduce_only": True,
            "metadata": {"exit_reason": reason},
        }

    def _entries(
        self,
        ctx: ExecutionContext,
        rows: dict[str, pd.Series],
        entries: list[tuple[str, int]],
    ) -> list[dict[str, Any]]:
        resting_symbols = {
            order.intent.symbol
            for order in ctx.resting_orders
            if not order.intent.reduce_only
        }
        equity = mark_to_market_equity(ctx)
        if equity <= 0:
            return []
        maker = str(self.params["entry_order_type"]) == "maker"
        notional = float(self.params["weight_per_leg"]) * equity
        if notional < float(self.params["min_trade_notional"]):
            return []
        brackets = stop_brackets(ctx, [symbol for symbol, _ in entries], self.params)
        max_range_atr = float(self.params["entry_max_bar_range_atr"])
        confirm_bar = bool(self.params["entry_confirm_bar"])
        intents: list[dict[str, Any]] = []
        for symbol, signal in entries:
            if symbol in resting_symbols or protection_cooldown_active(ctx, symbol):
                continue
            row = rows[symbol]
            close = float(row["close"])
            atr_value = float(row["starter_stop_atr"])
            if not (close > 0 and pd.notna(atr_value) and atr_value > 0):
                continue
            bar_range = float(row["high"]) - float(row["low"])
            if max_range_atr > 0 and bar_range > max_range_atr * atr_value:
                continue
            if confirm_bar and signal * (close - float(row["open"])) < 0:
                continue
            intent: dict[str, Any] = {
                "action": "OPEN",
                "venue": str(self.params["venue"]),
                "symbol": symbol,
                "side": "buy" if signal > 0 else "sell",
                "notional": notional,
                "metadata": {
                    "entry_reason": "liquidation_flush",
                    "signal_flush_return": float(row["starter_flush_return"]),
                    "signal_oi_change": float(row["starter_oi_change"]),
                },
            }
            if maker:
                offset = float(self.params["entry_offset_atr"]) * atr_value
                limit_price = close - offset if signal > 0 else close + offset
                if limit_price <= 0:
                    continue
                intent["limit_price"] = limit_price
                intent["time_in_force"] = "ALO"
                intent["expires_after_bars"] = int(self.params["entry_ttl_bars"])
            if symbol in brackets:
                intent["bracket"] = brackets[symbol]
            intents.append(intent)
        return intents


def build_strategy(
    params: dict[str, Any] | None = None,
) -> MixedLiquidationFlushStrategy:
    return MixedLiquidationFlushStrategy(params)
