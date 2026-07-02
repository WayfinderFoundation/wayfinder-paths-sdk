"""IMX ATR Target Short — 1h NewLow5 entry; exits at entry - 2*ATR(14) target
or SMA20 bounce (both Min-2 gated); 7% bracket stop. Port of the live
imx-atr-target job.

The ATR target stays a decide()-level close-based exit rather than a bracket
take_profit: a bracket TP would fire intrabar on lows and skip the Min-2 gate,
which would be a real behavioral change from prod (prod has no native TP
order). entry_atr and the entry reference price are captured at the decision
bar in strategy_state, exactly like prod's state file."""

from __future__ import annotations

from typing import Any

from wayfinder_paths.jobs.strategies._base import ShortMomentumStrategy
from wayfinder_paths.jobs.strategies.indicators import simple_atr


class ImxAtrTargetStrategy(ShortMomentumStrategy):
    default_params = {
        "symbol": "IMX",
        "notional_usd": 1000.0,
        "atr_period": 14,
        "atr_mult": 2.0,
    }

    def min_bars(self) -> int:
        return (
            max(
                self.params["low_period"],
                self.params["sma_period"],
                self.params["atr_period"],
            )
            + 2
        )

    def compute_indicators(
        self, highs: list[float], lows: list[float], closes: list[float]
    ) -> dict[str, Any]:
        return {
            "atr": simple_atr(highs, lows, closes, int(self.params["atr_period"]))
        }

    def on_entry(
        self,
        state: dict[str, Any],
        *,
        closes: list[float],
        indicators: dict[str, Any],
    ) -> None:
        state["entry_atr"] = indicators["atr"][-1]

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
        entry_atr = float(state.get("entry_atr") or indicators["atr"][-1])
        target = entry_price - float(self.params["atr_mult"]) * entry_atr
        if closes[-1] <= target:
            return "atr_target"
        if closes[-1] > sma20[-1]:
            return "sma20_bounce"
        return None


def build_strategy(params: dict[str, Any] | None = None) -> ImxAtrTargetStrategy:
    return ImxAtrTargetStrategy(params)
