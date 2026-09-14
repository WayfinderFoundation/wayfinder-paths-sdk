"""Freestyle jobs: the action contract, the paper runtime, the validation
ladder with its sandboxed dry run, and the compiler wrapper."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wayfinder_paths.jobs.freestyle.contract import FreestyleSpec, normalize_action
from wayfinder_paths.jobs.freestyle.runtime import (
    LEDGER_PATH,
    STATE_PATH,
    run_freestyle_tick,
)
from wayfinder_paths.jobs.freestyle.validate import (
    static_checks,
    validate_freestyle_job,
)
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore

HORMUZ = """
from wayfinder_paths.jobs.freestyle import FreestyleSpec

SPEC = FreestyleSpec(venues=("polymarket", "hyperliquid"), max_notional_per_tick=500, max_loss_usd=20)

def tick(ctx):
    odds = ctx.quote("polymarket", "polymarket:hormuz-closure-2026:YES")
    ctx.state["last_odds"] = odds
    if odds > 0.4 and "BTC" not in ctx.positions:
        ctx.act({"venue": "hyperliquid", "kind": "market", "symbol": "BTC", "side": "long",
                 "notional": 100, "max_loss": 10})
    elif odds <= 0.4 and "BTC" in ctx.positions:
        ctx.act({"venue": "hyperliquid", "kind": "close", "symbol": "BTC"})
