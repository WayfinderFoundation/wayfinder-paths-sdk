"""Backtest telemetry (`profile`), the bounded compute-window default, and the
compact stdout summary — the diagnosability + token fixes for the create-a-
strategy loop."""

from __future__ import annotations

import datetime
import types

from wayfinder_paths.jobs.execution import ExecutionContext, OrderIntent
from wayfinder_paths.jobs.execution.job import summarize_backtest_payload
from wayfinder_paths.jobs.execution.simulator import (
    DEFAULT_WARMUP_BARS,
    PreparedExecutionDataset,
    simulate_execution,
)

SPEC = {
    "market_kind": "perp",
    "data_contract": {"bar_interval": "1h", "symbols": ["IMX"]},
}


def _dataset(n: int) -> PreparedExecutionDataset:
    rows = []
    price = 1.0
    for i in range(n):
        price = max(0.01, price * (1.0 + 0.02 * ((i % 11) - 5) / 100.0))
        secs = 1_700_000_000 + i * 3600
        ts = datetime.datetime.fromtimestamp(secs, datetime.UTC).isoformat()
        rows.append(
            {
                "timestamp": ts,
                "symbol": "IMX",
                "open": price,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "volume": 1000.0,
            }
        )
    return PreparedExecutionDataset.from_rows(rows)


def _build_strategy(params=None):
    """Recomputes over the whole handed frame every bar (the pathology) but
    only DECIDES on the last 20 closes — so decisions are identical for any
    compute window >= 21."""

    def decide(ctx: ExecutionContext):
        frame = ctx.view.symbol_frame("IMX")
        if len(frame) < 22:
            return []
        df = frame.copy()  # full-frame copy each bar — the churn source
        close = df["close"].astype(float)
        df["ema"] = close.ewm(span=9, adjust=False).mean()
        last = float(close.iloc[-1])
        low_n = float(close.iloc[-21:-1].min())
        pos = ctx.ledger.positions.get("IMX")
        if pos is not None:
            if last > low_n * 1.03:
                return [
                    OrderIntent(
                        action="close",
                        venue="hyperliquid",
                        symbol="IMX",
                        side="buy",
                        size=pos.size,
                        reduce_only=True,
                    )
                ]
            return []
        if last < low_n:
            return [
                OrderIntent(
                    action="open",
                    venue="hyperliquid",
                    symbol="IMX",
                    side="short",
                    notional=100.0,
                    reduce_only=False,
                    bracket={
                        "stop_loss": last * 1.07,
                        "take_profit": last * 0.9,
                        "policy": "conservative",
                    },
                )
            ]
        return []

    ns = types.SimpleNamespace()
    ns.decide = decide
    return ns


def test_profile_reports_window_and_timing() -> None:
    res = simulate_execution(_build_strategy, _dataset(300), SPEC, {})
    profile = res.profile
    assert profile["compute_window"] == DEFAULT_WARMUP_BARS
    assert profile["compute_window_source"] == "default"
    assert profile["bars_total"] == 300
    assert profile["bars_timed"] > 0
    assert set(profile["tick_ms"]) >= {"mean", "p50", "p95", "max", "last"}
    assert "tick_time_growing" in profile


def test_warmup_window_never_changes_stats() -> None:
    """The bounded-window default must produce byte-identical results to full
    history for a strategy that only looks at the last N bars — the safety
    property that lets us window by default."""
    ds_size = 400
    full = simulate_execution(
        _build_strategy, _dataset(ds_size), SPEC, {"full_history": True}
    )
    default = simulate_execution(_build_strategy, _dataset(ds_size), SPEC, {})
    tight = simulate_execution(
        _build_strategy, _dataset(ds_size), SPEC, {"warmup_bars": 30}
    )
    assert full.profile["compute_window"] == "full_history"
    assert tight.profile["compute_window_source"] == "warmup_bars"
    for key in ("net_return", "sharpe", "trade_count", "win_rate"):
        assert full.stats[key] == default.stats[key] == tight.stats[key]


def test_lookback_bars_still_windows_for_back_compat() -> None:
    res = simulate_execution(
        _build_strategy, _dataset(120), SPEC, {"lookback_bars": 40}
    )
    assert res.profile["compute_window"] == 40
    assert res.profile["compute_window_source"] == "lookback_bars"


def test_summarize_backtest_payload_drops_heavy_arrays() -> None:
    res = simulate_execution(_build_strategy, _dataset(120), SPEC, {})
    payload = {
        "type": "single",
        "result": res.to_dict(),
        "artifacts": {"latest": "/j/results/backtest/latest.json"},
        "validation": {"status": "passed"},
    }
    summary = summarize_backtest_payload(payload)
    # Decision-grade fields kept.
    assert summary["result"]["stats"] == res.stats
    assert summary["result"]["profile"] == res.profile
    assert summary["artifacts"]["latest"].endswith("latest.json")
    # Multi-MB per-bar arrays dropped.
    for heavy in ("equity_curve", "trades", "positions", "trace", "visualization"):
        assert heavy not in summary["result"]
    # And the summary is dramatically smaller.
    import json

    assert (
        len(json.dumps(summary, default=str))
        < len(json.dumps(payload, default=str)) // 10
    )
