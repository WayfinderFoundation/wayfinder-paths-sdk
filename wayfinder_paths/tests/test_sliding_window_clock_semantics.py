"""The live shape: decide() sees exactly `warmup_bars` bars every tick. Ages,
cooldowns and cadence must be measured on the global bar ordinal; a stored
bar_index never advances. Pins the failure, the primitive and the preview."""

from __future__ import annotations

import types
from datetime import UTC, datetime, timedelta
from typing import Any

from wayfinder_paths.jobs.execution import OrderIntent
from wayfinder_paths.jobs.execution.simulator import (
    PreparedExecutionDataset,
    simulate_execution,
)
from wayfinder_paths.jobs.execution.validation import sequence_preview

SPEC = {
    "market_kind": "perp",
    "data_contract": {"bar_interval": "1h", "symbols": ["SNX"]},
}
PARAMS = {"warmup_bars": 20}
ARM_TO_ENTRY_BARS = 5
HOLD_BARS = 3
COOLDOWN_BARS = 8
CADENCE = 6


def _hourly_rows(count: int = 300) -> list[dict[str, Any]]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    for index in range(count):
        price = 100.0 + (index % 17) * 0.1
        rows.append(
            {
                "timestamp": (start + timedelta(hours=index)).isoformat(),
                "symbol": "SNX",
                "open": price,
                "high": price + 0.3,
                "low": price - 0.3,
                "close": price + 0.1,
                "volume": 100.0,
            }
        )
    return rows


def _machine(clock: str) -> Any:
    """Arm, enter ARM_TO_ENTRY_BARS later, hold HOLD_BARS, then cool down
    COOLDOWN_BARS before re-arming; count cadence ticks. `clock` selects the
    stamp: the global ordinal (correct) or the bounded bar_index (stuck)."""

    def build(params: dict[str, Any]) -> Any:
        def now(ctx: Any) -> int:
            return int(ctx.bar_ordinal if clock == "ordinal" else ctx.bar_index)

        def decide(ctx: Any) -> list[OrderIntent]:
            state = ctx.strategy_state
            if ctx.bar_index < PARAMS["warmup_bars"]:
                return []
            if ctx.every_n_bars(CADENCE):
                state["cadence_ticks"] = int(state.get("cadence_ticks") or 0) + 1
            stamp = now(ctx)
            phase = state.get("phase") or "idle"
            if phase == "idle":
                state["phase"], state["armed_at"] = "armed", stamp
                return []
            if phase == "armed" and stamp - int(state["armed_at"]) >= ARM_TO_ENTRY_BARS:
                state["phase"], state["entered_at"] = "in", stamp
                return [
                    OrderIntent(
                        action="OPEN",
                        venue="hyperliquid",
                        symbol="SNX",
                        side="long",
                        size=1.0,
                    )
                ]
            if phase == "in" and stamp - int(state["entered_at"]) >= HOLD_BARS:
                state["phase"], state["closed_at"] = "cooling", stamp
                return [
                    OrderIntent(
                        action="CLOSE",
                        venue="hyperliquid",
                        symbol="SNX",
                        side="long",
                        reduce_only=True,
                    )
                ]
            if phase == "cooling" and stamp - int(state["closed_at"]) >= COOLDOWN_BARS:
                state["phase"], state["armed_at"] = "armed", stamp
            return []

        return types.SimpleNamespace(decide=decide)

    return build


def _ordinals(intents: list[dict[str, Any]], action: str) -> list[int]:
    return [
        int(datetime.fromisoformat(str(row["timestamp"])).timestamp() // 3600)
        for row in intents
        if str(row.get("action")).upper() == action
    ]


def test_ordinal_clock_expires_cools_and_paces_under_a_fixed_window() -> None:
    dataset = PreparedExecutionDataset.from_rows(_hourly_rows(), {})
    result = simulate_execution(
        _machine("ordinal"), dataset, SPEC, PARAMS, record_strategy_state=True
    )

    opens = _ordinals(result.trace["intents"], "OPEN")
    closes = _ordinals(result.trace["intents"], "CLOSE")
    assert len(opens) >= 10 and len(closes) >= 9
    # Expiry: the first entry is exactly ARM_TO_ENTRY_BARS after arming on the
    # first decision bar (bar index 19 of the 300-bar series).
    first_decision = (
        int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() // 3600)
        + PARAMS["warmup_bars"]
        - 1
    )
    assert opens[0] == first_decision + ARM_TO_ENTRY_BARS
    # Hold and cooldown: close HOLD_BARS after each open, next open
    # COOLDOWN_BARS + ARM_TO_ENTRY_BARS after each close.
    assert all(c - o == HOLD_BARS for o, c in zip(opens, closes, strict=False))
    assert all(
        n - c == COOLDOWN_BARS + ARM_TO_ENTRY_BARS
        for c, n in zip(closes, opens[1:], strict=False)
    )
    # Cadence: one tick per CADENCE bars over the decision bars.
    digests = [
        row["strategy_state_digest"].get("cadence_ticks")
        for row in result.trace["runs"]
    ]
    decision_bars = len(result.trace["runs"]) - (PARAMS["warmup_bars"] - 1)
    assert abs(len({d for d in digests if d}) - decision_bars // CADENCE) <= 1

    preview = sequence_preview(_machine("ordinal"), dataset, SPEC, PARAMS, bars=200)
    assert preview["status"] == "entries" and preview["entries"] >= 8


def test_bar_index_clock_arms_once_and_never_enters() -> None:
    dataset = PreparedExecutionDataset.from_rows(_hourly_rows(), {})
    result = simulate_execution(
        _machine("bar_index"), dataset, SPEC, PARAMS, record_strategy_state=True
    )

    assert _ordinals(result.trace["intents"], "OPEN") == []
    phases = {
        row["strategy_state_digest"].get("phase")
        for row in result.trace["runs"]
        if row.get("strategy_state_digest")
    }
    assert len(phases) == 1  # armed forever

    preview = sequence_preview(_machine("bar_index"), dataset, SPEC, PARAMS, bars=200)
    assert preview["status"] == "armed_no_entry"
    assert preview["entries"] == 0
    assert preview["state_keys"]["phase"]["changes"] == 1
