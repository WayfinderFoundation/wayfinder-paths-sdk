"""Engine-level leverage wire: the operator knob (execution_params.leverage)
scales order sizing at the ONE seam backtest and live share, exactly once
(compound strategies stamp leverage_applied and are skipped), never on
reduce-only intents — plus the detached gate-restamp flow the knob kicks."""

from __future__ import annotations

import json

from wayfinder_paths.jobs.execution.engine import _apply_engine_leverage
from wayfinder_paths.jobs.execution.primitives import OrderIntent
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore


def _open(**kw) -> OrderIntent:
    defaults = dict(action="OPEN", venue="hyperliquid", symbol="HYPE", side="sell")
    defaults.update(kw)
    return OrderIntent.from_any(defaults)


def test_engine_scales_unstamped_open_intents() -> None:
    intents = [_open(notional=500.0), _open(size=3.0)]
    _apply_engine_leverage(intents, {"leverage": 2.0})
    assert intents[0].notional == 1000.0
    assert intents[1].size == 6.0
    for intent in intents:
        assert intent.metadata["leverage_applied"] is True
        assert intent.metadata["engine_leverage"] == 2.0


def test_engine_skips_reduce_only_and_stamped() -> None:
    close = _open(action="CLOSE", size=3.0)
    reduce = _open(size=3.0, reduce_only=True)
    stamped = _open(notional=500.0, metadata={"leverage_applied": True})
    intents = [close, reduce, stamped]
    _apply_engine_leverage(intents, {"leverage": 2.0})
    assert close.size == 3.0
    assert reduce.size == 3.0
    assert stamped.notional == 500.0


def test_engine_inert_without_leverage() -> None:
    for params in (
        {},
        {"leverage": None},
        {"leverage": 1.0},
        {"leverage": 0},
        {"leverage": "x"},
    ):
        intent = _open(notional=500.0)
        _apply_engine_leverage([intent], params)
        assert intent.notional == 500.0
        assert "leverage_applied" not in (intent.metadata or {})


def test_engine_scaling_is_idempotent() -> None:
    intent = _open(notional=500.0)
    _apply_engine_leverage([intent], {"leverage": 2.0})
    _apply_engine_leverage([intent], {"leverage": 2.0})  # persisted pending intent
    assert intent.notional == 1000.0


def test_compound_base_stamps_leverage_applied() -> None:
    import inspect

    from wayfinder_paths.jobs import strategies as strategies_pkg
    from wayfinder_paths.jobs.strategies import _base

    source = inspect.getsource(_base)
    # The stamp is the double-scaling guard: compound sizing already
    # multiplied equity x leverage, so its intents must opt out of the
    # engine-level knob.
    assert "leverage_applied" in source
    assert strategies_pkg is not None


def test_apply_execution_leverage_kicks_restamp(tmp_path, monkeypatch) -> None:
    from wayfinder_paths.jobs import sync as sync_module
    from wayfinder_paths.jobs.sync import apply_execution_leverage

    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("lev-wire", agent_mode="intervene")
    job.execution_params["leverage"] = 2.0
    store.save(job)

    monkeypatch.setattr(sync_module, "sync_all_jobs", lambda **kwargs: None)
    spawned: list[tuple[str, str, dict]] = []

    def fake_spawn(store_arg, job_id, op, kwargs):
        spawned.append((job_id, op, kwargs))
        return {"started": True, "op": op, "pid": 1}

    monkeypatch.setattr("wayfinder_paths.jobs.background.spawn_detached_op", fake_spawn)

    result = apply_execution_leverage(job.id, 3.0, store=store)
    assert result["restamp"]["started"] is True
    assert spawned == [(job.id, "restamp", {"job_id": job.id})]
    journal = (store.job_dir(job.id) / "journal.jsonl").read_text()
    assert "gate_restamp_kicked" in journal

    # Unchanged value: no restamp churn.
    result = apply_execution_leverage(job.id, 3.0, store=store)
    assert result["restamp"] is None
    assert len(spawned) == 1

    # Spawn failure degrades to an error note; the knob still succeeds.
    def boom(*args):
        raise RuntimeError("no subprocess")

    monkeypatch.setattr("wayfinder_paths.jobs.background.spawn_detached_op", boom)
    result = apply_execution_leverage(job.id, 4.0, store=store)
    assert result["leverage"] == 4.0
    assert "error" in result["restamp"]


def test_spawn_detached_op_echo_round_trip(tmp_path) -> None:
    from wayfinder_paths.jobs.background import spawn_detached_op

    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("bg-echo", agent_mode="intervene")
    store.save(job)

    outcome = spawn_detached_op(store, job.id, "__echo__", {"hello": "world"})
    assert outcome["started"] is True
    ops_dir = store.job_dir(job.id) / "state" / "background_ops"
    status = json.loads((ops_dir / "__echo__.json").read_text())
    assert status["state"] == "running"
    assert status["pid"] == outcome["pid"]

    # The detached child completes and writes the result file op_status reads.
    import time

    result_path = ops_dir / "__echo__.result.json"
    for _ in range(100):
        try:
            if json.loads(result_path.read_text()) == {"hello": "world"}:
                break
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    assert json.loads(result_path.read_text()) == {"hello": "world"}


def test_gate_shows_restamp_in_progress(tmp_path, monkeypatch) -> None:
    import os

    from wayfinder_paths.jobs import sync as sync_module

    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("gate-flag", agent_mode="intervene")
    store.save(job)
    monkeypatch.setattr(
        sync_module,
        "evaluate_live_gate",
        lambda job_id, store=None: {"live_ready": False, "reasons": ["stale"]},
    )

    gate = sync_module._gate_with_restamp(job.id, store)
    assert "restamp_in_progress" not in gate

    ops_dir = store.job_dir(job.id) / "state" / "background_ops"
    ops_dir.mkdir(parents=True, exist_ok=True)
    (ops_dir / "restamp.json").write_text(
        json.dumps({"state": "running", "pid": os.getpid(), "started_at": "2026-08-17"})
    )
    gate = sync_module._gate_with_restamp(job.id, store)
    assert gate["restamp_in_progress"] is True
    assert gate["live_ready"] is False  # authoritative gate stays strict

    (ops_dir / "restamp.json").write_text(
        json.dumps({"state": "running", "pid": 2**22 - 1})
    )
    gate = sync_module._gate_with_restamp(job.id, store)
    assert "restamp_in_progress" not in gate


def test_op_runner_restamp_dispatch(monkeypatch, tmp_path) -> None:
    from wayfinder_paths.jobs.execution import op_runner

    calls: list[str] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.execution.validation.validate_execution_job",
        lambda job_id: calls.append("validate") or {"status": "passed"},
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.execution.job.backtest_execution_job",
        lambda job_id: calls.append("backtest")
        or {"result": {"stats": {"net_return": 0.1}}},
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.execution.preflight.run_preflight",
        lambda job_id: calls.append("preflight") or {"status": "green"},
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.gating.evaluate_live_gate",
        lambda job_id: {"live_ready": True, "reasons": []},
    )
    result = op_runner._run("restamp", {"job_id": "x"})
    assert calls == ["validate", "backtest", "preflight"]
    assert result["validation"] == "passed"
    assert result["preflight"] == "green"
    assert result["gate"]["live_ready"] is True
