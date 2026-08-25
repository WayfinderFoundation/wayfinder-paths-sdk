"""Pre-registered decision gates: mechanical rework-vs-retire resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from wayfinder_paths.jobs import compiler as compiler_mod
from wayfinder_paths.jobs.decision_gates import (
    evaluate_decision_gates,
    evaluate_gate_criteria,
    load_decision_gates,
    register_decision_gate,
    reopen_decision_gate,
    resolve_decision_gate,
)
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.owner_attention import build_owner_attention
from wayfinder_paths.jobs.store import JobStore

FAILING_SUMMARY = {
    "runs": {"count": 40},
    "trades": {
        "closed_count": 20,
        "wins": 6,
        "losses": 14,
        "win_rate": 0.3,
        "net_pnl": -12.5,
    },
}
CRITERIA = {"min_trades": 20, "max_win_rate": 0.4, "max_net_pnl": 0}


@pytest.fixture()
def compiled(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    compiles: list[str] = []

    class FakeCompiler:
        def __init__(self, *, store=None) -> None:
            self.store = store

        def compile(self, job, **kwargs):
            compiles.append(job.id)
            return {"ok": True}

    monkeypatch.setattr(compiler_mod, "JobCompiler", FakeCompiler)
    return compiles


def _store(
    tmp_path: Path, *, wallet_label: str | None = None
) -> tuple[JobStore, WayfinderJob]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "gate-demo",
        script=".wayfinder_runs/demo.py",
        interval_seconds=60,
        agent_mode="intervene",
        execution_contract="jobs_v1",
    )
    if wallet_label:
        job.execution_params["wallet_label"] = wallet_label
    store.save(job)
    workspace = store.job_dir(job.id) / "workspace" / "src"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "strategy.py").write_text("print('imx')\n", encoding="utf-8")
    return store, job


def _journal_types(store: JobStore, job_id: str) -> list[str]:
    return [
        str(event.get("type")) for event in store.read_jsonl(job_id, "journal.jsonl")
    ]


# ── registry lifecycle ───────────────────────────────────────────────────


def test_register_requires_window_ceiling_and_successor(tmp_path: Path) -> None:
    store, job = _store(tmp_path)
    with pytest.raises(ValueError, match="min_trades"):
        register_decision_gate(
            store, job.id, criteria={"max_win_rate": 0.4}, successor_ref="lane-b"
        )
    with pytest.raises(ValueError, match="ceiling"):
        register_decision_gate(
            store, job.id, criteria={"min_trades": 20}, successor_ref="lane-b"
        )
    with pytest.raises(ValueError, match="successor_ref"):
        register_decision_gate(store, job.id, criteria=CRITERIA, successor_ref="  ")
    with pytest.raises(ValueError, match="unknown criteria"):
        register_decision_gate(
            store,
            job.id,
            criteria={**CRITERIA, "vibes": 1},
            successor_ref="lane-b",
        )

    gate = register_decision_gate(
        store, job.id, criteria=CRITERIA, successor_ref="lane-b", gate_id="gate-imx"
    )
    assert gate["status"] == "armed"
    assert gate["pre_registered_ts"]
    assert "decision_gate_registered" in _journal_types(store, job.id)
    with pytest.raises(ValueError, match="already registered"):
        register_decision_gate(
            store, job.id, criteria=CRITERIA, successor_ref="lane-b", gate_id="gate-imx"
        )


def test_criteria_need_the_full_evidence_window(tmp_path: Path) -> None:
    met, measured = evaluate_gate_criteria(
        CRITERIA, {"trades": {"closed_count": 12, "win_rate": 0.1, "net_pnl": -50}}
    )
    assert met is False and measured["closed_trades"] == 12
    met, _ = evaluate_gate_criteria(CRITERIA, FAILING_SUMMARY)
    assert met is True
    # A recovered line does not trip.
    met, _ = evaluate_gate_criteria(
        CRITERIA,
        {"trades": {"closed_count": 25, "win_rate": 0.55, "net_pnl": 40.0}},
    )
    assert met is False


# ── paper auto-resolution ────────────────────────────────────────────────


def test_paper_job_auto_resolves_when_criteria_met(
    tmp_path: Path, compiled: list[str]
) -> None:
    store, job = _store(tmp_path)
    register_decision_gate(
        store, job.id, criteria=CRITERIA, successor_ref="lane-b", gate_id="gate-imx"
    )
    store.write_json(job.id, "results/forward/summary.json", FAILING_SUMMARY)

    events = evaluate_decision_gates(store, job)

    assert [event["type"] for event in events] == ["gate_auto_resolved"]
    event = events[0]
    assert event["measured"]["closed_trades"] == 20
    assert event["undo"]["command"] == (
        f"wayfinder job decision-gate reopen {job.id} gate-imx"
    )
    # Incumbent retired: loop disabled + recompiled, workspace archived.
    assert store.load(job.id).script_loop.enabled is False
    assert compiled == [job.id]
    archived = store.job_dir(job.id) / event["archived_workspace"] / "workspace"
    assert (archived / "src" / "strategy.py").exists()
    # Active workspace left in place — reopen is non-destructive.
    assert (store.job_dir(job.id) / "workspace" / "src" / "strategy.py").exists()
    # Pivot note is operator-visible and attributed to gate-auto, not owner.
    agenda = (store.job_dir(job.id) / "research" / "agenda.md").read_text()
    assert "gate-auto" in agenda and "lane-b" in agenda
    gate = load_decision_gates(store, job.id)["gates"][0]
    assert gate["status"] == "resolved"
    assert gate["resolution"]["by"] == "gate-auto"
    # Resolved gates do not re-fire.
    assert evaluate_decision_gates(store, job) == []


def test_unmet_criteria_leave_the_gate_armed(
    tmp_path: Path, compiled: list[str]
) -> None:
    store, job = _store(tmp_path)
    register_decision_gate(
        store, job.id, criteria=CRITERIA, successor_ref="lane-b", gate_id="gate-imx"
    )
    store.write_json(
        job.id,
        "results/forward/summary.json",
        {"trades": {"closed_count": 5, "win_rate": 0.2, "net_pnl": -3}},
    )

    assert evaluate_decision_gates(store, job) == []
    assert load_decision_gates(store, job.id)["gates"][0]["status"] == "armed"
    assert store.load(job.id).script_loop.enabled is True
    assert compiled == []


# ── live jobs escalate instead ───────────────────────────────────────────


def test_live_capable_job_trips_to_needs_you(
    tmp_path: Path, compiled: list[str]
) -> None:
    store, job = _store(tmp_path, wallet_label="main")
    register_decision_gate(
        store, job.id, criteria=CRITERIA, successor_ref="lane-b", gate_id="gate-imx"
    )
    store.write_json(job.id, "results/forward/summary.json", FAILING_SUMMARY)

    events = evaluate_decision_gates(store, job)

    assert [event["type"] for event in events] == ["decision_gate_tripped"]
    assert store.load(job.id).script_loop.enabled is True  # nothing executed
    assert compiled == []
    gate = load_decision_gates(store, job.id)["gates"][0]
    assert gate["status"] == "tripped_needs_owner"
    needs_you = build_owner_attention(store, job.id)["needs_you"]
    assert [item["kind"] for item in needs_you] == ["decision_gate_tripped"]
    assert needs_you[0]["ref_id"] == "gate-imx"
    # A tripped gate does not re-trip on the next watchdog pass.
    assert evaluate_decision_gates(store, job) == []


# ── undo and manual resolution ───────────────────────────────────────────


def test_reopen_reverses_an_auto_resolution(
    tmp_path: Path, compiled: list[str]
) -> None:
    store, job = _store(tmp_path)
    register_decision_gate(
        store, job.id, criteria=CRITERIA, successor_ref="lane-b", gate_id="gate-imx"
    )
    store.write_json(job.id, "results/forward/summary.json", FAILING_SUMMARY)
    evaluate_decision_gates(store, job)
    assert store.load(job.id).script_loop.enabled is False

    gate = reopen_decision_gate(store, job.id, "gate-imx", by="owner")

    assert gate["status"] == "reopened"
    assert store.load(job.id).script_loop.enabled is True
    assert compiled == [job.id, job.id]  # retire + reopen recompiles
    assert "decision_gate_reopened" in _journal_types(store, job.id)
    # Reopened is terminal until re-registered: criteria still met, no re-trip.
    assert evaluate_decision_gates(store, store.load(job.id)) == []


def test_owner_resolves_a_tripped_gate(tmp_path: Path, compiled: list[str]) -> None:
    store, job = _store(tmp_path, wallet_label="main")
    register_decision_gate(
        store, job.id, criteria=CRITERIA, successor_ref="lane-b", gate_id="gate-imx"
    )
    store.write_json(job.id, "results/forward/summary.json", FAILING_SUMMARY)
    evaluate_decision_gates(store, job)

    gate = resolve_decision_gate(store, job.id, "gate-imx", by="owner", note="agreed")

    assert gate["status"] == "resolved"
    assert gate["resolution"]["by"] == "owner"
    assert "decision_gate_resolved" in _journal_types(store, job.id)
    # Acknowledged, not executed: the loop keeps running.
    assert store.load(job.id).script_loop.enabled is True
    assert compiled == []


# ── watchdog integration ─────────────────────────────────────────────────


def test_watchdog_pass_runs_the_gate_check(
    tmp_path: Path, compiled: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from wayfinder_paths.jobs import watchdog as watchdog_mod

    class FakeBridge:
        def __init__(self, *, repo_root=None) -> None:
            self.repo_root = repo_root

        def job_states(self) -> dict:
            return {}

    monkeypatch.setattr(watchdog_mod, "RunnerBridge", FakeBridge)
    store, job = _store(tmp_path)
    register_decision_gate(
        store, job.id, criteria=CRITERIA, successor_ref="lane-b", gate_id="gate-imx"
    )
    store.write_json(job.id, "results/forward/summary.json", FAILING_SUMMARY)

    result = watchdog_mod.recover_stalled_applications(store=store)

    gate_events = [
        event
        for event in result["recovered"]
        if event.get("type") == "gate_auto_resolved"
    ]
    assert len(gate_events) == 1
    assert load_decision_gates(store, job.id)["gates"][0]["status"] == "resolved"
