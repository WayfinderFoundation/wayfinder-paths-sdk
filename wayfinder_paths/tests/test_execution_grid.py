from __future__ import annotations

from pathlib import Path

from wayfinder_paths.jobs.execution import ExecutionSpec
from wayfinder_paths.jobs.execution.simulator import (
    PreparedExecutionDataset,
    run_execution_grid,
)


def _write_strategy(path: Path) -> None:
    path.write_text(
        """
from wayfinder_paths.jobs.execution import OrderIntent


class Strategy:
    def __init__(self, params):
        self.params = params

    def decide(self, ctx):
        latest = ctx.view.latest("BTC")
        if not ctx.ledger.positions and latest["close"] > self.params["threshold"]:
            return [OrderIntent(action="OPEN", venue="hyperliquid", symbol="BTC", side="long", size=1)]
        return []


def build_strategy(params):
    return Strategy(params)
""".lstrip(),
        encoding="utf-8",
    )


def test_execution_grid_expands_params_and_ranks_valid_runs(tmp_path: Path) -> None:
    script = tmp_path / "strategy.py"
    _write_strategy(script)
    dataset = PreparedExecutionDataset.from_rows(
        [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "symbol": "BTC",
                "open": 100,
                "high": 105,
                "low": 99,
                "close": 104,
            },
            {
                "timestamp": "2026-01-01T00:05:00Z",
                "symbol": "BTC",
                "open": 104,
                "high": 107,
                "low": 103,
                "close": 106,
            },
        ]
    )

    result = run_execution_grid(
        script,
        dataset,
        ExecutionSpec(),
        {"threshold": [101, 200]},
    )

    assert len(result.runs) == 2
    assert len(result.ranked) == 2
    assert result.invalid == []
    assert {row["params"]["threshold"] for row in result.runs} == {101, 200}
