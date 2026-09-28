"""Catalogue retirement must not mutate existing jobs or hide research sources."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from wayfinder_paths.jobs import evolution_campaign, starters
from wayfinder_paths.jobs.bundles import copy_job_bundle
from wayfinder_paths.jobs.store import JobStore


def skip_fetch(store: JobStore, job_id: str, *, days: int = 120) -> dict[str, bool]:
    return {"spawned": False}


def test_retired_starter_is_resolvable_but_cannot_be_newly_launched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original = starters.get_starter("mixed-volume-capitulation-1h")
    retired = replace(original, selectable=False)
    monkeypatch.setattr(starters, "STARTER_DEFINITIONS", (retired,))
    assert starters.starter_catalog() == []
    assert starters.get_starter(original.id) == retired
    assert retired.to_dict()["selectable"] is False
    store = JobStore(repo_root=tmp_path)
    with pytest.raises(ValueError, match="retired from new selection"):
        starters.create_starter_job(original.id, store=store, compile_job=False)
    assert not (store.job_dir(original.id) / "job.yaml").exists()


def test_retirement_preserves_reopen_and_recorded_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original = starters.get_starter("mixed-volume-capitulation-1h")
    monkeypatch.setattr(starters, "STARTER_DEFINITIONS", (original,))
    monkeypatch.setattr(
        starters,
        "_spawn_starter_dataset_fetch",
        skip_fetch,
    )
    store = JobStore(repo_root=tmp_path)
    starters.create_starter_job(original.id, store=store, compile_job=False)
    root = store.job_dir(original.id)
    tracked_paths = (
        root / "job.yaml",
        root / "workspace/src/strategy.py",
        root / "results/backtest/starter_evidence.json",
    )
    before = {str(path): path.read_bytes() for path in tracked_paths}
    evidence = store.read_json(original.id, "results/backtest/starter_evidence.json")
    monkeypatch.setattr(
        starters,
        "STARTER_DEFINITIONS",
        (replace(original, selectable=False, name="Changed catalogue name"),),
    )
    reopened = starters.create_starter_job(original.id, store=store, compile_job=False)
    assert reopened["created"] is False
    assert reopened["starter"] == evidence
    assert {str(path): path.read_bytes() for path in tracked_paths} == before


def test_revision_specific_leverage_evidence_takes_precedence() -> None:
    original = starters.get_starter("mixed-volume-capitulation-1h")
    updated_sweep: dict[str, Any] = {
        "results": [{"leverage": 1, "net_return": 0.01}],
        "revision": "new",
    }
    revised = replace(
        original,
        research_evidence={
            **original.research_evidence,
            "jobs_v1_leverage_sweep": updated_sweep,
        },
    )
    result = revised.to_dict()["research_evidence"]["jobs_v1_leverage_sweep"]
    assert result == updated_sweep
    result["results"][0]["net_return"] = 99
    assert updated_sweep["results"][0]["net_return"] == 0.01


def test_missing_new_leverage_evidence_does_not_resurrect_old_sweep() -> None:
    original = starters.get_starter("mixed-volume-capitulation-1h")
    revised = replace(original, research_evidence={"jobs_v1_leverage_sweep": {}})
    assert revised.to_dict()["research_evidence"]["jobs_v1_leverage_sweep"] == {}


def test_evolution_snapshots_only_selectable_starters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    active = starters.get_starter("mixed-volume-capitulation-1h")
    retired = replace(starters.get_starter("mixed-rsi-snapback-1h"), selectable=False)
    monkeypatch.setattr(evolution_campaign, "STARTER_DEFINITIONS", (active, retired))
    monkeypatch.setattr(starters, "_spawn_starter_dataset_fetch", skip_fetch)
    store = JobStore(repo_root=tmp_path)
    starters.create_starter_job(active.id, store=store, compile_job=False)
    campaign_root = tmp_path / "campaign"
    copy_job_bundle(store.job_dir(active.id), campaign_root / "source")
    snapshots = evolution_campaign._snapshot_starter_seeds(
        store, active.id, campaign_root, dataset_symbols=active.symbols
    )
    assert [row["starter_id"] for row in snapshots] == [active.id]
    assert snapshots[0]["research_evidence_reset"] is True
    assert not (campaign_root / "starters" / retired.id).exists()