"""


def _job(
    tmp_path: Path, source: str = HORMUZ, **params
) -> tuple[JobStore, WayfinderJob]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "hormuz-perp",
        script="workspace/src/hormuz_perp.py",
        interval_seconds=300,
        timeout_seconds=120,
        execution_contract="freestyle_v1",
        source={"kind": "freestyle", "origin": "inline"},
    )
    job.execution_params.update(params)
    root = store.init_layout(job)
    (root / "workspace" / "src" / "hormuz_perp.py").write_text(source, encoding="utf-8")
    store.save(job)
    return store, job


def test_normalize_action_maps_kinds_to_engine_intents() -> None:
    intent = normalize_action(
        {
            "venue": "hyperliquid",
            "kind": "market",
            "symbol": "BTC",
            "side": "long",
            "notional": 100,
            "max_loss": 5,
        }
    )
    assert intent.action == "OPEN" and intent.side == "long" and intent.notional == 100
    assert intent.metadata["max_loss"] == 5
    close = normalize_action(
        {"venue": "hyperliquid", "kind": "close", "symbol": "BTC"}, position_side="long"
    )
    assert close.action == "CLOSE" and close.reduce_only and close.side == "sell"
    buy = normalize_action(
        {
            "venue": "polymarket",
            "kind": "buy",
            "symbol": "polymarket:m:YES",
            "notional": 10,
        }
    )
    assert buy.side == "long"
    with pytest.raises(ValueError):
        normalize_action({"venue": "hyperliquid", "kind": "close", "symbol": "BTC"})
    with pytest.raises(ValueError):
        normalize_action({"venue": "hyperliquid", "kind": "market", "symbol": "BTC"})


def test_spec_from_mapping_and_defaults() -> None:
    spec = FreestyleSpec.from_any({"venues": ["hyperliquid"], "max_loss_usd": "5"})
    assert spec.venues == ("hyperliquid",) and spec.max_loss_usd == 5.0
    assert FreestyleSpec.from_any(None).quote_interval == "5m"


def test_dry_run_paper_ticks_fill_through_the_seam_and_leave_the_job_untouched(
    tmp_path: Path,
) -> None:
    store, job = _job(tmp_path)
    root = store.job_dir(job.id)
    marks = {
        "polymarket:polymarket:hormuz-closure-2026:YES": 0.6,
        "hyperliquid:BTC": 50_000.0,
    }
    payload = run_freestyle_tick(root, dry_run=True, ticks=2, marks=marks)
    assert payload["ok"], payload
    assert payload["fills"] == [] or payload["fills"][0]["symbol"] == "BTC"
    # the first tick opened BTC; the second held it (odds still above 0.4)
    assert "BTC" in payload["positions"]
    assert payload["positions"]["BTC"]["side"] == "long"
    assert payload["equity"] > 0
    # the dry run never touches the real ledger, state or forward dir
    assert not (root / LEDGER_PATH).exists()
    assert not (root / STATE_PATH).exists()
    # the store lays out empty forward files at creation; the dry run adds no rows
    real_orders = root / "results" / "forward" / "orders.jsonl"
    assert not real_orders.exists() or real_orders.stat().st_size == 0
    assert (
        root / "reports" / "validation" / "dryrun" / "forward" / "fills.jsonl"
    ).exists()


def test_paper_tick_persists_ledger_state_and_forward_rows(
    tmp_path: Path, monkeypatch
) -> None:
    store, job = _job(tmp_path)
    root = store.job_dir(job.id)
    from wayfinder_paths.jobs.freestyle import runtime as rt

    class _Gateway(rt.StubVenueGateway):
        pass

    monkeypatch.setattr(
        rt,
        "VenueGateway",
        lambda **kwargs: _Gateway(
            marks={
                "polymarket:polymarket:hormuz-closure-2026:YES": 0.7,
                "hyperliquid:BTC": 40_000.0,
            }
        ),
    )
    monkeypatch.setattr(rt, "fire_triggers", lambda *a, **k: None)
    monkeypatch.setattr(rt, "JobStore", lambda: store)
    monkeypatch.setenv("WAYFINDER_JOB_MODE", "paper")
    monkeypatch.delenv("WAYFINDER_DRY_RUN", raising=False)
    monkeypatch.delenv("WAYFINDER_JOB_REVISION", raising=False)
    monkeypatch.delenv("WAYFINDER_FORWARD_DIR", raising=False)
    payload = run_freestyle_tick(root)
    assert payload["ok"], payload
    ledger = json.loads((root / LEDGER_PATH).read_text())
    assert ledger["mode"] == "paper" and "BTC" in ledger["ledger"]["positions"]
    state = json.loads((root / STATE_PATH).read_text())
    assert state["last_odds"] == pytest.approx(0.7)
    rows = (root / "results" / "forward" / "fills.jsonl").read_text().splitlines()
    assert len(rows) == 1 and json.loads(rows[0])["mode"] == "paper"
    runs = (root / "results" / "forward" / "runs.jsonl").read_text().splitlines()
    assert json.loads(runs[-1])["status"] == "ok"
    # second tick with odds below the threshold closes the position and books a trade
    monkeypatch.setattr(
        rt,
        "VenueGateway",
        lambda **kwargs: _Gateway(
            marks={
                "polymarket:polymarket:hormuz-closure-2026:YES": 0.2,
                "hyperliquid:BTC": 41_000.0,
            }
        ),
    )
    payload = run_freestyle_tick(root)
    assert payload["ok"] and "BTC" not in payload["positions"]
    trades = (root / "results" / "forward" / "trades.jsonl").read_text().splitlines()
    assert json.loads(trades[-1])["net_pnl"] > 0


def test_revision_drift_refuses_the_tick(tmp_path: Path, monkeypatch) -> None:
    store, job = _job(tmp_path)
    root = store.job_dir(job.id)
    monkeypatch.setenv("WAYFINDER_JOB_MODE", "paper")
    monkeypatch.setenv("WAYFINDER_JOB_REVISION", "deadbeef0000")
    monkeypatch.delenv("WAYFINDER_DRY_RUN", raising=False)
    from wayfinder_paths.jobs.freestyle import runtime as rt

    monkeypatch.setattr(rt, "fire_triggers", lambda *a, **k: None)
    monkeypatch.setattr(rt, "JobStore", lambda: store)
    payload = run_freestyle_tick(root)
    assert payload["ok"] is False and payload["refused"] is True
    assert "revision drift" in payload["error"]


def test_halted_job_refuses_openers_but_allows_exits(
    tmp_path: Path, monkeypatch
) -> None:
    store, job = _job(tmp_path)
    root = store.job_dir(job.id)
    from wayfinder_paths.jobs.freestyle import runtime as rt
    from wayfinder_paths.jobs.halt import request_halt

    request_halt(store, job.id, reason="owner stop")
    monkeypatch.setattr(
        rt,
        "VenueGateway",
        lambda **kwargs: rt.StubVenueGateway(
            marks={
                "polymarket:polymarket:hormuz-closure-2026:YES": 0.9,
                "hyperliquid:BTC": 40_000.0,
            }
        ),
    )
    monkeypatch.setattr(rt, "fire_triggers", lambda *a, **k: None)
    monkeypatch.setattr(rt, "JobStore", lambda: store)
    monkeypatch.setenv("WAYFINDER_JOB_MODE", "paper")
    monkeypatch.delenv("WAYFINDER_DRY_RUN", raising=False)
    monkeypatch.delenv("WAYFINDER_JOB_REVISION", raising=False)
    payload = run_freestyle_tick(root)
    assert payload["ok"] and payload["halted"]
    assert payload["actions"][0]["status"] == "refused"
    assert "halted" in payload["actions"][0]["reason"]


def test_static_checks_block_direct_venue_writes_and_async_ticks(
    tmp_path: Path,
) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import time\nfrom wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_place_market_order\n"
        "async def tick(ctx):\n    time.sleep(1)\n    await hyperliquid_place_market_order(x=1)\n",
        encoding="utf-8",
    )
    by_name = {c["name"]: c for c in static_checks(bad)}
    assert by_name["no_direct_venue_writes"]["passed"] is False
    assert by_name["tick_is_sync"]["passed"] is False
    assert by_name["no_sleep_loops"]["passed"] is False
    good = tmp_path / "good.py"
    good.write_text(HORMUZ, encoding="utf-8")
    assert all(c["passed"] for c in static_checks(good))


def test_validate_freestyle_job_runs_the_sandboxed_dry_run(tmp_path: Path) -> None:
    store, job = _job(
        tmp_path,
        freestyle={
            "validation_marks": {
                "polymarket:polymarket:hormuz-closure-2026:YES": 0.55,
                "hyperliquid:BTC": 30_000,
            }
        },
    )
    report = validate_freestyle_job(job.id, store=store)
    by_name = {c["name"]: c for c in report["checks"]}
    assert by_name["dry_run_ok"]["passed"], by_name["dry_run_ok"]
    assert report["status"] == "passed"
    assert report["kind"] == "freestyle_v1" and report["revision"]
    assert report["freestyle"]["spec"]["max_loss_usd"] == 20
    assert report["freestyle"]["dry_run"]["intents"][0]["symbol"] == "BTC"
    assert (store.job_dir(job.id) / "reports" / "validation" / "latest.json").exists()


def test_validate_freestyle_job_fails_a_crashing_script(tmp_path: Path) -> None:
    store, job = _job(
        tmp_path, source="def tick(ctx):\n    raise RuntimeError('boom')\n"
    )
    report = validate_freestyle_job(job.id, store=store)
    by_name = {c["name"]: c for c in report["checks"]}
    assert by_name["dry_run_ok"]["passed"] is False
    assert "boom" in str(by_name["dry_run_ok"]["error"])
    assert report["status"] == "failed"


def test_compiler_writes_the_freestyle_wrapper(tmp_path: Path, monkeypatch) -> None:
    from wayfinder_paths.jobs import compiler as compiler_mod

    class _Bridge:
        def __init__(self, *, repo_root=None):
            self.calls = []

        def ensure_started(self):
            return {"ok": True}

        def add_or_update_script_job(self, **kwargs):
            self.calls.append(kwargs)
            return {"ok": True, "result": {"name": kwargs["name"]}}

        def delete(self, name):
            return {"ok": True}

    monkeypatch.setattr(compiler_mod, "RunnerBridge", _Bridge)
    store, job = _job(tmp_path)
    links = compiler_mod.JobCompiler(store=store).compile(job, start_daemon=False)
    wrapper = (
        Path(tmp_path) / links["jobs"][0]["response"]["result"]["name"]
        if False
        else None
    )
    script_wrapper = store.runs_jobs_dir / "hormuz_perp_script.py"
    text = script_wrapper.read_text(encoding="utf-8")
    assert "run_freestyle_tick as run_tick" in text
    assert "SystemExit(2" in text
    assert wrapper is None
