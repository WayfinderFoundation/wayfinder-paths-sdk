"""Portfolio regime/incumbent health and governed response policy."""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import pandas as pd
import pytest

from wayfinder_paths.jobs.governance import commit_epoch, governance_dir
from wayfinder_paths.jobs.halt import clear_halt, read_halt
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.regime_health import (
    active_regime_leverage_cap,
    regime_health_job,
)
from wayfinder_paths.jobs.regime_market import summarize_market_state
from wayfinder_paths.jobs.store import JobStore


def _market_bars() -> pd.DataFrame:
    """Sixty days of 6h bars, with a violent correlated final week."""
    start = pd.Timestamp("2026-06-20T00:00:00Z")
    prices = {"BTC": 100.0, "ETH": 80.0}
    rows: list[dict[str, object]] = []
    count = 60 * 4
    for index in range(count):
        timestamp = start + pd.Timedelta(hours=6 * index)
        shifted = index >= count - 28
        common = 0.018 if index % 2 == 0 else -0.016
        for symbol in ("BTC", "ETH"):
            if shifted:
                change = common
                volume = 5.0
            else:
                wave = 0.0015 * math.sin(index / 5)
                change = wave if symbol == "BTC" else -wave
                volume = 1_000.0
            previous = prices[symbol]
            close = max(previous * (1.0 + change), 1.0)
            prices[symbol] = close
            rows.append(
                {
                    "timestamp": timestamp.isoformat(),
                    "symbol": symbol,
                    "open": previous,
                    "high": max(previous, close) * 1.001,
                    "low": min(previous, close) * 0.999,
                    "close": close,
                    "volume": volume,
                }
            )
    return pd.DataFrame(rows)


def _forensics_row(
    timestamp: dt.datetime,
    bps: float,
    *,
    symbol: str = "HYPE",
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "entry_reason": "incumbent",
        "exit_reason": "bracket_stop" if bps < 0 else "time_exit",
        "realized_bps": bps,
        "entry_ts": (timestamp - dt.timedelta(hours=1)).isoformat(),
        "exit_ts": timestamp.isoformat(),
        "regime_at_entry": {
            "trend": "down",
            "vol_pctile": 80.0,
            "session": "us",
        },
        "archetype": "trend_fight" if bps < 0 else "clean_win",
    }


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _loss_job(tmp_path: Path, *, response: str | None = None) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("regime-loss", agent_mode="intervene")
    job.execution_params["initial_capital"] = 100.0
    store.save(job)
    root = store.job_dir(job.id)
    now = dt.datetime(2026, 8, 21, tzinfo=dt.UTC)

    baseline = [
        _forensics_row(now - dt.timedelta(days=60 - index), 80.0 + index)
        for index in range(30)
    ]
    backtest = root / "results" / "backtest"
    backtest.mkdir(parents=True, exist_ok=True)
    (backtest / "trade_forensics.json").write_text(
        json.dumps({"trades": baseline}), encoding="utf-8"
    )
    forward_forensics = [
        _forensics_row(now - dt.timedelta(days=2), -300.0),
        _forensics_row(now - dt.timedelta(days=1), -50.0, symbol="SOL"),
        _forensics_row(now - dt.timedelta(hours=2), -50.0, symbol="SOL"),
    ]
    _write_jsonl(
        root / "results" / "forward" / "trade_forensics.jsonl",
        forward_forensics,
    )
    trades = [
        {
            "symbol": "HYPE",
            "closed_at": (now - dt.timedelta(days=2)).isoformat(),
            "net_pnl": -12.0,
        },
        {
            "symbol": "SOL",
            "closed_at": (now - dt.timedelta(days=1)).isoformat(),
            "net_pnl": -1.0,
        },
        {
            "symbol": "SOL",
            "closed_at": (now - dt.timedelta(hours=2)).isoformat(),
            "net_pnl": -1.0,
        },
    ]
    _write_jsonl(root / "results" / "forward" / "trades.jsonl", trades)
    if response:
        protected = governance_dir(tmp_path, job.id)
        protected.mkdir(parents=True, exist_ok=True)
        (protected / "hard_constraints.yaml").write_text(
            "max_drawdown_pct: 0.15\n"
            "regime_response:\n"
            f"  warning: {response}\n"
            f"  critical: {response}\n"
            "  max_leverage: 1.0\n",
            encoding="utf-8",
        )
        commit_epoch(protected, note="test owner policy")
    return store, job.id


def test_market_state_detects_joint_regime_break() -> None:
    funding_start = pd.Timestamp("2026-06-20T00:00:00Z")
    funding_rows = []
    for index in range(60 * 6):
        timestamp = funding_start + pd.Timedelta(hours=4 * index)
        funding_rows.append(
            {
                "timestamp": timestamp.isoformat(),
                "symbol": "ETH",
                "value": (
                    8e-5 if index >= (53 * 6) else (1e-5 if index % 2 else -1e-5)
                ),
            }
        )
    report = summarize_market_state(
        _market_bars(), funding_rows=funding_rows, windows=(7,)
    )
    window = report["windows"]["7"]
    assert report["available"] is True
    assert window["volatility_ratio"] > 2.5
    assert window["correlation_delta"] > 0.35
    assert window["liquidity_ratio"] < 0.1
    assert window["regime_js_divergence"] > 0.1
    assert abs(window["funding_shift"]["z_score"]) > 3


