"""Operator-owned mode: every flip records WHO made it, and leaving live
with open engine positions is refused (a live->paper flip resets the engine
and orphans real venue positions — observed live: a reverted canary left a
HYPE short unmanaged, stopless, for 26 hours)."""

from __future__ import annotations

import json

import pytest
import yaml

from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import OPERATOR_STATE_PATH, apply_script_mode


def _job(tmp_path, *, mode: str = "paper") -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "op-demo", script="workspace/src/strategy.py", agent_mode="intervene"
    )
    job.script_loop.mode = mode
    job.execution_params["wallet_label"] = "funding-carry-basket"
    store.save(job)
    src = store.job_dir(job.id) / "workspace" / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "strategy.py").write_text("def build_strategy():\n    pass\n")
    return store, job.id


def _quiet_pipeline(monkeypatch) -> None:
    monkeypatch.setattr(
        "wayfinder_paths.jobs.sync.JobCompiler",
        lambda store: type("C", (), {"compile": lambda self, job: {"ok": True}})(),
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.sync.sync_all_jobs", lambda **kwargs: None
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.sync.evaluate_live_gate",
        lambda job_id, store=None: {"live_ready": True, "reasons": []},
    )


def test_flip_records_operator_state_and_journal(tmp_path, monkeypatch) -> None:
    store, job_id = _job(tmp_path)
    _quiet_pipeline(monkeypatch)

    result = apply_script_mode(job_id, "live", store=store)
    assert result["set_by"] == "owner"
    operator = store.read_json(job_id, OPERATOR_STATE_PATH)
    assert operator["script_mode"]["mode"] == "live"
    assert operator["script_mode"]["set_by"] == "owner"
    assert operator["script_mode"]["forced"] is False

    journal = (store.job_dir(job_id) / "journal.jsonl").read_text()
    row = json.loads(journal.strip().splitlines()[-1])
    assert row["type"] == "script_mode_set"
    assert row["set_by"] == "owner"

    # An agent-made flip is recorded as the agent's — provenance, not a block.
    apply_script_mode(job_id, "paper", store=store, set_by="agent")
    operator = store.read_json(job_id, OPERATOR_STATE_PATH)
    assert operator["script_mode"]["set_by"] == "agent"


def test_leaving_live_with_open_positions_is_refused(tmp_path, monkeypatch) -> None:
    store, job_id = _job(tmp_path, mode="live")
    _quiet_pipeline(monkeypatch)
    store.write_json(
        job_id,
        "state/engine_state.json",
        {"mode": "live", "positions": {"HYPE": {"side": "short", "size": 0.48}}},
    )

    with pytest.raises(ValueError, match="orphans them"):
        apply_script_mode(job_id, "paper", store=store)
    # Nothing written: mode unchanged, no operator record of the refused flip.
    job_yaml = yaml.safe_load((store.job_dir(job_id) / "job.yaml").read_text())
    assert job_yaml["script_loop"]["mode"] == "live"

    # force=True is the explicit orphaning escape hatch, and says so on record.
    result = apply_script_mode(job_id, "paper", store=store, force=True)
    assert result["mode"] == "paper"
    operator = store.read_json(job_id, OPERATOR_STATE_PATH)
    assert operator["script_mode"]["forced"] is True


def test_leaving_live_flat_needs_no_force(tmp_path, monkeypatch) -> None:
    store, job_id = _job(tmp_path, mode="live")
    _quiet_pipeline(monkeypatch)
    store.write_json(
        job_id, "state/engine_state.json", {"mode": "live", "positions": {}}
    )
    result = apply_script_mode(job_id, "paper", store=store)
    assert result["mode"] == "paper"


def test_worker_stable_prompt_carries_operator_record(tmp_path, monkeypatch) -> None:
    from wayfinder_paths.jobs.worker import _operator_block

    store, job_id = _job(tmp_path)
    assert _operator_block(store, job_id) == {}

    _quiet_pipeline(monkeypatch)
    apply_script_mode(job_id, "live", store=store)
    block = _operator_block(store, job_id)
    assert block["script_mode"]["set_by"] == "owner"
    assert "AUTHORIZED" in block["_basis"]
