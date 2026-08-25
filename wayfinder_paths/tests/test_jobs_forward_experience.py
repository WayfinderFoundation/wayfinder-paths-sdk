from __future__ import annotations

from datetime import UTC, datetime, timedelta

from wayfinder_paths.jobs.forward import ForwardRecorder
from wayfinder_paths.jobs.forward_experience import (
    AUDIT_COVERAGE_TARGET,
    CONSERVATIVE_PRIOR_BPS,
    build_forward_experience,
    execution_cost_assumptions,
)
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore


def _job(store: JobStore, job_id: str, mode: str) -> None:
    job = WayfinderJob.new(job_id, script="workspace/src/strategy.py")
    job.script_loop.mode = mode
    store.save(job)


def test_forward_experience_separates_live_costs_from_paper_priors(tmp_path) -> None:
    store = JobStore(repo_root=tmp_path)
    _job(store, "target", "paper")
    _job(store, "live-source", "live")
    _job(store, "paper-source", "paper")
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)

    live = ForwardRecorder(
        job_id="live-source",
        job_dir=store.job_dir("live-source"),
        mode="live",
    )
    for index in range(80):
        live.record_fill(
            ts=(now - timedelta(hours=80 - index)).isoformat(),
            symbol="BTC",
            venue="hyperliquid",
            order_type="market",
            avg_price=100.0,
            raw={"reference_price": 99.98},
            fill_id=f"fill-{index}",
        )
    for index in range(5):
        live.record_fill(
            ts=(now - timedelta(hours=index + 1)).isoformat(),
            symbol="ETH",
            venue="hyperliquid",
            slippage_bps=2.0,
            fill_id=f"thin-{index}",
        )

    paper = ForwardRecorder(
        job_id="paper-source",
        job_dir=store.job_dir("paper-source"),
        mode="paper",
    )
    paper.record_trade_close(
        ts=(now - timedelta(hours=1)).isoformat(),
        symbol="SOL",
        net_pnl=3.5,
    )
    paper.record_trade_close(
        ts=(now - timedelta(hours=1)).isoformat(),
        symbol="SOL",
        net_pnl=3.5,
    )

    report = build_forward_experience(store, "target", now=now)
    assert report["generated_at"] == now.isoformat()
    live_block = report["live_execution"]
    btc = live_block["cells"]["hyperliquid|BTC|market"]
    eth = live_block["cells"]["hyperliquid|ETH|market"]
    assert btc["method"] == "empirical"
    assert btc["audit_passed"] is True
    assert btc["audit_coverage"] >= AUDIT_COVERAGE_TARGET
    assert eth["method"] == "conservative_prior"
    assert eth["p90_bps"] == CONSERVATIVE_PRIOR_BPS
    assert execution_cost_assumptions(report, symbols={"ETH"}) == {
        "p50_bps": CONSERVATIVE_PRIOR_BPS,
        "p90_bps": CONSERVATIVE_PRIOR_BPS,
        "audit_passed": True,
        "source": "owner_live_fills",
        "cells": 1,
    }
    assert report["paper_strategy_priors"] == [
        {
            "job_id": "paper-source",
            "closed_trades": 1,
            "net_pnl": 3.5,
            "symbols": ["SOL"],
        }
    ]


def test_forward_experience_never_crosses_job_store_boundary(tmp_path) -> None:
    owner = JobStore(repo_root=tmp_path / "owner")
    outsider = JobStore(repo_root=tmp_path / "outsider")
    _job(owner, "target", "paper")
    _job(outsider, "foreign-live", "live")
    recorder = ForwardRecorder(
        job_id="foreign-live",
        job_dir=outsider.job_dir("foreign-live"),
        mode="live",
    )
    recorder.record_fill(ts="2026-08-25T11:00:00+00:00", slippage_bps=99, symbol="BTC")
    report = build_forward_experience(
        owner, "target", now=datetime(2026, 8, 25, 12, tzinfo=UTC)
    )
    assert report["live_execution"]["samples"] == 0
    assert report["owner_scope"] == "job_store"
