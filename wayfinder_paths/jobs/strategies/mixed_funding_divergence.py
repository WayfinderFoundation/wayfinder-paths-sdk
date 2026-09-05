"""Funding divergence across a broad perp panel: crowded positioning that
price is not rewarding.

The signal (per symbol, on completed bars): the hourly funding rate's
z-score over a trailing window is beyond ``funding_z_entry`` while the
trailing ``confirm_return_bars`` return fails to confirm the crowd — longs
are paying up but price is flat or lower (short), or shorts are paying up but
price is flat or higher (long). ``oi_confirmation`` reads open interest over
``oi_lookback_bars`` as well: ``"building"`` requires the crowd to still be
adding (open interest up, the fade is against a crowd that has not started to
unwind), ``"unwinding"`` requires it to be shrinking (open interest down, the
trade joins a liquidation already under way), ``"off"`` ignores it.

Two execution styles share the implementation: ``entry_order_type="maker"``
rests a post-only order ``entry_offset_atr`` ATRs beyond the close for
``entry_ttl_bars`` bars; ``"market"`` takes the next open. Every position is
closed with a marketable order after ``max_hold_bars`` completed bars, or
earlier on a fill-relative catastrophe stop.

Inputs are the job's own bars plus the ``funding`` feature column (hourly
Hyperliquid funding, as-of merged) and, when confirmation is on, the
``open_interest`` feature column recorded by the wake refresh.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from wayfinder_paths.jobs.execution.primitives import (
    ExecutionContext,
    mark_to_market_equity,
)
from wayfinder_paths.jobs.indicators import bounded_span
from wayfinder_paths.jobs.strategies._starter_utils import (
    MEAN_REVERSION_STOP_DEFAULTS,
    add_stop_atr,
    current_rows,
    merge_params,
    protection_cooldown_active,
    stop_brackets,
)

FUNDING_COLUMN = "funding"
OPEN_INTEREST_COLUMN = "open_interest"
OI_CONFIRMATION_MODES = frozenset({"off", "building", "unwinding"})


class MixedFundingDivergenceStrategy:
    default_params: dict[str, Any] = {
        "symbols": ["BTC", "ETH", "SOL", "XRP"],
        "venue": "hyperliquid",
        "funding_z_window_bars": 2880,
        "funding_z_entry": 2.0,
        "confirm_return_bars": 96,
        "confirm_return_max": 0.0,
        "oi_confirmation": "off",
        "oi_lookback_bars": 96,
        "max_hold_bars": 96,
        "weight_per_leg": 0.05,
        "entry_order_type": "maker",
        "entry_offset_atr": 0.5,
        "entry_ttl_bars": 8,
        "min_trade_notional": 25.0,
        "maker_fee_bps": 1.5,
        "maker_trade_through_bps": 1.0,
        **MEAN_REVERSION_STOP_DEFAULTS,
    }

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = merge_params(self.default_params, params)
        if str(self.params["entry_order_type"]) not in {"market", "maker"}:
            raise ValueError("entry_order_type must be 'market' or 'maker'")
        if str(self.params["oi_confirmation"]) not in OI_CONFIRMATION_MODES:
            raise ValueError(
                "oi_confirmation must be one of "
                + ", ".join(sorted(OI_CONFIRMATION_MODES))
            )
        if float(self.params["weight_per_leg"]) <= 0:
            raise ValueError("weight_per_leg must be positive")
        self.warmup_bars = (
            max(
                int(self.params["funding_z_window_bars"]),
                int(self.params["confirm_return_bars"]),
                int(self.params["oi_lookback_bars"]),
                bounded_span(int(self.params["stop_atr_period"])),
            )
            + 4
        )

    def precompute(self, frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        window = int(self.params["funding_z_window_bars"])
        min_periods = max(10, window // 4)
        confirm_bars = int(self.params["confirm_return_bars"])
        confirm_max = float(self.params["confirm_return_max"])
        z_entry = float(self.params["funding_z_entry"])
        oi_mode = str(self.params["oi_confirmation"])
        oi_lookback = int(self.params["oi_lookback_bars"])
        derived: dict[str, pd.DataFrame] = {}
        for symbol, frame in frames.items():
            close = pd.to_numeric(frame["close"], errors="coerce")
            if FUNDING_COLUMN in frame.columns:
                funding = pd.to_numeric(frame[FUNDING_COLUMN], errors="coerce").ffill()
                mean = funding.rolling(window, min_periods=min_periods).mean()
                std = funding.rolling(window, min_periods=min_periods).std()
                funding_z = (funding - mean) / std.replace(0.0, np.nan)
            else:
                funding_z = pd.Series(np.nan, index=frame.index)
            confirm_return = close.pct_change(confirm_bars, fill_method=None)
            if OPEN_INTEREST_COLUMN in frame.columns:
                open_interest = pd.to_numeric(
                    frame[OPEN_INTEREST_COLUMN], errors="coerce"
                ).ffill()
                oi_change = open_interest / open_interest.shift(oi_lookback) - 1.0
            else:
                oi_change = pd.Series(np.nan, index=frame.index)
            if oi_mode == "building":
                oi_confirms = oi_change > 0
            elif oi_mode == "unwinding":
                oi_confirms = oi_change < 0
            else:
                oi_confirms = pd.Series(True, index=frame.index)
            short_signal = (
                (funding_z > z_entry) & (confirm_return <= confirm_max) & oi_confirms
            )
            long_signal = (
                (funding_z < -z_entry) & (confirm_return >= -confirm_max) & oi_confirms
            )
            signal = pd.Series(0.0, index=frame.index)
            signal[short_signal.fillna(False)] = -1.0
            signal[long_signal.fillna(False)] = 1.0
            derived[symbol] = pd.DataFrame(
                {
                    "starter_funding_z": funding_z,
                    "starter_confirm_return": confirm_return,
                    "starter_oi_change": oi_change,
                    "starter_signal": signal,
                }
            )
        return add_stop_atr(derived, frames, period=int(self.params["stop_atr_period"]))

    def decide(self, ctx: ExecutionContext) -> list[dict[str, Any]]:
        symbols = list(self.params["symbols"])
        if ctx.bar_index < self.warmup_bars:
            return []
        rows = current_rows(
            ctx, symbols, required_columns=("starter_signal", "starter_stop_atr")
        )
        if rows is None:
            return []
        intents: list[dict[str, Any]] = []
        entries: list[tuple[str, int]] = []
        max_hold = int(self.params["max_hold_bars"])
        for symbol, row in rows.items():
            signal = (
                int(row["starter_signal"]) if pd.notna(row["starter_signal"]) else 0
            )
            position = ctx.ledger.positions.get(symbol)
            if position is not None:
                position_side = 1 if position.side == "long" else -1
                if position.bars_held >= max_hold - 1 or (
                    signal != 0 and signal != position_side
                ):
                    intents.append(
                        self._market_close(
                            position,
                            symbol,
                            "max_hold"
                            if signal == 0 or signal == position_side
                            else "signal_flipped",
                        )
                    )
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
        weight = float(self.params["weight_per_leg"])
        notional = weight * equity
        if notional < float(self.params["min_trade_notional"]):
            return []
        brackets = stop_brackets(ctx, [symbol for symbol, _ in entries], self.params)
        intents: list[dict[str, Any]] = []
        for symbol, signal in entries:
            if symbol in resting_symbols or protection_cooldown_active(ctx, symbol):
                continue
            row = rows[symbol]
            close = float(row["close"])
            atr_value = float(row["starter_stop_atr"])
            if not (close > 0 and pd.notna(atr_value) and atr_value > 0):
                continue
            intent: dict[str, Any] = {
                "action": "OPEN",
                "venue": str(self.params["venue"]),
                "symbol": symbol,
                "side": "buy" if signal > 0 else "sell",
                "notional": notional,
                "metadata": {
                    "entry_reason": "funding_divergence",
                    "signal_funding_z": float(row["starter_funding_z"]),
                    "signal_confirm_return": float(row["starter_confirm_return"]),
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
) -> MixedFundingDivergenceStrategy:
    return MixedFundingDivergenceStrategy(params)
