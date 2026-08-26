"""Scheduled strategy wrapper: shadow default, seeded gate, bracket lifecycle."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from wayfinder_paths.jobs.execution import ExecutionSpec, PositionLedger
from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    ExecutionContext,
    StateSnapshot,
)
from wayfinder_paths.jobs.strategies.pattern_match_universe import (
    STATE_KEY,
    PatternMatchUniverseStrategy,
)


def _rows(count: int = 900) -> list[dict]:
    index = np.arange(count)
    close = 100 * np.exp(0.0004 * index + 0.015 * np.sin(2 * np.pi * index / 120))
    open_ = close * np.exp(-0.0002)
    timestamps = pd.date_range("2026-01-01T00:00:00Z", periods=count, freq="15min")
    return [
        {
            "timestamp": timestamp,
            "symbol": "TEST",
            "open": float(open_price),
            "high": float(max(open_price, close_price) * 1.002),
            "low": float(min(open_price, close_price) * 0.998),
            "close": float(close_price),
            "volume": 1_000_000.0,
            "funding_rate": 0.0,
            "premium": 0.0,
            "funding_observed_at": timestamp.floor("h"),
            "funding_payment_rate": 0.0,
        }
        for timestamp, open_price, close_price in zip(
            timestamps, open_, close, strict=True
        )
    ]


def _calibration(tmp_path) -> str:
    path = tmp_path / "calibration.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "markets": {
                    "TEST": {
                        "timing": {"96": 0.0},
                        "vote": 0.5,
                        "steepness": -10.0,
                        "development_signals": 100,
                    }
                },
                "lane_seeds": {
                    lane: {
                        "as_of": "2025-12-31T00:00:00Z",
                        "resolved_count": 50,
                        "recent_net_returns": [0.001] * 10,
                    }
                    for lane in ("TEST|long", "TEST|short")
                },
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def _strategy(tmp_path, *, allow_orders: bool) -> PatternMatchUniverseStrategy:
    return PatternMatchUniverseStrategy(
        {
            "symbols": ["TEST"],
            "calibration_path": _calibration(tmp_path),
            "warmup_bars": 300,
            "minimum_history_bars": 300,
            "history_limit": 800,
            "rerank_pool": 8,
            "top_matches": 3,
            "signal_every_bars": 1,
            "minimum_volume_24h_usd": 0.0,
            "allow_orders": allow_orders,
        }
    )


def _ctx(rows: list[dict], state: dict, ledger=None) -> ExecutionContext:
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "15m"
    return ExecutionContext(
        view=CompletedBarsView.from_rows(rows),
        ledger=ledger or PositionLedger(),
        state_snapshot=StateSnapshot(status="valid"),
        capacity=None,
        params={"initial_capital": 10_000.0},
        timestamp=pd.Timestamp(rows[-1]["timestamp"]).isoformat(),
        execution_spec=spec,
        strategy_state=state,
    )


def test_strategy_shadows_by_default_even_when_seeded_lane_passes(tmp_path) -> None:
    strategy = _strategy(tmp_path, allow_orders=False)
    strategy.params["universe_symbols"] = ["TEST", "NEW"]
    state: dict = {}
    intents = strategy.decide(_ctx(_rows(), state))

    assert intents == []
    model_state = state[STATE_KEY]
    assert "TEST" in model_state["pending_shadows"]
    latest = model_state["latest"]["TEST"]
    assert latest["lane_gate"]["allowed"] is True
    assert latest["lane_gate"]["resolved_count"] == 50
    assert model_state["latest"]["NEW"]["reason"] == "uncalibrated"
    assert model_state["latest"]["NEW"]["query_time"] is not None


def test_strategy_scores_off_phase_without_admitting_candidate(tmp_path) -> None:
    strategy = _strategy(tmp_path, allow_orders=True)
    rows = _rows()
    context = _ctx(rows, {})
    interval_seconds = 15 * 60
    current_phase = (
        int(pd.Timestamp(rows[-1]["timestamp"]).timestamp() // interval_seconds) % 4
    )
    strategy.params["signal_every_bars"] = 4
    strategy.params["signal_offset"] = (current_phase + 1) % 4

    intents = strategy.decide(context)

    assert intents == []
    model_state = context.strategy_state[STATE_KEY]
    assert model_state["pending_shadows"] == {}
    latest = model_state["latest"]["TEST"]
    assert latest["candidate_phase"] is False
    assert latest["model_actionable"] is True
    assert latest["actionable"] is False
    assert latest["reason"] == "off_validated_hourly_phase"


def test_explicit_order_opt_in_uses_fill_relative_symmetric_bracket(tmp_path) -> None:
    strategy = _strategy(tmp_path, allow_orders=True)
    state: dict = {}
    intents = strategy.decide(_ctx(_rows(), state))

    assert len(intents) == 1
    intent = intents[0]
    assert intent["action"] == "OPEN"
    assert intent["side"] == "sell"
    assert intent["notional"] == 100.0
    assert intent["bracket"]["stop_loss_pct"] == pytest.approx(
        intent["bracket"]["take_profit_pct"]
    )
    assert intent["bracket"]["policy"] == "conservative"


def test_shadow_collision_resolves_to_stop_and_updates_lane_history(tmp_path) -> None:
    strategy = _strategy(tmp_path, allow_orders=False)
    state: dict = {}
    rows = _rows()
    strategy.decide(_ctx(rows, state))
    final = rows[-1]
    rows.append(
        {
            **final,
            "timestamp": pd.Timestamp(final["timestamp"]) + pd.Timedelta(minutes=15),
            "open": final["close"],
            "high": final["close"] * 1.20,
            "low": final["close"] * 0.80,
        }
    )
    strategy.decide(_ctx(rows, state))

    model_state = state[STATE_KEY]
    resolution = model_state["recent_resolutions"][-1]
    assert resolution["exit_reason"] == "both_stop"
    assert resolution["bars_held"] == 1
    assert resolution["net_return"] < 0
    assert resolution["feedback_eligible"] is True
    assert model_state["lane_history"]["TEST|short"]["resolved_count"] == 51


def test_shadow_with_missing_funding_cannot_update_feedback(tmp_path) -> None:
    strategy = _strategy(tmp_path, allow_orders=False)
    state: dict = {}
    rows = _rows()
    strategy.decide(_ctx(rows, state))
    lane_before = state[STATE_KEY]["lane_history"]["TEST|short"]["resolved_count"]
    final = rows[-1]
    rows.append(
        {
            **final,
            "timestamp": pd.Timestamp(final["timestamp"]) + pd.Timedelta(minutes=15),
            "open": final["close"],
            "high": final["close"] * 1.20,
            "low": final["close"] * 0.80,
            "funding_payment_rate": None,
        }
    )

    strategy.decide(_ctx(rows, state))

    model_state = state[STATE_KEY]
    resolution = model_state["recent_resolutions"][-1]
    assert resolution["feedback_eligible"] is False
    assert resolution["net_return"] is None
    assert model_state["lane_history"]["TEST|short"]["resolved_count"] == lane_before
