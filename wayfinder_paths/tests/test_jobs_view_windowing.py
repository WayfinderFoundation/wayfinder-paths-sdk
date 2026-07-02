"""CompletedBarsView fast-path semantics: shared caches must never leak
future data into truncated views, and bounded windows must match live-driver
lookback semantics exactly."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from wayfinder_paths.jobs.execution import ExecutionSpec
from wayfinder_paths.jobs.execution.primitives import CompletedBarsView
from wayfinder_paths.jobs.execution.simulator import (
    PreparedExecutionDataset,
    simulate_execution,
)
from wayfinder_paths.tests.test_jobs_strategies_scenarios import bars_from_closes


def _view(count: int = 10, symbols: tuple[str, ...] = ("SNX",)) -> CompletedBarsView:
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        rows.extend(bars_from_closes([10.0 + i for i in range(count)], symbol=symbol))
    return CompletedBarsView.from_rows(rows)


def test_through_child_sees_only_prefix() -> None:
    parent = _view(10)
    child = parent.through(4)
    assert child.timestamps == parent.timestamps[:5]
    assert len(child) == 5
    assert child.latest("SNX")["close"] == 14.0


def test_truncated_view_rejects_future_row_at() -> None:
    """The shared row index must not leak lookahead data into children."""
    parent = _view(10)
    child = parent.through(4)
    future_ts = parent.timestamps[7]
    parent.row_at(future_ts, symbol="SNX")  # parent sees it
    with pytest.raises(ValueError, match="No bar at"):
        child.row_at(future_ts, symbol="SNX")


def test_through_by_timestamp_matches_int_path() -> None:
    parent = _view(10)
    ts = parent.timestamps[6]
    by_ts = parent.through(ts)
    by_int = parent.through(6)
    assert by_ts.timestamps == by_int.timestamps
    # Cutoff between bars truncates to the last completed bar.
    between = parent.through(ts + pd.Timedelta(minutes=30))
    assert between.timestamps == by_int.timestamps
    # Cutoff before the first bar -> empty view.
    early = parent.through(parent.timestamps[0] - pd.Timedelta(hours=1))
    assert len(early) == 0


def test_window_bounds_visible_history() -> None:
    parent = _view(10)
    window = parent.window(7, 3)
    assert window.timestamps == parent.timestamps[5:8]
    assert window.latest("SNX")["close"] == 17.0
    # Bars before the window start are invisible, exactly like a live fetch.
    with pytest.raises(ValueError, match="No bar at"):
        window.row_at(parent.timestamps[2], symbol="SNX")
    # Window wider than history degrades to the full prefix.
    assert parent.window(4, 100).timestamps == parent.timestamps[:5]


def test_row_at_bars_are_immutable_and_junk_rejected() -> None:
    import dataclasses

    parent = _view(4)
    ts = parent.timestamps[1]
    first = parent.row_at(ts, symbol="SNX")
    # Shared index instances are safe because MarketBar is frozen.
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.close = -1.0  # type: ignore[misc]
    assert parent.row_at(ts, symbol="SNX").close == 11.0
    with pytest.raises(ValueError, match="No bar at"):
        parent.row_at(pd.Timestamp("2026-01-01T01:00:00"))  # naive tz
    with pytest.raises(ValueError, match="No bar at"):
        parent.row_at(ts, symbol="MISSING")


def test_multi_symbol_child_row_lookups() -> None:
    parent = _view(6, symbols=("AAA", "BBB"))
    child = parent.through(3)
    ts = child.timestamps[-1]
    assert child.row_at(ts, symbol="AAA").symbol == "AAA"
    assert child.row_at(ts, symbol="BBB").symbol == "BBB"
    assert child.row_at(ts).symbol == "AAA"  # first row at ts (sorted)


def test_symbol_frame_matches_filter() -> None:
    parent = _view(5, symbols=("AAA", "BBB"))
    frame = parent.symbol_frame("BBB")
    assert set(frame["symbol"]) == {"BBB"}
    assert len(frame) == 5


class WindowProbe:
    """Records how many timestamps decide() can see each tick."""

    def __init__(self) -> None:
        self.seen: list[int] = []

    def decide(self, ctx: Any) -> list[dict[str, Any]]:
        self.seen.append(len(ctx.view.timestamps))
        return []


def _spec() -> ExecutionSpec:
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "1h"
    return spec


def test_simulator_honors_lookback_bars() -> None:
    dataset = PreparedExecutionDataset.from_rows(
        bars_from_closes([10.0 + i * 0.1 for i in range(30)], symbol="SNX")
    )
    bounded = WindowProbe()
    simulate_execution(lambda params: bounded, dataset, _spec(), {"lookback_bars": 5})
    assert max(bounded.seen) == 5
    assert bounded.seen[:5] == [1, 2, 3, 4, 5]

    unbounded = WindowProbe()
    simulate_execution(lambda params: unbounded, dataset, _spec(), {})
    assert unbounded.seen == list(range(1, 31))


def test_lookback_wider_than_history_is_bit_identical_to_full() -> None:
    closes = [10.0 + (i % 3) * 0.01 for i in range(28)] + [9.0, 8.9, 11.5]
    from wayfinder_paths.jobs.strategies import build_snx_momentum

    def run(params: dict[str, Any]):
        return simulate_execution(
            build_snx_momentum,
            PreparedExecutionDataset.from_rows(bars_from_closes(closes, symbol="SNX")),
            _spec(),
            {"symbol": "SNX", "notional_usd": 500.0, **params},
        )

    full = run({})
    windowed = run({"lookback_bars": 10_000})
    assert full.stats == windowed.stats
    assert full.trades == windowed.trades
    assert full.equity_curve == windowed.equity_curve


def test_validation_flags_missing_lookback() -> None:
    from wayfinder_paths.jobs.execution.validation import _timing_checks

    def check(job_data: dict[str, Any]) -> dict[str, Any]:
        rows = _timing_checks(job_data, _spec())
        return next(row for row in rows if row["name"] == "lookback_bars_declared")

    missing = check({"execution_contract": "jobs_v1", "execution_params": {}})
    assert missing["passed"] is False
    assert missing["blocking"] is False  # warn, never block

    declared = check(
        {"execution_contract": "jobs_v1", "execution_params": {"lookback_bars": 200}}
    )
    assert declared["passed"] is True

    legacy = check({"execution_contract": "legacy", "execution_params": {}})
    assert legacy["passed"] is True  # legacy jobs exempt
