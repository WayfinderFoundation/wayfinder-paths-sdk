"""SNX Momentum Short — 1h NewLow5 entry; SuperTrend(7,2.5) floor (Min-2) +
SMA20 bounce exits; 7% bracket stop. Port of the live snx-momentum job."""

from __future__ import annotations

from typing import Any

from wayfinder_paths.jobs.strategies._base import ShortMomentumStrategy
from wayfinder_paths.jobs.strategies.indicators import supertrend


class SnxMomentumStrategy(ShortMomentumStrategy):
    default_params = {
        "symbol": "SNX",
        "notional_usd": 2500.0,
        "st_period": 7,
        "st_mult": 2.5,
    }
    extra_period_keys = ("st_period",)

    def compute_indicators(
        self, highs: list[float], lows: list[float], closes: list[float]
    ) -> dict[str, Any]:
        return {
            "supertrend": supertrend(
                highs,
                lows,
                closes,
                int(self.params["st_period"]),
                float(self.params["st_mult"]),
            )
        }

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
        # SuperTrend floor needs the Min-2 hold; the SMA20 bounce is ungated
        # (matches prod). The 7% adverse move is the bracket stop_loss.
        exit_floor = indicators["supertrend"][-1] == -1 and bars_since_entry >= int(
            self.params["min_hold"]
        )
        exit_sma20 = closes[-1] > sma20[-1]
        if exit_floor:
            return "st_floor"
        if exit_sma20:
            return "sma20_bounce"
        return None


def build_strategy(params: dict[str, Any] | None = None) -> SnxMomentumStrategy:
    return SnxMomentumStrategy(params)
