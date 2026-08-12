from __future__ import annotations

from wayfinder_paths.jobs.execution import ExecutionSpec, OrderIntent
from wayfinder_paths.jobs.execution.simulator import (
    PreparedExecutionDataset,
    simulate_execution,
)


def _bar(ts: str, symbol: str, px: float) -> dict:
    return {
        "timestamp": ts,
        "symbol": symbol,
        "open": px,
        "high": px,
        "low": px,
        "close": px,
        "volume": 1.0,
    }


class _OpenBBBOnce:
    def __init__(self, params):
        self.params = params

    def decide(self, ctx):
        if not ctx.ledger.positions:
            return [
                OrderIntent(
                    action="OPEN", venue="hyperliquid", symbol="BBB",
                    side="long", size=1,
                )
            ]
        return []


def test_absent_symbol_marks_at_last_known_close() -> None:
    """A held position must stay marked at its symbol's LAST close on bars where
    that symbol does not print — not reset to avg_price. Regression for the
    multi-asset equity oscillation: with unaligned bars, marking absent symbols
    at avg_price makes equity swing bar-to-bar and corrupts drawdown/sharpe."""
    # Small position vs large capital so the annualized return stays sane; the
    # point is only that BBB's price differs from entry on the absent bars.
    rows = [
        _bar("2026-01-01T00:00:00Z", "AAA", 100), _bar("2026-01-01T00:00:00Z", "BBB", 100),
        _bar("2026-01-01T00:05:00Z", "AAA", 100), _bar("2026-01-01T00:05:00Z", "BBB", 100),
        _bar("2026-01-01T00:10:00Z", "AAA", 100),                                   # BBB absent
        _bar("2026-01-01T00:15:00Z", "AAA", 100), _bar("2026-01-01T00:15:00Z", "BBB", 130),  # BBB +30%
        _bar("2026-01-01T00:20:00Z", "AAA", 100),                                   # BBB absent -> mark @130
        _bar("2026-01-01T00:25:00Z", "AAA", 100),                                   # BBB absent -> mark @130
    ]
    dataset = PreparedExecutionDataset.from_rows(rows)
    result = simulate_execution(
        lambda params: _OpenBBBOnce(params),
        dataset,
        ExecutionSpec(),
        {"initial_capital": 100_000.0},
    )
    def at(hhmm: str) -> dict:
        return next(r for r in result.equity_curve if f"T{hhmm}:00" in r["timestamp"])

    # BBB entered ~100; after it rises to 130 the ~+30 unrealized must persist on
    # the following bars where BBB is absent (buggy path marks it flat -> ~0).
    assert at("00:20")["unrealized_pnl"] > 15
    assert at("00:25")["unrealized_pnl"] > 15

    # Equity must not oscillate: once BBB is at 130 it stays there while absent.
    assert at("00:20")["equity"] == at("00:25")["equity"]
