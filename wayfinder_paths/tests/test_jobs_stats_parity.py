"""Stats parity with the legacy framework (core/backtesting/stats.py).

Each new `_stats` key is checked against an embedded copy of the legacy
formula (the indicator-parity-test pattern), so drift from the legacy math
fails loudly. A golden-stability test pins the 12 preexisting keys so the
additive change cannot alter existing outputs.

Deliberate divergences from legacy (documented in BACKTESTING.md):
- `sharpe` keeps ddof=1 (preexisting); sortino/volatility_ann port legacy ddof=0
- sqn/kelly/best/worst run on discrete per-trade PnLs, not per-period returns
- `total_funding` is credited-positive (jobs ledger sign), legacy negative-is-income
- durations are float seconds (`_s` suffix), not Timedelta
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from wayfinder_paths.jobs.execution import ExecutionSpec
from wayfinder_paths.jobs.execution.simulator import (
    GRID_RANK_KEYS,
    PreparedExecutionDataset,
    _stats,
    run_execution_grid,
    simulate_execution,
)
from wayfinder_paths.jobs.strategies import build_snx_momentum
from wayfinder_paths.tests.test_jobs_strategies_scenarios import bars_from_closes

HOUR = 3600
PPY = (365 * 24 * 3600) / HOUR  # hourly bars


def _ts(i: int) -> str:
    return f"2026-01-01T{i:02d}:00:00+00:00"


def _equity_curve(values: list[float]) -> list[dict[str, Any]]:
    return [
        {"timestamp": _ts(i), "equity": v, "value": v} for i, v in enumerate(values)
    ]


def _trade(
    i: int,
    *,
    symbol: str = "SNX",
    size: float,
    price: float,
    fee: float = 0.0,
    reduce_only: bool = False,
    pnl: float | None = None,
) -> dict[str, Any]:
    return {
        "timestamp": _ts(i),
        "symbol": symbol,
        "side": "sell" if reduce_only else "buy",
        "filled_size": size,
        "avg_price": price,
        "fee": fee,
        "reduce_only": reduce_only,
        "realized_pnl_delta": pnl if pnl is not None else 0.0,
    }


# One long round trip with a partial close: entry t1, partial exit t3 (+4),
# final exit t5 (-2). Equity path includes a drawdown from t1 to t3.
EQUITY = [100.0, 110.0, 99.0, 104.5, 110.0, 121.0]
TRADES = [
    _trade(1, size=10, price=10.0, fee=0.5),
    _trade(3, size=4, price=11.0, fee=0.2, reduce_only=True, pnl=4.0),
    _trade(5, size=6, price=12.0, fee=0.3, reduce_only=True, pnl=-2.0),
]
GUARD_EVENTS = [
    {"kind": "funding_applied", "symbol": "SNX", "amount": -1.5, "timestamp": _ts(2)},
    {"kind": "funding_applied", "symbol": "SNX", "amount": 0.5, "timestamp": _ts(4)},
    {"kind": "purity_warning", "reason": "socket", "timestamp": _ts(4)},
]
PRICE_SERIES = {
    "SNX": [{"timestamp": _ts(i), "close": c} for i, c in enumerate([10.0, 11, 13.0])],
    "IMX": [{"timestamp": _ts(i), "close": c} for i, c in enumerate([5.0, 4.8, 4.5])],
}
POSITIONS = [
    {"timestamp": _ts(1), "positions": {"SNX": {"size": 10, "avg_price": 10.0}}},
    {"timestamp": _ts(3), "positions": {"SNX": {"size": 6, "avg_price": 10.0}}},
    {"timestamp": _ts(5), "positions": {}},
]


def _full_stats() -> dict[str, Any]:
    return _stats(
        _equity_curve(EQUITY),
        TRADES,
        POSITIONS,
        HOUR,
        params={},
        guard_events=GUARD_EVENTS,
        price_series=PRICE_SERIES,
    )


def _returns() -> np.ndarray:
    values = np.asarray(EQUITY)
    return values[1:] / values[:-1] - 1.0


# ---------------------------------------------------------------------------
# Embedded legacy-formula parity (core/backtesting/stats.py)
# ---------------------------------------------------------------------------


def test_sortino_matches_legacy_formula() -> None:
    # Needs >=2 distinct negative returns so downside std (ddof=0) is nonzero.
    values = np.asarray([100.0, 110.0, 99.0, 104.5, 94.0, 121.0])
    returns = values[1:] / values[:-1] - 1.0
    downside = returns[returns < 0]
    expected = float(np.sqrt(PPY) * returns.mean() / downside.std(ddof=0))
    stats = _stats(_equity_curve(list(values)), [], None, HOUR, params={})
    assert stats["sortino"] == pytest.approx(expected)


def test_sortino_single_downside_reports_legacy_zero() -> None:
    # Exactly one negative return: downside std (ddof=0) == 0 → legacy 0.0.
    assert _full_stats()["sortino"] == 0.0


def test_volatility_ann_matches_legacy_formula() -> None:
    returns = _returns()
    expected = float(returns.std(ddof=0) * np.sqrt(PPY))
    assert _full_stats()["volatility_ann"] == pytest.approx(expected)


def test_cagr_and_return_ann_match_legacy_formula() -> None:
    returns = _returns()
    years = len(returns) / PPY
    expected = float((EQUITY[-1] / EQUITY[0]) ** (1 / years) - 1)
    stats = _full_stats()
    assert stats["cagr"] == pytest.approx(expected)
    assert stats["return_ann"] == pytest.approx(expected)


def test_calmar_matches_legacy_formula() -> None:
    stats = _full_stats()
    expected = abs(stats["cagr"] / stats["max_drawdown_pct"])
    assert stats["calmar"] == pytest.approx(expected)


def test_sqn_and_kelly_on_exit_pnls() -> None:
    pnls = np.asarray([4.0, -2.0])
    expected_sqn = float(np.sqrt(len(pnls)) * pnls.mean() / pnls.std(ddof=1))
    win_rate = 0.5
    win_loss_ratio = 4.0 / 2.0
    expected_kelly = win_rate - (1 - win_rate) / win_loss_ratio
    stats = _full_stats()
    assert stats["sqn"] == pytest.approx(expected_sqn)
    assert stats["kelly_criterion"] == pytest.approx(expected_kelly)
    assert stats["best_trade_pnl"] == 4.0
    assert stats["worst_trade_pnl"] == -2.0


def test_avg_drawdown_matches_legacy_formula() -> None:
    values = np.asarray(EQUITY)
    dd = values / np.maximum.accumulate(values) - 1.0
    expected = float(dd[dd < 0].mean())
    assert _full_stats()["avg_drawdown"] == pytest.approx(expected)


def test_drawdown_durations() -> None:
    # Peak at t1 (110); dd<0 at t2, t3; recovered exactly at t4 (110).
    # Legacy run-detection: run = [t2, recovery point t4) => 2h.
    stats = _full_stats()
    assert stats["max_drawdown_duration_s"] == pytest.approx(2 * HOUR)
    assert stats["avg_drawdown_duration_s"] == pytest.approx(2 * HOUR)


def test_unrecovered_drawdown_runs_to_last_bar() -> None:
    stats = _stats(
        _equity_curve([100.0, 110.0, 99.0, 95.0]), [], None, HOUR, params={}
    )
    # dd<0 from t2 through the end: last_ts(t3) - start(t2) = 1h
    assert stats["max_drawdown_duration_s"] == pytest.approx(HOUR)


def test_trade_durations_with_partial_close() -> None:
    stats = _full_stats()
    # entry t1 -> partial close t3 (2h), final close t5 (4h)
    assert stats["max_trade_duration_s"] == pytest.approx(4 * HOUR)
    assert stats["avg_trade_duration_s"] == pytest.approx(3 * HOUR)


def test_buy_hold_return_equal_weight_multi_symbol() -> None:
    expected = ((13.0 / 10.0 - 1) + (4.5 / 5.0 - 1)) / 2
    assert _full_stats()["buy_hold_return"] == pytest.approx(expected)


def test_total_fees_funding_and_turnover() -> None:
    stats = _full_stats()
    assert stats["total_fees"] == pytest.approx(1.0)
    # Only funding_applied events count; purity_warning ignored.
    assert stats["total_funding"] == pytest.approx(-1.0)
    assert stats["total_turnover_usd"] == pytest.approx(10 * 10.0 + 4 * 11.0 + 6 * 12.0)
    # Per-bar notional / equity, averaged over all 6 bars (0 when no trades).
    expected_avg = (100.0 / 110.0 + 44.0 / 104.5 + 72.0 / 121.0) / 6
    assert stats["avg_turnover"] == pytest.approx(expected_avg)


# ---------------------------------------------------------------------------
# Fallbacks and schema stability
# ---------------------------------------------------------------------------


def test_no_downside_returns_reports_legacy_zero_sortino() -> None:
    stats = _stats(_equity_curve([100.0, 101.0, 102.0]), [], None, HOUR, params={})
    assert stats["sortino"] == 0.0  # legacy convention, not None/inf


def test_new_keys_present_in_empty_branch() -> None:
    stats = _stats([], [], None, HOUR, params={})
    for key in (
        "sortino",
        "volatility_ann",
        "cagr",
        "return_ann",
        "calmar",
        "sqn",
        "kelly_criterion",
        "best_trade_pnl",
        "worst_trade_pnl",
        "max_trade_duration_s",
        "avg_trade_duration_s",
        "max_drawdown_duration_s",
        "avg_drawdown_duration_s",
        "avg_drawdown",
        "buy_hold_return",
        "total_fees",
        "total_funding",
        "avg_turnover",
        "total_turnover_usd",
    ):
        assert key in stats, f"missing {key} in empty-branch stats"
    assert stats["sortino"] is None
    assert stats["buy_hold_return"] is None
    assert stats["total_fees"] == 0.0


def test_golden_stability_of_preexisting_keys() -> None:
    """The 12 preexisting keys must be bit-identical to their pre-change
    values on this fixture (hand-derived; sharpe via the ddof=1 formula)."""
    stats = _full_stats()
    returns = _returns()
    mean = returns.mean()
    sharpe = float((mean / returns.std(ddof=1)) * np.sqrt(PPY))
    assert stats["net_return"] == pytest.approx(121.0 / 100.0 - 1.0)
    assert stats["ending_equity"] == 121.0
    assert stats["trade_count"] == 3
    assert stats["sharpe"] == pytest.approx(sharpe)
    assert stats["max_drawdown_pct"] == pytest.approx(99.0 / 110.0 - 1.0)
    assert stats["win_rate"] == 0.5
    assert stats["profit_factor"] == pytest.approx(4.0 / 2.0)
    assert stats["avg_trade_pnl"] == pytest.approx(1.0)
    assert stats["exposure_pct"] == pytest.approx(2 / 3)
    assert stats["peak_notional_usd"] == pytest.approx(100.0)
    assert stats["margin_used"] == pytest.approx(100.0)
    assert stats["return_on_margin"] == pytest.approx(21.0 / 100.0)


# ---------------------------------------------------------------------------
# End-to-end plumbing
# ---------------------------------------------------------------------------


def _two_trade_closes() -> list[float]:
    base = [10.0 + (i % 3) * 0.01 for i in range(28)]
    return base + [9.0, 8.9, 11.5, 11.6, 8.0, 7.9, 12.5]


def _spec() -> ExecutionSpec:
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "1h"
    return spec


def test_simulate_execution_populates_new_stats() -> None:
    closes = _two_trade_closes()
    result = simulate_execution(
        build_snx_momentum,
        PreparedExecutionDataset.from_rows(bars_from_closes(closes, symbol="SNX")),
        _spec(),
        {"symbol": "SNX", "notional_usd": 2500.0, "initial_capital": 5000.0},
    )
    stats = result.stats
    assert stats["buy_hold_return"] == pytest.approx(closes[-1] / closes[0] - 1.0)
    assert stats["total_fees"] == 0.0  # fee_bps defaults to 0
    assert stats["total_funding"] == 0.0
    assert stats["total_turnover_usd"] > 0
    assert stats["max_trade_duration_s"] is not None
    assert stats["sortino"] is not None
    assert stats["volatility_ann"] is not None


def test_grid_ranks_by_sortino(tmp_path) -> None:
    assert {"sortino", "calmar", "cagr"} <= GRID_RANK_KEYS
    script = tmp_path / "strategy.py"
    script.write_text(
        "from wayfinder_paths.jobs.strategies.snx_momentum import build_strategy\n",
        encoding="utf-8",
    )
    result = run_execution_grid(
        script,
        PreparedExecutionDataset.from_rows(
            bars_from_closes(_two_trade_closes(), symbol="SNX")
        ),
        _spec(),
        {"symbol": ["SNX"], "notional_usd": [1000.0, 2500.0]},
        rank_by="sortino",
    )
    assert len(result.runs) == 2
    assert result.ranked, "grid must rank by sortino without error"
