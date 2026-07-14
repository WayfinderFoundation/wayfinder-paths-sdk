"""`apply_script_mode` is the ONE compiler-safe way to flip paper<->live.

It edits `job.yaml` (`script_loop.mode`), recompiles — re-baking
`WAYFINDER_JOB_MODE` into the runner env — and re-syncs. Going live is gated on
`evaluate_live_gate` (`live_ready`) plus a declared `wallet_label`; reverting to
paper is always allowed. Hand-patching the runner env is what these guards exist
to make unnecessary (see test_mcp_runner_tool for the env rejection).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wayfinder_paths.jobs import sync as sync_mod
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import apply_script_mode


class _CaptureBridge:
    """Fake RunnerBridge capturing the env baked into each compiled script job."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *, repo_root=None):  # constructed as RunnerBridge(repo_root=…)
        return self

    def ensure_started(self):
        return {"ok": True}

    def add_or_update_script_job(self, **kwargs):
        self.calls.append(kwargs)
        return {"ok": True, "result": {"name": kwargs["name"]}}

    # Used by sync_all_jobs -> snapshot_job; empty == runner down (graceful).
    def job_states(self) -> dict:
        return {}


def _baked_script_mode(bridge: _CaptureBridge) -> str | None:
    for call in bridge.calls:
        if call["name"].endswith("-script"):
            return call["env"]["WAYFINDER_JOB_MODE"]
    return None


def _job(tmp_path: Path, *, mode: str, wallet: str | None = None) -> JobStore:
    store = JobStore(repo_root=tmp_path)
    script = tmp_path / ".wayfinder/jobs/carry/workspace/src/loop.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('ok')\n", encoding="utf-8")
    job = WayfinderJob.new("carry", script=str(script), interval_seconds=60)
    job.script_loop.mode = mode
    if wallet is not None:
        job.execution_params["wallet_label"] = wallet
    store.create_job(job)
    return store


def _patch_bridges(monkeypatch) -> _CaptureBridge:
    bridge = _CaptureBridge()
    # compiler builds the runner env; sync snapshots the (absent) runner.
    monkeypatch.setattr("wayfinder_paths.jobs.compiler.RunnerBridge", bridge)
    monkeypatch.setattr(sync_mod, "RunnerBridge", bridge)
    return bridge


def test_revert_to_paper_recompiles_env(tmp_path, monkeypatch) -> None:
    store = _job(tmp_path, mode="live", wallet="carry")
    bridge = _patch_bridges(monkeypatch)

    result = apply_script_mode("carry", "paper", store=store)

    assert result["mode"] == "paper"
    assert store.load("carry").script_loop.mode == "paper"
    assert _baked_script_mode(bridge) == "paper"  # env re-baked to paper


def test_go_live_succeeds_when_gated(tmp_path, monkeypatch) -> None:
    store = _job(tmp_path, mode="paper", wallet="carry")
    bridge = _patch_bridges(monkeypatch)
    monkeypatch.setattr(
        sync_mod,
        "evaluate_live_gate",
        lambda *a, **k: {"live_ready": True, "reasons": []},
    )

    result = apply_script_mode("carry", "live", store=store)

    assert result["mode"] == "live"
    assert store.load("carry").script_loop.mode == "live"
    assert _baked_script_mode(bridge) == "live"  # env re-baked to live


def test_go_live_refused_without_wallet_label(tmp_path, monkeypatch) -> None:
    store = _job(tmp_path, mode="paper")  # no wallet_label
    _patch_bridges(monkeypatch)
    # Gate would pass — the wallet check must fire first and block regardless.
    monkeypatch.setattr(
        sync_mod,
        "evaluate_live_gate",
        lambda *a, **k: {"live_ready": True, "reasons": []},
    )

    with pytest.raises(ValueError, match="wallet_label"):
        apply_script_mode("carry", "live", store=store)

    assert store.load("carry").script_loop.mode == "paper"  # nothing written


def test_go_live_refused_when_gate_not_ready(tmp_path, monkeypatch) -> None:
    store = _job(tmp_path, mode="paper", wallet="carry")
    _patch_bridges(monkeypatch)
    monkeypatch.setattr(
        sync_mod,
        "evaluate_live_gate",
        lambda *a, **k: {"live_ready": False, "reasons": ["no backtest artifact"]},
    )

    with pytest.raises(ValueError, match="no backtest artifact"):
        apply_script_mode("carry", "live", store=store)

    assert store.load("carry").script_loop.mode == "paper"  # nothing written


def test_unknown_mode_rejected(tmp_path, monkeypatch) -> None:
    store = _job(tmp_path, mode="paper", wallet="carry")
    _patch_bridges(monkeypatch)

    with pytest.raises(ValueError, match="script mode must be"):
        apply_script_mode("carry", "halted", store=store)
