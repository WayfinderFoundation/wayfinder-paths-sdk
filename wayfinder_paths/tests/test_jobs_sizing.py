from __future__ import annotations

from typing import Any

import pytest

from wayfinder_paths.jobs.execution import ExecutionSpec
from wayfinder_paths.jobs.execution.simulator import (
    PreparedExecutionDataset,
    run_execution_grid,
    simulate_execution,
)
from wayfinder_paths.jobs.strategies import build_snx_momentum
from wayfinder_paths.tests.test_jobs_strategies_scenarios import bars_from_closes


def _spec() -> ExecutionSpec:
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "1h"
    return spec


def _two_trade_closes() -> list[float]:
    """Two full short round trips: NewLow5 entry, SMA20-bounce exit (profit),
    rearm lift, second NewLow5 entry, second bounce exit."""
    base = [10.0 + (i % 3) * 0.01 for i in range(28)]
    first = [9.0, 8.9, 11.5]  # entry, fill/hold, bounce exit (close > SMA20)
    relift = [11.6]  # keeps rearm lifted (close > SMA20)
    second = [8.0, 7.9, 12.5]  # second entry cycle
    return base + first + relift + second


def _run(params: dict[str, Any]):
    return simulate_execution(
        build_snx_momentum,
        PreparedExecutionDataset.from_rows(
            bars_from_closes(_two_trade_closes(), symbol="SNX")
        ),
        _spec(),
        {"symbol": "SNX", "notional_usd": 2500.0, "initial_capital": 5000.0, **params},
    )


def _opens(result) -> list[dict[str, Any]]:
    return [row for row in result.trace["intents"] if row["action"] == "OPEN"]


def test_fixed_notional_default_is_unchanged() -> None:
    implicit = _run({})
    explicit = _run({"sizing": "fixed_notional", "leverage": 3.0})

    assert [o["size"] for o in _opens(implicit)] == [
        o["size"] for o in _opens(explicit)
    ], "leverage must be inert under fixed_notional sizing"
    assert implicit.trace["fills"] == explicit.trace["fills"]


def test_compound_sizing_scales_with_equity_and_leverage() -> None:
    one_x = _run({"sizing": "compound", "leverage": 1.0})
    two_x = _run({"sizing": "compound", "leverage": 2.0})

    opens_1x = _opens(one_x)
    assert len(opens_1x) == 2
    first_close = 9.0
    assert opens_1x[0]["size"] == round(5000.0 / first_close, 1)

    # The first round trip (short exited on the upward bounce) is a LOSS, so
    # the second entry must size off the SHRUNKEN equity — compounding
    # responds to realized results in both directions.
    first_notional = opens_1x[0]["size"] * first_close
    second_notional = opens_1x[1]["size"] * 8.0
    assert second_notional < first_notional
    equity_at_second_entry = {
        row["timestamp"]: row["equity"] for row in one_x.equity_curve
    }[opens_1x[1]["timestamp"]]
    assert second_notional == pytest.approx(equity_at_second_entry, rel=0.02)

    opens_2x = _opens(two_x)
    assert opens_2x[0]["size"] == round(2 * 5000.0 / first_close, 1)


def test_mark_to_market_equity_matches_simulator_curve() -> None:
    from wayfinder_paths.jobs.execution import mark_to_market_equity

    recorded: list[float] = []

    class Probe:
        def decide(self, ctx):
            recorded.append(mark_to_market_equity(ctx))
            latest = ctx.view.latest("SNX")
            if "SNX" not in ctx.ledger.positions and float(latest["close"]) < 9.5:
                return [
                    {
                        "action": "OPEN",
                        "venue": "hyperliquid",
                        "symbol": "SNX",
                        "side": "sell",
                        "size": 10,
                    }
                ]
            return []

    result = simulate_execution(
        lambda params: Probe(),
        PreparedExecutionDataset.from_rows(
            bars_from_closes(_two_trade_closes(), symbol="SNX")
        ),
        _spec(),
        {"initial_capital": 5000.0},
    )

    curve = {row["timestamp"]: row["equity"] for row in result.equity_curve}
    for run, equity in zip(result.trace["runs"], recorded, strict=True):
        assert equity == pytest.approx(curve[run["timestamp"]])


@pytest.mark.parametrize("leverage", [0.0, -1.0, 100.0])
def test_invalid_leverage_raises(leverage: float) -> None:
    with pytest.raises(ValueError, match="leverage"):
        _run({"sizing": "compound", "leverage": leverage})


def test_compound_entry_skipped_below_min_notional() -> None:
    result = _run(
        {"sizing": "compound", "leverage": 1.0, "initial_capital": 5.0}
    )

    assert _opens(result) == [], "equity below min_notional_usd must not enter"


def test_margin_stats_arithmetic() -> None:
    result = _run({"sizing": "compound", "leverage": 2.0})

    stats = result.stats
    assert stats["peak_notional_usd"] > 0
    assert stats["margin_used"] == pytest.approx(stats["peak_notional_usd"] / 2.0)
    assert stats["return_on_margin"] == pytest.approx(
        (result.equity_curve[-1]["equity"] - result.equity_curve[0]["equity"])
        / stats["margin_used"]
    )


def test_margin_stats_present_when_flat() -> None:
    flat_closes = [10.0 + (i % 3) * 0.01 for i in range(30)]
    result = simulate_execution(
        build_snx_momentum,
        PreparedExecutionDataset.from_rows(bars_from_closes(flat_closes, symbol="SNX")),
        _spec(),
        {"symbol": "SNX"},
    )

    stats = result.stats
    assert stats["peak_notional_usd"] == 0.0
    assert stats["margin_used"] == 0.0
    assert stats["return_on_margin"] is None


def test_grid_accepts_return_on_margin_and_leverage_dimension(tmp_path) -> None:
    script = tmp_path / "strategy.py"
    script.write_text(
        "from wayfinder_paths.jobs.strategies.snx_momentum import build_strategy\n",
        encoding="utf-8",
    )
    dataset = PreparedExecutionDataset.from_rows(
        bars_from_closes(_two_trade_closes(), symbol="SNX")
    )

    result = run_execution_grid(
        script,
        dataset,
        _spec(),
        {
            "symbol": ["SNX"],
            "initial_capital": [5000.0],
            "sizing": ["compound"],
            "leverage": [1.0, 2.0, 3.0],
        },
        rank_by="return_on_margin",
    )

    assert len(result.runs) == 3
    assert all(row["stats"]["return_on_margin"] is not None for row in result.runs)
    leverages = {row["params"]["leverage"] for row in result.runs}
    assert leverages == {1.0, 2.0, 3.0}
