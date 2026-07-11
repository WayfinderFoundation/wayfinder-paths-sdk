"""Backtest telemetry (`profile`), the bounded compute-window default, and the
compact stdout summary — the diagnosability + token fixes for the create-a-
strategy loop."""

from __future__ import annotations

import datetime
import json
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
    assert (
        len(json.dumps(summary, default=str))
        < len(json.dumps(payload, default=str)) // 10
    )


def test_grid_summary_flags_in_sample_only() -> None:
    """A grid with no walk-forward is in-sample only — the summary must say so
    (and strip the heavy per-run arrays), so the agent validates OOS instead of
    trusting a curve-fit ranking."""
    grid_payload = {
        "type": "grid",
        "result": {
            "grid_id": "g1",
            "rank_by": "net_return",
            "runs": [{}, {}],
            "invalid": [],
            "ranked": [
                {"params": {"x": 1}, "net_return": 0.4, "equity_curve": [1, 2, 3]}
            ],
        },
    }
    summary = summarize_backtest_payload(grid_payload)
    assert "note" in summary and "out-of-sample" in summary["note"]
    assert "equity_curve" not in summary["result"]["ranked"][0]  # heavy key stripped
    # Once validated out-of-sample, the overfit note goes away.
    validated = summarize_backtest_payload(
        {**grid_payload, "walk_forward": {"summary": {"decay_ratio": 0.9}}}
    )
    assert "note" not in validated


def test_effective_workers_never_oversubscribes(monkeypatch) -> None:
    from wayfinder_paths.jobs.execution import simulator as sim

    monkeypatch.setattr(sim, "available_cpu_count", lambda: 2)
    assert sim._effective_workers(8, "process") == 2  # clamped to cores
    assert sim._effective_workers(1, "process") == 1
    assert sim._effective_workers(0, "process") == 2  # 0 => all cores
    assert sim._effective_workers(8, "serial") == 1  # serial is always 1


def test_quick_bars_truncates_to_last_n() -> None:
    """--quick backtests only the last N bars — cheap for parameter sweeps."""
    full = simulate_execution(_build_strategy, _dataset(300), SPEC, {})
    ds = _dataset(300)
    # Emulate the job-level `quick_bars` truncation (last 60 bars).
    from wayfinder_paths.jobs.execution.simulator import PreparedExecutionDataset

    ts = ds.bars.timestamps
    quick = PreparedExecutionDataset(ds.bars.window(len(ts) - 1, 60), {})
    quick_res = simulate_execution(_build_strategy, quick, SPEC, {})
    assert quick_res.profile["bars_total"] == 60
    assert full.profile["bars_total"] == 300


def test_diagnose_backtest_agrees_with_stats(tmp_path) -> None:
    """The diagnose headline is the run's own stats verbatim, and the buckets
    come from the same realized PnL — so it never disagrees with the backtest
    (unlike hand-rolled recomputation)."""
    from wayfinder_paths.jobs.backtest_artifacts import diagnose_backtest
    from wayfinder_paths.jobs.store import JobStore

    store = JobStore(repo_root=tmp_path)
    job_id = "diag-job"
    bt_dir = store.job_dir(job_id) / "results" / "backtest"
    bt_dir.mkdir(parents=True, exist_ok=True)
    latest = {
        "run_id": "r1",
        "stats": {
            "net_return": 0.05,
            "trade_count": 3,
            "win_rate": 0.667,
            "sharpe": 1.2,
        },
        "trades": [
            {
                "timestamp": "2026-01-01T08:00:00+00:00",
                "side": "buy",
                "reduce_only": True,
                "realized_pnl_delta": 2.0,
                "raw": {"metadata": {"exit_reason": "take_profit"}},
            },
            {
                "timestamp": "2026-01-01T09:00:00+00:00",
                "side": "buy",
                "reduce_only": True,
                "realized_pnl_delta": 1.0,
                "raw": {"metadata": {"exit_reason": "take_profit"}},
            },
            {
                "timestamp": "2026-01-01T10:00:00+00:00",
                "side": "buy",
                "reduce_only": True,
                "realized_pnl_delta": -1.5,
                "raw": {"metadata": {"exit_reason": "stop_loss"}},
            },
            # entry fill (no realized pnl) — excluded from trade buckets
            {
                "timestamp": "2026-01-01T07:00:00+00:00",
                "side": "sell",
                "realized_pnl_delta": 0.0,
            },
        ],
    }
    (bt_dir / "latest.json").write_text(json.dumps(latest))

    diag = diagnose_backtest(job_id, store=store)
    assert diag["available"] is True
    # Headline is the stats verbatim — no recomputation, no drift.
    assert diag["headline"]["net_return"] == 0.05
    assert diag["headline"]["win_rate"] == 0.667
    # Only the 3 closing fills are analyzed.
    assert diag["closed_trades_analyzed"] == 3
    tp = diag["by_exit_reason"]["take_profit"]
    assert tp["trades"] == 2 and tp["pnl"] == 3.0 and tp["win_rate"] == 1.0
    sl = diag["by_exit_reason"]["stop_loss"]
    assert sl["trades"] == 1 and sl["pnl"] == -1.5 and sl["win_rate"] == 0.0
    assert diag["worst_trades"][0]["pnl"] == -1.5
    assert diag["best_trades"][0]["pnl"] == 2.0
