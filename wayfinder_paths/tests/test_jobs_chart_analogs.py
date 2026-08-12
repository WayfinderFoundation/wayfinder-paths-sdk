"""Agent chart lenses: indicator engine, text chart op, analog search, and
regime-tagged forensics."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd
import pytest

from wayfinder_paths.jobs.chart import analogs_job, chart_job
from wayfinder_paths.jobs.execution.primitives import ExecutionSpec
from wayfinder_paths.jobs.indicators import (
    compute_indicator,
    compute_indicators,
    regime_snapshot,
)
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.trade_forensics import forensics_for_closed_trades


def _wavy_frame(count: int = 300, *, symbol: str = "LIT") -> pd.DataFrame:
    rows = []
    base = pd.Timestamp("2026-07-01T00:00:00Z")
    for i in range(count):
        price = 100 + 5 * math.sin(i / 9) + i * 0.01
        rows.append(
            {
                "timestamp": (base + pd.Timedelta(minutes=5 * i)).isoformat(),
                "symbol": symbol,
                "open": price,
                "high": price + 0.4,
                "low": price - 0.4,
                "close": price + 0.1,
                "volume": 10.0 + (i % 7),
            }
        )
    return pd.DataFrame(rows)


def test_indicator_engine_specs_and_errors() -> None:
    frame = _wavy_frame()
    cols = compute_indicators(
        frame, ["ema:9", "rsi:14", "bb:20:2", "atr:14", "macd:12:26:9", "don:20"]
    )
    assert set(cols) == {
        "ema9",
        "rsi14",
        "bb20_pctb",
        "bb20_bw",
        "atr14",
        "macd12_26",
        "macds9",
        "don20_pos",
    }
    rsi = cols["rsi14"].dropna()
    assert rsi.between(0, 100).all() and rsi.std() > 1  # actually oscillates
    assert cols["don20_pos"].dropna().between(-0.01, 1.01).all()
    expected_ema = frame["close"].astype(float).ewm(span=9, adjust=False).mean()
    pd.testing.assert_series_equal(cols["ema9"], expected_ema, check_names=False)

    with pytest.raises(ValueError, match="unknown indicator"):
        compute_indicator(frame, "wizardry:9")
    with pytest.raises(ValueError, match="vwap takes no parameters"):
        compute_indicator(frame, "vwap:3")
    with pytest.raises(ValueError, match="too many indicators"):
        compute_indicators(frame, [f"ema:{n}" for n in range(2, 12)])


def test_regime_snapshot_tags() -> None:
    frame = _wavy_frame(200)
    at = pd.Timestamp(frame["timestamp"].iloc[-1])
    snap = regime_snapshot(frame, at)
    assert snap["trend"] in {"up", "down"}
    assert 0 <= snap["vol_pctile"] <= 100
    assert snap["session"] in {"asia", "europe", "us", "late"}
    assert regime_snapshot(frame.head(10), at) == {"insufficient_history": True}


def _make_chart_job(tmp_path: Path, frame: pd.DataFrame) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "lens-demo",
        script=".wayfinder/jobs/lens-demo/workspace/src/strategy.py",
        interval_seconds=300,
    )
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "5m"
    job.execution_spec = spec.to_dict()
    store.save(job)
    root = store.job_dir(job.id)
    bars_path = root / "results" / "backtest" / "input_bars.json"
    bars_path.parent.mkdir(parents=True, exist_ok=True)
    bars_path.write_text(json.dumps(frame.to_dict("records")), encoding="utf-8")
    return store, job.id


def test_chart_job_renders_window_with_marks(tmp_path: Path) -> None:
    frame = _wavy_frame(280)
    store, job_id = _make_chart_job(tmp_path, frame)
    root = store.job_dir(job_id)
    forward = root / "results" / "forward"
    forward.mkdir(parents=True, exist_ok=True)
    entry_ts = str(frame["timestamp"].iloc[250])
    exit_ts = str(frame["timestamp"].iloc[260])
    (forward / "fills.jsonl").write_text(
        json.dumps(
            {
                "symbol": "LIT",
                "side": "sell",
                "reduce_only": False,
                "timestamp": entry_ts,
                "avg_price": 100.0,
                "raw": {"intent_metadata": {"entry_reason": "fade"}},
            }
        )
        + "\n"
        + json.dumps(
            {
                "symbol": "LIT",
                "side": "buy",
                "reduce_only": True,
                "timestamp": exit_ts,
                "avg_price": 99.0,
                "raw": {"intent_metadata": {"exit_reason": "time_exit"}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (forward / "trades.jsonl").write_text(
        json.dumps(
            {
                "symbol": "LIT",
                "side": "buy",
                "price": 99.0,
                "net_pnl": 0.25,
                "closed_at": exit_ts,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = chart_job(
        job_id,
        indicators=["ema:9", "rsi:14"],
        bars=60,
        around_trade="last",
        store=store,
    )
    assert result["header"]["symbol"] == "LIT"
    assert result["header"]["window_note"] is not None
    assert result["columns"][:6] == ["ts", "open", "high", "low", "close", "volume"]
    assert "ema9" in result["columns"] and "rsi14" in result["columns"]
    marks = {row[0]: row[-1] for row in result["rows"] if row[-1]}
    assert any("ENTRY:sell:fade" in m for m in marks.values())
    assert any("EXIT:time_exit" in m for m in marks.values())
    assert len(result["rows"]) <= 60
    assert result["header"]["regime_at_end"].get("trend") in {"up", "down"}


def test_chart_job_resamples_timeframe(tmp_path: Path) -> None:
    frame = _wavy_frame(288)
    store, job_id = _make_chart_job(tmp_path, frame)
    result = chart_job(job_id, timeframe="30m", bars=20, store=store)
    stamps = [pd.Timestamp(row[0]) for row in result["rows"]]
    assert all(
        (b - a).total_seconds() == 1800
        for a, b in zip(stamps, stamps[1:], strict=False)
    )
    with pytest.raises(ValueError, match="unknown timeframe"):
        chart_job(job_id, timeframe="7m", store=store)


def test_analogs_job_finds_planted_motif(tmp_path: Path) -> None:
    # Sine-wave series: every full period is an analog of the current window,
    # so the search must find multiple non-overlapping matches.
    frame = _wavy_frame(400)
    store, job_id = _make_chart_job(tmp_path, frame)
    result = analogs_job(job_id, window=20, top=8, horizon=6, store=store)
    assert result["summary"]["matches"] >= 3
    assert result["summary"]["mean_bps"] is not None
    starts = [pd.Timestamp(m["start_ts"]) for m in result["matches"]]
    for a, b in zip(sorted(starts), sorted(starts)[1:], strict=False):
        assert (b - a).total_seconds() >= 10 * 300  # non-overlap: >= window/2
    assert "hypothesis fuel" in result["read"]

    with pytest.raises(ValueError, match="need at least"):
        analogs_job(job_id, window=200, horizon=50, store=store)


def test_forensics_rows_carry_regime_tags() -> None:
    frame = _wavy_frame(120)
    entry_ts = str(frame["timestamp"].iloc[90])
    exit_ts = str(frame["timestamp"].iloc[100])
    fills = [
        {
            "symbol": "LIT",
            "side": "sell",
            "reduce_only": False,
            "timestamp": entry_ts,
            "avg_price": 100.0,
        },
    ]
    trades = [
        {"symbol": "LIT", "side": "buy", "price": 99.5, "closed_at": exit_ts},
    ]
    rows = forensics_for_closed_trades({"LIT": frame}, trades, fills, post_bars=(4,))
    assert len(rows) == 1
    regime = rows[0]["regime_at_entry"]
    assert regime.get("trend") in {"up", "down"}
    assert regime.get("session") in {"asia", "europe", "us", "late"}


def test_chart_around_trade_beyond_dataset_end_fails_loud(tmp_path: Path) -> None:
    frame = _wavy_frame(280)
    store, job_id = _make_chart_job(tmp_path, frame)
    forward = store.job_dir(job_id) / "results" / "forward"
    forward.mkdir(parents=True, exist_ok=True)
    beyond = (
        pd.Timestamp(frame["timestamp"].iloc[-1]) + pd.Timedelta(hours=6)
    ).isoformat()
    (forward / "trades.jsonl").write_text(
        json.dumps({"symbol": "LIT", "side": "buy", "price": 99.0, "closed_at": beyond})
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="BEYOND the dataset end"):
        chart_job(job_id, around_trade="last", store=store)


def test_chart_header_reports_dataset_end(tmp_path: Path) -> None:
    frame = _wavy_frame(120)
    store, job_id = _make_chart_job(tmp_path, frame)
    result = chart_job(job_id, bars=20, store=store)
    assert pd.Timestamp(result["header"]["dataset_end"]) == pd.Timestamp(
        frame["timestamp"].iloc[-1]
    )
