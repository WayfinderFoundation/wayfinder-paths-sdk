"""Fleet portfolio awareness: correlation/overlap report from forward
books, the per-job wake block with its diversification directive, the
scheduler boost, and the watchdog cadence hook."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.portfolio import (
    build_portfolio_report,
    portfolio_block,
    portfolio_report_path,
    write_portfolio_report,
)
from wayfinder_paths.jobs.store import JobStore


def _job_with_book(store, job_id, daily_pnl, symbol="HYPE"):
    job = WayfinderJob.new(
        job_id,
        script="workspace/src/strategy.py",
        agent_mode="intervene",
        execution_contract="jobs_v1",
    )
    store.save(job)
    trades = store.job_dir(job_id) / "results" / "forward" / "trades.jsonl"
    trades.parent.mkdir(parents=True, exist_ok=True)
    with trades.open("w", encoding="utf-8") as handle:
        for day, pnl in daily_pnl.items():
            handle.write(
                json.dumps(
                    {
                        "closed_at": f"{day}T12:00:00+00:00",
                        "net_pnl": pnl,
                        "symbol": symbol,
                        "size": 10.0,
                        "price": 2.5,
                    }
                )
                + "\n"
            )
    return job_id


DAYS = [f"2026-08-{d:02d}" for d in range(1, 9)]


def test_correlation_and_overlap(tmp_path) -> None:
    store = JobStore(repo_root=tmp_path)
    series = {day: float(i + 1) for i, day in enumerate(DAYS)}
    _job_with_book(store, "alpha", series, symbol="HYPE")
    _job_with_book(store, "beta", series, symbol="HYPE")  # identical book
    _job_with_book(
        store,
        "gamma",
        {day: -value for day, value in series.items()},
        symbol="BTC",
    )
    _job_with_book(store, "sparse", {DAYS[0]: 1.0}, symbol="ETH")  # < min days

    report = build_portfolio_report(store)
    jobs = report["jobs"]
    assert jobs["alpha"]["correlations"]["beta"] == 1.0
    assert jobs["alpha"]["correlations"]["gamma"] == -1.0
    assert "sparse" not in jobs["alpha"]["correlations"]  # below shared-day floor
    assert report["shared_symbols"] == {"HYPE": ["alpha", "beta"]}
    assert jobs["alpha"]["gross_notional_by_symbol"]["HYPE"] == 200.0  # 8*10*2.5


def test_block_and_scheduler_boost_on_high_correlation(tmp_path) -> None:
    store = JobStore(repo_root=tmp_path)
    series = {day: float(i + 1) for i, day in enumerate(DAYS)}
    _job_with_book(store, "alpha", series)
    _job_with_book(store, "beta", series)
    write_portfolio_report(store)

    block = portfolio_block(store, "alpha")
    assert block["avg_correlation_to_fleet"] == 1.0
    assert block["shared_symbols"] == {"HYPE": ["beta"]}
    assert "diversification_directive" in block

    from wayfinder_paths.jobs.improver.scheduler import assign_island

    result = assign_island(store, "alpha")
    assert any("correlation" in reason for reason in result["reasons"])
    assert result["target_weights"]["diversification"] > 0.1  # doubled, renormed

    # No report for an unknown job -> no block, never raises.
    assert portfolio_block(store, "nope") is None


def test_watchdog_refresh_is_rate_limited(tmp_path) -> None:
    from wayfinder_paths.jobs.watchdog import _refresh_portfolio_report

    store = JobStore(repo_root=tmp_path)
    series = {day: float(i + 1) for i, day in enumerate(DAYS)}
    _job_with_book(store, "alpha", series)
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    _refresh_portfolio_report(store, now)
    path = portfolio_report_path(store)
    first = path.read_text()

    # Fresh report: a pass minutes later leaves it untouched.
    _job_with_book(store, "beta", series)
    _refresh_portfolio_report(store, now)
    assert path.read_text() == first

    # Stale report: the next pass past the window rebuilds with the new job.
    stale = json.loads(first)
    stale["generated_at"] = "2026-08-17T10:00:00+00:00"
    path.write_text(json.dumps(stale))
    _refresh_portfolio_report(store, now)
    assert "beta" in json.loads(path.read_text())["jobs"]


def test_watchdog_pass_writes_report_end_to_end(tmp_path) -> None:
    from wayfinder_paths.jobs.watchdog import recover_stalled_applications

    store = JobStore(repo_root=tmp_path)
    series = {day: float(i + 1) for i, day in enumerate(DAYS)}
    _job_with_book(store, "alpha", series)
    outcome = recover_stalled_applications(store=store)
    assert not [e for e in outcome["errors"] if e.get("job_id") == "_portfolio"]
    assert portfolio_report_path(store).exists()


def test_wake_context_carries_portfolio_block(tmp_path) -> None:
    from wayfinder_paths.jobs.worker import prepare_job_worker_prompt

    store = JobStore(repo_root=tmp_path)
    series = {day: float(i + 1) for i, day in enumerate(DAYS)}
    _job_with_book(store, "alpha", series)
    _job_with_book(store, "beta", series)
    write_portfolio_report(store)
    sections = prepare_job_worker_prompt(store=store, job_id="alpha", mode="intervene")
    assert "avg_correlation_to_fleet" in sections["prompt"]
    assert "diversification_directive" in sections["prompt"]