def test_large_concentrated_drawdown_is_critical_and_refreshes_attribution(
    tmp_path: Path,
) -> None:
    store, job_id = _loss_job(tmp_path)
    now = dt.datetime(2026, 8, 21, tzinfo=dt.UTC)
    # Agent-writable job metadata cannot move the detector's goalposts.
    job = store.load(job_id)
    job.performance["regime_detector"] = {"drawdown_critical": 0.99}
    store.save(job)
    report = regime_health_job(job_id, store=store, force=True, now=now)

    assert report["status"] == "critical"
    assert {signal["kind"] for signal in report["signals"]} >= {
        "drawdown",
        "edge_decay",
        "loss_concentration",
    }
    seven = report["windows"]["windows"]["7"]
    assert seven["max_drawdown_pct"] == 0.14
    assert seven["largest_loss_share"] == pytest.approx(12 / 14, abs=1e-4)
    assert report["policy"]["action"] == "alert_only"
    assert report["response"]["applied"] is False
    assert report["attribution"]["required"] is True
    assert (store.job_dir(job_id) / "results/research/attribution.json").exists()

    # Recomputing unchanged evidence does not emit another transition event.
    regime_health_job(job_id, store=store, force=True, now=now)
    journal = (store.job_dir(job_id) / "journal.jsonl").read_text(encoding="utf-8")
    assert journal.count("portfolio_regime_shift_detected") == 1


def test_market_only_break_warns_before_forward_losses(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("regime-market", agent_mode="intervene")
    store.save(job)
    market = summarize_market_state(_market_bars(), windows=(7,))
    store.write_json(job.id, "results/research/market_state.json", market)

    report = regime_health_job(
        job.id,
        store=store,
        force=True,
        now=pd.Timestamp(market["as_of"]).to_pydatetime(),
    )

    assert report["status"] == "warning"
    assert report["transition"]["alert"] is True
    assert report["windows"]["windows"]["7"]["closed_trades"] == 0
    assert {signal["kind"] for signal in report["signals"]} >= {
        "volatility_shift",
        "correlation_shift",
        "liquidity_deterioration",
    }
    assert report["response"]["applied"] is False


def test_stale_market_artifact_is_not_treated_as_current_regime(
    tmp_path: Path,
) -> None:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("regime-stale-market", agent_mode="intervene")
    store.save(job)
    market = summarize_market_state(_market_bars(), windows=(7,))
    store.write_json(job.id, "results/research/market_state.json", market)

    report = regime_health_job(
        job.id,
        store=store,
        force=True,
        now=pd.Timestamp(market["as_of"]).to_pydatetime() + dt.timedelta(days=2),
    )

    assert [signal["kind"] for signal in report["signals"]] == ["market_data_stale"]
    assert report["status"] == "watch"


def test_legacy_constitution_cannot_authorize_automatic_response(
    tmp_path: Path,
) -> None:
    store, job_id = _loss_job(tmp_path)
    root = store.job_dir(job_id)
    (root / "constitution.yaml").write_text(
        "hard_constraints:\n"
        "  regime_response:\n"
        "    warning: flatten\n"
        "    critical: flatten\n",
        encoding="utf-8",
    )

    report = regime_health_job(
        job_id,
        store=store,
        force=True,
        now=dt.datetime(2026, 8, 21, tzinfo=dt.UTC),
    )

    assert report["status"] == "critical"
    assert report["governance"]["trusted"] is False
    assert report["policy"]["action"] == "alert_only"
    assert "verified protected governance" in report["policy"]["error"]
    assert read_halt(root) is None


def test_owner_governed_pause_latches_and_requires_owner_clear(tmp_path: Path) -> None:
    store, job_id = _loss_job(tmp_path, response="pause_entries")
    report = regime_health_job(
        job_id,
        store=store,
        force=True,
        now=dt.datetime(2026, 8, 21, tzinfo=dt.UTC),
    )
    halt = read_halt(store.job_dir(job_id))
    assert report["response"]["applied"] is True
    assert halt and halt["source"] == "regime_health" and halt["flatten"] is False
    with pytest.raises(PermissionError, match="requires by='owner'"):
        clear_halt(store, job_id, by="agent")
    assert clear_halt(store, job_id, by="owner")["cleared"] is True


def test_governed_leverage_cap_is_runtime_only(tmp_path: Path) -> None:
    store, job_id = _loss_job(tmp_path, response="clamp_leverage")
    root = store.job_dir(job_id)
    report = regime_health_job(
        job_id,
        store=store,
        force=True,
        now=dt.datetime(2026, 8, 21, tzinfo=dt.UTC),
    )
    assert report["response"]["effective_on"] == "next_tick"
    assert active_regime_leverage_cap(report) == 1.0
    assert store.load(job_id).execution_params.get("leverage") is None

    # A healthy report releases the cap without rewriting the user's dial.
    report_path = root / "results" / "research" / "regime_health.json"
    doc = json.loads(report_path.read_text(encoding="utf-8"))
    doc["status"] = "healthy"
    report_path.write_text(json.dumps(doc), encoding="utf-8")
    assert active_regime_leverage_cap(doc) is None
