from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from wayfinder_paths.jobs.archive import (
    behavior_cell,
    quality_diversity_snapshot,
    record_candidate,
)
from wayfinder_paths.jobs.evolution_campaign import (
    _parent_source,
    _same_family_nonwins,
    campaign_prompt_block,
    campaign_status,
    finalize_campaign,
    maybe_start_campaign,
    prepare_candidate,
    start_campaign,
)
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.starter_casebook import (
    MAX_PROMPT_CASES,
    load_starter_casebook,
)
from wayfinder_paths.jobs.store import JobStore


def _job(tmp_path, job_id: str) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        job_id,
        name="Majors momentum lab",
        goal="find robust momentum and factor strategies",
        script="workspace/src/strategy.py",
        agent_mode="intervene",
        execution_contract="jobs_v1",
    )
    store.save(job)
    root = store.job_dir(job.id)
    (root / "workspace" / "src" / "strategy.py").write_text(
        "def decide(ctx):\n    return []\n", encoding="utf-8"
    )
    bars = root / "results" / "backtest" / "input_bars.json"
    bars.parent.mkdir(parents=True, exist_ok=True)
    bars.write_text('{"metadata":{"days":120},"bars":[]}\n', encoding="utf-8")
    return store, job.id


def test_rollout_is_gated_and_campaign_context_is_bounded(tmp_path) -> None:
    other_store, other_id = _job(tmp_path / "other", "other-job")
    assert maybe_start_campaign(other_store, other_id) is None

    store, job_id = _job(tmp_path / "target", "majors-5m-lab")
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    state = start_campaign(store, job_id, now=now)
    assert state["status"] == "active"
    assert state["counts"]["generated"] == 0
    assert campaign_status(store, job_id)["campaign_id"] == state["campaign_id"]
    root = store.job_dir(job_id)
    manifest_path = root / state["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    snapshot = manifest_path.parent / manifest["dataset"]["path"]
    original = snapshot.read_text(encoding="utf-8")
    (root / "results" / "backtest" / "input_bars.json").write_text(
        "[]\n", encoding="utf-8"
    )
    assert snapshot.read_text(encoding="utf-8") == original
    assert manifest["forward_context_cutoff"] == now.isoformat()
    block = campaign_prompt_block(store, job_id)
    assert block is not None
    assert len(block["cases"]) <= MAX_PROMPT_CASES
    assert block["constraints"] == {
        "paper_only": True,
        "live_requires_owner": True,
        "no_audit_access_before_finalists": True,
    }
    assert len(load_starter_casebook()) > len(block["cases"])


def test_prepare_candidate_isolated_lineage_and_mutation_budget(tmp_path) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    start_campaign(store, job_id, now=datetime(2026, 8, 25, tzinfo=UTC))
    first = prepare_candidate(
        store,
        job_id,
        family="breakout",
        summary="structural breadth and exit mutation",
        mutation_kind="parameter",  # slot 1 is forced structural by budget
        now=datetime(2026, 8, 25, 1, tzinfo=UTC),
    )
    assert first["mutation_kind"] == "structural"
    root = store.job_dir(job_id)
    bundle = root / first["bundle"]
    assert bundle != root
    assert (bundle / "workspace" / "src" / "strategy.py").exists()
    assert (bundle / "job.yaml").exists()
    archive = quality_diversity_snapshot(store, job_id)
    assert archive == {}  # generated candidate has no behavior until evaluated

    second = prepare_candidate(
        store,
        job_id,
        family="breakout",
        summary="second structural attempt",
        now=datetime(2026, 8, 25, 1, tzinfo=UTC),
    )
    state = campaign_status(store, job_id)
    state["candidates"][0]["status"] = "low_fidelity_rejected"
    state["candidates"][1]["status"] = "invalid"
    store.write_json(job_id, "state/evolution_campaign.json", state)
    third = prepare_candidate(
        store,
        job_id,
        family="breakout",
        summary="stuck rule jumps basin",
        now=datetime(2026, 8, 25, 1, tzinfo=UTC),
    )
    assert second["parent_source"] == "qd_elite"
    assert third["forced_jump"] is True
    assert third["parent_source"] == "de_novo"

    state["candidates"].append({"family": "breakout", "status": "dev_frontier"})
    assert _same_family_nonwins(state, "breakout", 2) is False

    mix = {"incumbent": 0.3, "qd_elite": 0.3, "crossover": 0.2, "de_novo": 0.2}
    sources = [_parent_source(slot, mix) for slot in range(1, 13)]
    assert {source: sources.count(source) for source in mix} == {
        "incumbent": 4,
        "qd_elite": 4,
        "crossover": 2,
        "de_novo": 2,
    }


def test_campaign_cooldown_is_enforced(tmp_path) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    now = datetime(2026, 8, 25, tzinfo=UTC)
    state = start_campaign(store, job_id, now=now)
    state["status"] = "complete"
    store.write_json(job_id, "state/evolution_campaign.json", state)
    with pytest.raises(ValueError, match="cooldown"):
        start_campaign(store, job_id, now=now)


def test_finalize_enforces_stage_budgets_and_isolates_candidate_failure(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    started = datetime(2026, 8, 25, tzinfo=UTC)
    start_campaign(store, job_id, now=started)
    candidates = []
    for index in range(6):
        candidate = prepare_candidate(
            store,
            job_id,
            family=f"family-{index}",
            summary=f"candidate {index}",
            now=started.replace(hour=1),
        )
        candidates.append(candidate)
    state = campaign_status(store, job_id)
    for index, candidate in enumerate(state["candidates"]):
        candidate["status"] = "quick_complete"
        candidate["objective"] = {
            "net_log_growth": 1.0 - index / 10,
            "downside_deviation": 0.01,
            "tail_loss": 0.01,
            "max_drawdown_pct": 0.05,
        }
    # Resume after one full-dev result was durably written: it remains audit
    # eligible and does not consume another dev evaluation.
    state["candidates"][0]["status"] = "dev_frontier"
    state["counts"]["quick_evaluated"] = 6
    state["counts"]["full_dev"] = 1
    store.write_json(job_id, "state/evolution_campaign.json", state)

    calls = {"dev": 0, "audit": 0}

    def fake_dev(store, job_id, candidate, *, tune):
        calls["dev"] += 1
        if calls["dev"] == 2:
            raise RuntimeError("bad candidate")
        return {"status": "dev_frontier", "evidence": "passed"}

    def fake_audit(store, job_id, state, candidate):
        calls["audit"] += 1
        return {"status": "paper_probation", "evidence": "passed audit"}

    monkeypatch.setattr("wayfinder_paths.jobs.evolution_campaign._full_dev", fake_dev)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign._sealed_audit", fake_audit
    )
    result = finalize_campaign(store, job_id)
    assert result["status"] == "complete"
    assert result["counts"] == {
        "generated": 6,
        "quick_evaluated": 6,
        "full_dev": 4,
        "audited": 2,
    }
    assert calls == {"dev": 3, "audit": 2}
    assert any(item["status"] == "invalid" for item in result["candidates"])


def test_quality_diversity_keeps_two_non_dominated_per_cell(tmp_path) -> None:
    store, job_id = _job(tmp_path, "archive-job")
    behavior = {
        "direction_bias": 0.8,
        "median_hold_bars": 4,
        "trades_per_asset_30d": 12,
    }
    assert behavior_cell(behavior) == "long/fast/regular"
    for index, growth in enumerate((0.1, 0.2, 0.3)):
        record_candidate(
            store,
            job_id,
            candidate_id=f"candidate-{index}",
            family="momentum",
            summary="qd",
            status="dev_frontier",
            objective={
                "net_log_growth": growth,
                "downside_deviation": 0.01,
                "tail_loss": 0.01,
                "max_drawdown_pct": 0.05,
            },
            behavior=behavior,
        )
    snapshot = quality_diversity_snapshot(store, job_id)
    assert [row["candidate_id"] for row in snapshot["long/fast/regular"]] == [
        "candidate-2"
    ]
