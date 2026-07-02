"""IMX Momentum Short — 1h NewLow5 entry with SMA50 gap filter; SMA20 bounce +
SMA50 floor exits (both Min-2 gated); 7% bracket stop. Port of the live
imx-momentum job."""

from __future__ import annotations

from typing import Any

from wayfinder_paths.jobs.strategies._base import ShortMomentumStrategy
from wayfinder_paths.jobs.strategies.indicators import sma


class ImxMomentumStrategy(ShortMomentumStrategy):
    default_params = {
        "symbol": "IMX",
        "notional_usd": 3000.0,
        "floor_sma": 50,
        "floor_gap_pct": 0.01,
    }

    def min_bars(self) -> int:
        return (
            max(
                self.params["low_period"],
                self.params["sma_period"],
                self.params["floor_sma"],
            )
            + 2
        )

    def compute_indicators(
        self, highs: list[float], lows: list[float], closes: list[float]
    ) -> dict[str, Any]:
        return {"sma_floor": sma(closes, int(self.params["floor_sma"]))}

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
        # Both exits gated by the Min-2 hold (matches prod); the 7% adverse
        # move is the bracket stop_loss and fires ungated.
        if bars_since_entry < int(self.params["min_hold"]):
            return None
        exit_floor = closes[-1] < indicators["sma_floor"][-1]
        exit_sma20 = closes[-1] > sma20[-1]
        if exit_floor:
            return "sma50_floor"
        if exit_sma20:
            return "sma20_bounce"
        return None

    def entry_allowed(
        self,
        *,
        closes: list[float],
        sma20: list[float],
        indicators: dict[str, Any],
    ) -> bool:
        # SMA50 gap filter: skip entries within 1% of the floor so the floor
        # exit retains a buffer (prod: close > sma50 * 0.99 -> skip).
        floor = indicators["sma_floor"][-1]
        gap = 1 - float(self.params["floor_gap_pct"])
        return not (floor and closes[-1] > floor * gap)


def build_strategy(params: dict[str, Any] | None = None) -> ImxMomentumStrategy:
    return ImxMomentumStrategy(params)
