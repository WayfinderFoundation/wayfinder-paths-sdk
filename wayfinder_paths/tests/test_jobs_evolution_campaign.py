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
    _write_timeseries_prefix,
    campaign_due,
    campaign_prompt_block,
    campaign_status,
    evolution_compute_window_open,
    finalize_campaign,
    maybe_start_campaign,
    prepare_candidate,
    resolve_candidate_bundle,
    start_campaign,
)
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.starter_casebook import (
    MAX_PROMPT_CASES,
    load_starter_casebook,
)
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.worker import _queue_evolution_worker


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
    block = campaign_prompt_block(store, job_id, now=now)
    assert block is not None
    assert len(block["cases"]) <= MAX_PROMPT_CASES
    assert block["constraints"] == {
        "paper_only": True,
        "live_requires_owner": True,
        "candidate_inputs_frozen_at_campaign_start": True,
        "finalist_requires_24h_forward_proposal": True,
    }
    assert len(load_starter_casebook()) > len(block["cases"])


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (0, 59, True),
        (1, 0, False),
        (3, 59, False),
        (4, 0, True),
        (6, 0, False),
        (9, 59, False),
        (10, 0, True),
    ],
)
def test_evolution_pauses_during_deepseek_peak_pricing(
    tmp_path, hour: int, minute: int, expected: bool
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    now = datetime(2026, 8, 25, hour, minute, tzinfo=UTC)
    assert evolution_compute_window_open(store, job_id, now=now) is expected


def test_campaign_start_reserves_full_window_before_peak_pricing(tmp_path) -> None:
    safe_store, safe_id = _job(tmp_path / "safe", "majors-5m-lab")
    safe = datetime(2026, 8, 25, 19, tzinfo=UTC)
    assert evolution_compute_window_open(
        safe_store, safe_id, now=safe, reserve_campaign=True
    )
    assert campaign_due(safe_store, safe_id, now=safe)

    blocked_store, blocked_id = _job(tmp_path / "blocked", "majors-5m-lab")
    blocked = datetime(2026, 8, 25, 21, tzinfo=UTC)
    assert not evolution_compute_window_open(
        blocked_store, blocked_id, now=blocked, reserve_campaign=True
    )
    assert not campaign_due(blocked_store, blocked_id, now=blocked)
    assert maybe_start_campaign(blocked_store, blocked_id, now=blocked) is None
    with pytest.raises(ValueError, match="peak pricing"):
        start_campaign(blocked_store, blocked_id, now=blocked)


def test_active_campaign_prompt_is_suppressed_during_peak_pricing(tmp_path) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    store.write_json(
        job_id,
        "state/evolution_campaign.json",
        {"campaign_id": "campaign-1", "status": "active"},
    )
    assert campaign_prompt_block(
        store, job_id, now=datetime(2026, 8, 25, 2, tzinfo=UTC)
    ) == {
        "status": "blocked",
        "reason": "evolution worker paused during peak model pricing",
    }


def test_prepare_candidate_isolated_lineage_and_mutation_budget(tmp_path) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    start_campaign(store, job_id, now=datetime(2026, 8, 25, 12, tzinfo=UTC))
    active_script = store.job_dir(job_id) / "workspace/src/strategy.py"
    active_script.write_text("raise RuntimeError('post-freeze change')\n")
    first = prepare_candidate(
        store,
        job_id,
        family="breakout",
        summary="structural breadth and exit mutation",
        mutation_kind="parameter",  # slot 1 is forced structural by budget
        now=datetime(2026, 8, 25, 13, tzinfo=UTC),
    )
    assert first["mutation_kind"] == "structural"
    root = store.job_dir(job_id)
    bundle = root / first["bundle"]
    assert bundle != root
    assert (bundle / "workspace" / "src" / "strategy.py").exists()
    assert (
        "post-freeze change"
        not in (bundle / "workspace" / "src" / "strategy.py").read_text()
    )
    assert (bundle / "job.yaml").exists()
    archive = quality_diversity_snapshot(store, job_id)
    assert archive == {}  # generated candidate has no behavior until evaluated

    second = prepare_candidate(
        store,
        job_id,
        family="breakout",
        summary="second structural attempt",
        now=datetime(2026, 8, 25, 13, tzinfo=UTC),
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
        now=datetime(2026, 8, 25, 13, tzinfo=UTC),
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
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    state = start_campaign(store, job_id, now=now)
    state["status"] = "complete"
    store.write_json(job_id, "state/evolution_campaign.json", state)
    with pytest.raises(ValueError, match="start interval"):
        start_campaign(store, job_id, now=now)


def test_finalize_enforces_stage_budgets_and_isolates_candidate_failure(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    started = datetime(2026, 8, 25, 12, tzinfo=UTC)
    start_campaign(store, job_id, now=started)
    candidates = []
    for index in range(6):
        candidate = prepare_candidate(
            store,
            job_id,
            family=f"family-{index}",
            summary=f"candidate {index}",
            now=started.replace(hour=13),
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
    # Resume after one full-dev result was durably written: it remains proposal
    # eligible and does not consume another development evaluation.
    state["candidates"][0]["status"] = "dev_frontier"
    state["counts"]["quick_evaluated"] = 6
    state["counts"]["full_dev"] = 1
    store.write_json(job_id, "state/evolution_campaign.json", state)

    calls = {"dev": 0, "gate": 0, "proposal": 0}

    def fake_dev(store, job_id, candidate, *, tune):
        calls["dev"] += 1
        if calls["dev"] == 2:
            raise RuntimeError("bad candidate")
        return {"status": "dev_frontier", "evidence": "passed"}

    def fake_gate(*args, **kwargs):
        calls["gate"] += 1
        return {
            "status": "ok",
            "ready": True,
            "objective": {"candidate": {"trade_count": 12}},
            "sim_wall_seconds": 1.0,
        }

    def fake_stage(*args, **kwargs):
        calls["proposal"] += 1
        return {
            "status": "queued",
            "candidate_id": kwargs["candidate_id"],
            "revision": kwargs["revision"],
        }

    monkeypatch.setattr("wayfinder_paths.jobs.evolution_campaign._full_dev", fake_dev)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign.evaluate_economic_gate", fake_gate
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.paper_experiment.stage_paper_proposal", fake_stage
    )
    result = finalize_campaign(store, job_id)
    assert result["status"] == "complete"
    assert result["counts"] == {
        "generated": 6,
        "quick_evaluated": 6,
        "full_dev": 4,
        "proposed": 1,
    }
    assert calls == {"dev": 3, "gate": 1, "proposal": 1}
    assert any(item["status"] == "invalid" for item in result["candidates"])


def test_finalize_rejects_economically_unready_finalist(tmp_path, monkeypatch) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    started = datetime(2026, 8, 25, 12, tzinfo=UTC)
    start_campaign(store, job_id, now=started)
    prepare_candidate(
        store,
        job_id,
        family="noise",
        summary="candidate with available but negative paired evidence",
        now=started.replace(hour=13),
    )
    state = campaign_status(store, job_id)
    state["candidates"][0].update(
        {
            "status": "quick_complete",
            "objective": {
                "net_log_growth": 0.1,
                "downside_deviation": 0.01,
                "tail_loss": 0.01,
                "max_drawdown_pct": 0.05,
            },
        }
    )
    state["counts"]["quick_evaluated"] = 1
    store.write_json(job_id, "state/evolution_campaign.json", state)

    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign._full_dev",
        lambda *args, **kwargs: {"status": "dev_frontier", "evidence": "passed"},
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign.evaluate_economic_gate",
        lambda *args, **kwargs: {
            "status": "ok",
            "ready": False,
            "reasons": ["paired utility estimate not > 0"],
        },
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.paper_experiment.stage_paper_proposal",
        lambda *args, **kwargs: pytest.fail("unready finalist reached paper staging"),
    )

    result = finalize_campaign(store, job_id)

    candidate = result["candidates"][0]
    assert candidate["status"] == "proposal_rejected"
    assert candidate["evidence"] == "paired utility estimate not > 0"


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


def test_candidate_bundle_is_confined_to_its_exact_campaign_slot(tmp_path) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    state = start_campaign(store, job_id, now=datetime(2026, 8, 25, 12, tzinfo=UTC))
    candidate = prepare_candidate(
        store,
        job_id,
        family="breakout",
        summary="containment fixture",
        now=datetime(2026, 8, 25, 13, tzinfo=UTC),
    )
    expected = (store.job_dir(job_id) / candidate["bundle"]).resolve()
    assert resolve_candidate_bundle(store, job_id, candidate) == expected

    invalid = [
        {**candidate, "bundle": ""},
        {**candidate, "bundle": str(expected)},
        {**candidate, "bundle": "../job.yaml"},
        {
            **candidate,
            "bundle": (
                "research/evolution/campaigns/sibling/candidates/"
                f"{candidate['candidate_id']}"
            ),
        },
        {**candidate, "candidate_id": "different-name"},
        {**candidate, "campaign_id": "sibling"},
    ]
    for row in invalid:
        with pytest.raises(ValueError):
            resolve_candidate_bundle(store, job_id, row)

    link_id = "symlink-candidate"
    link = expected.parent / link_id
    link.symlink_to(store.job_dir(job_id), target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        resolve_candidate_bundle(
            store,
            job_id,
            {
                **candidate,
                "candidate_id": link_id,
                "bundle": str(link.relative_to(store.job_dir(job_id))),
            },
        )

    manifest = json.loads(
        (store.job_dir(job_id) / state["manifest"]).read_text(encoding="utf-8")
    )
    serialized = json.dumps(manifest)
    assert str(tmp_path / "audit") not in serialized
    assert "allocation.json" not in serialized
    assert not (tmp_path / "audit" / job_id / "evolution").exists()


def test_jsonl_features_are_frozen_at_the_campaign_cutoff(tmp_path) -> None:
    source = tmp_path / "features.jsonl"
    destination = tmp_path / "snapshot" / "features.jsonl"
    source.write_text(
        "\n".join(
            json.dumps({"timestamp": f"2026-08-25T0{hour}:00:00Z", "value": hour})
            for hour in range(3)
        )
        + "\n",
        encoding="utf-8",
    )

    assert _write_timeseries_prefix(
        source,
        destination,
        cutoff=datetime(2026, 8, 25, 1, tzinfo=UTC),
    )
    rows = [json.loads(line) for line in destination.read_text().splitlines()]
    assert [row["value"] for row in rows] == [0, 1]


def test_evolution_uses_a_dedicated_long_lived_session(tmp_path, monkeypatch) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    store.write_json(
        job_id,
        "state/evolution_campaign.json",
        {"campaign_id": "campaign-1", "status": "active"},
    )

    class FakeClient:
        def __init__(self) -> None:
            self.prompts = []

        def healthy(self):
            return True

        def find_child_session(self, *, parent_id, title):
            assert title == f"job/{job_id}/evolution"
            return "evolution-session"

        def create_session(self, **kwargs):
            raise AssertionError("the stable evolution session should be reused")

        def prompt_async(self, *, session_id, text, agent):
            self.prompts.append((session_id, text, agent))
            return True

    client = FakeClient()
    monkeypatch.setattr("wayfinder_paths.jobs.worker.OPENCODE_CLIENT", client)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign.campaign_prompt_block",
        lambda store, job_id: {
            "campaign_id": "campaign-1",
            "next_action": "prepare the next candidate",
        },
    )

    result = _queue_evolution_worker(store, job_id)

    assert result == {"queued": True, "session_id": "evolution-session"}
    assert len(client.prompts) == 1
    assert "immutable 24-hour forward paper proposal" in client.prompts[0][1]
    session = store.read_json(job_id, "reports/evolution/session.json")
    assert session["session_id"] == "evolution-session"
