"""Shared skeleton for the ported SNX/IMX short-momentum strategies.

Ported from the live prod scripts (vault-backend ops_runs pull 2026-06-29).
Behavior mapping onto the jobs_v1 engine:

- NewLow5 entry, post-exit re-arm (no new short until a LATER bar closes
  above SMA20), and Min-hold gating are identical to prod.
- The 7% adverse stop is expressed ONLY as a bracket stop_loss: the engine
  emulates it intrabar on OHLC highs (ohlc_rules.use_high_low_for_stops),
  which is equivalent to prod's native Hyperliquid stop order — no duplicate
  close-based stop check here.
- bars_since_entry = position.bars_held + 1 restores prod's completed-candle
  counting under the engine's next-bar-open fill model: the Min-2 gate
  unlocks at the close of entry+2 in both systems.
- Strategy scratch state lives in ctx.strategy_state (keys: engaged, rearm,
  rearm_since, plus per-strategy extras). `engaged` mirrors whether THIS
  strategy owns the position; if the engine's bracket stop (or anything
  external) removed the position, the flat-while-engaged transition re-arms —
  same as prod's shared-wallet state sync.
"""

from __future__ import annotations

from typing import Any

from wayfinder_paths.jobs.execution.primitives import (
    ExecutionContext,
    mark_to_market_equity,
)
from wayfinder_paths.jobs.strategies.indicators import sma

MAX_LEVERAGE = 25.0


class ShortMomentumStrategy:
    """Template method: subclasses supply indicators, exit reasons, and entry
    filters via hooks.

    Sizing: `sizing="fixed_notional"` (default, prod behavior) sizes every
    entry at `notional_usd`. `sizing="compound"` sizes entries at
    current equity × `leverage` — real compounding via position sizing,
    tracked bar-by-bar from the ledger (the honest version of the old
    cumprod-return convention). Set `initial_capital` in execution_params so
    equity has an explicit base."""

    default_params: dict[str, Any] = {}

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        merged = {
            "symbol": "SNX",
            "venue": "hyperliquid",
            "notional_usd": 1000.0,
            "sizing": "fixed_notional",
            "leverage": 1.0,
            "min_notional_usd": 10.0,
            "low_period": 5,
            "sma_period": 20,
            "stop_pct": 0.07,
            "min_hold": 2,
        }
        merged.update(self.default_params)
        merged.update(params or {})
        self.params = merged

    def entry_notional(self, ctx: ExecutionContext) -> float:
        if str(self.params.get("sizing") or "fixed_notional") != "compound":
            return float(self.params["notional_usd"])
        raw = self.params.get("leverage")
        leverage = 1.0 if raw is None else float(raw)  # 0 must NOT default to 1
        if not 0 < leverage <= MAX_LEVERAGE:
            raise ValueError(
                f"leverage must be in (0, {MAX_LEVERAGE:g}], got {leverage}"
            )
        return max(mark_to_market_equity(ctx), 0.0) * leverage

    # ── hooks ────────────────────────────────────────────────────────────
    def min_bars(self) -> int:
        return max(self.params["low_period"], self.params["sma_period"]) + 2

    def compute_indicators(
        self,
        highs: list[float],
        lows: list[float],
        closes: list[float],
    ) -> dict[str, Any]:
        return {}

    def exit_reason(
        self,
        *,
        closes: list[float],
        sma20: list[float],
        indicators: dict[str, Any],
        state: dict[str, Any],
        entry_price: float,
        bars_since_entry: int,
    ) -> str | None:
        raise NotImplementedError

    def entry_allowed(
        self,
        *,
        closes: list[float],
        sma20: list[float],
        indicators: dict[str, Any],
    ) -> bool:
        return True

    def on_entry(
        self,
        state: dict[str, Any],
        *,
        closes: list[float],
        indicators: dict[str, Any],
    ) -> None:
        return None

    # ── shared decide flow ────────────────────────────────────────────────
    def decide(self, ctx: ExecutionContext) -> list[dict[str, Any]]:
        symbol = str(self.params["symbol"])
        frame = ctx.view.symbol_frame(symbol)  # read-only, no defensive copy
        if len(frame) < self.min_bars():
            return []
        closes = frame["close"].to_numpy(dtype=float).tolist()
        highs = frame["high"].to_numpy(dtype=float).tolist()
        lows = frame["low"].to_numpy(dtype=float).tolist()

        state = ctx.strategy_state
        position = ctx.ledger.positions.get(symbol)
        sma20 = sma(closes, int(self.params["sma_period"]))
        indicators = self.compute_indicators(highs, lows, closes)

        # External exit detection: the engine's bracket stop (evaluated on
        # OHLC highs — see ohlc_rules.use_high_low_for_stops) or any outside
        # close removed the position we opened.
        if state.get("engaged") and position is None:
            self._arm_rearm(state, ctx.timestamp)

        # Re-arm lift: first bar AFTER rearm_since whose close > SMA20.
        if state.get("rearm") and state.get("rearm_since"):
            since = str(state["rearm_since"])
            # times computed lazily: only the (rare) re-arm scan needs the
            # per-bar isoformat strings, and they cost O(bars) to build.
            times = [ts.isoformat() for ts in frame["timestamp"]]
            for index in range(len(closes)):
                if times[index] > since and sma20[index] and closes[index] > sma20[index]:
                    state["rearm"] = False
                    state["rearm_since"] = None
                    break

        if position is not None:
            state["engaged"] = True
            bars_since_entry = position.bars_held + 1
            entry_price = float(
                state.get("entry_ref") or position.avg_price or closes[-1]
            )
            reason = self.exit_reason(
                closes=closes,
                sma20=sma20,
                indicators=indicators,
                state=state,
                entry_price=entry_price,
                bars_since_entry=bars_since_entry,
            )
            if reason:
                self._arm_rearm(state, ctx.timestamp)
                return [
                    {
                        "action": "CLOSE",
                        "venue": str(self.params["venue"]),
                        "symbol": symbol,
                        "side": "buy",
                        "size": position.size,
                        "reduce_only": True,
                        "metadata": {"exit_reason": reason},
                    }
                ]
            return []

        if state.get("rearm") or state.get("engaged"):
            return []

        low_period = int(self.params["low_period"])
        current_close = closes[-1]
        prev_low = (
            min(closes[-(low_period + 1) : -1])
            if len(closes) > low_period
            else current_close
        )
        if current_close >= prev_low:
            return []
        if not self.entry_allowed(closes=closes, sma20=sma20, indicators=indicators):
            return []
        notional = self.entry_notional(ctx)
        if (
            str(self.params.get("sizing") or "fixed_notional") == "compound"
            and notional < float(self.params["min_notional_usd"])
        ):
            return []
        size = round(notional / current_close, 1)
        if size <= 0:
            return []
        state["engaged"] = True
        state["rearm"] = False
        state["rearm_since"] = None
        state["entry_ref"] = current_close
        self.on_entry(state, closes=closes, indicators=indicators)
        return [
            {
                "action": "OPEN",
                "venue": str(self.params["venue"]),
                "symbol": symbol,
                "side": "sell",
                "size": size,
                "bracket": {
                    "stop_loss": current_close * (1 + float(self.params["stop_pct"]))
                },
                "metadata": {"entry_reason": "new_low_5"},
            }
        ]

    def _arm_rearm(self, state: dict[str, Any], timestamp: str) -> None:
        state["engaged"] = False
        state["rearm"] = True
        state["rearm_since"] = timestamp
        state["entry_ref"] = None
        state["entry_atr"] = None
