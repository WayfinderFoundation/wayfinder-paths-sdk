from __future__ import annotations

import io
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml

from wayfinder_paths.jobs.archive import (
    behavior_cell,
    quality_diversity_snapshot,
    record_candidate,
)
from wayfinder_paths.jobs.compute_lock import ComputeLockBusy
from wayfinder_paths.jobs.evolution_campaign import (
    _isolated_full_dev,
    _parent_source,
    _same_family_nonwins,
    _write_timeseries_prefix,
    campaign_due,
    campaign_prompt_block,
    campaign_status,
    evaluate_candidate,
    evolution_compute_window_open,
    finalize_campaign,
    maybe_start_campaign,
    prepare_candidate,
    resolve_candidate_bundle,
    start_campaign,
)
from wayfinder_paths.jobs.execution.op_process import op_runner_command
from wayfinder_paths.jobs.execution.primitives import DEFAULT_WARMUP_BARS
from wayfinder_paths.jobs.failures import TransientInfrastructureError
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.starter_casebook import (
    MAX_PROMPT_CASES,
    load_starter_casebook,
    select_starter_cases,
)
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.worker import (
    _queue_evolution_worker,
    nudge_evolution_session,
    retire_evolution_session,
    run_job_worker,
)


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
    assert block["job_id"] == job_id
    for required in (
        "wayfinder_core_jobs",
        'action="evolution_prepare"',
        f'job_id="{job_id}"',
        "family=",
        "summary=",
        'mutation_kind="structural"',
    ):
        assert required in block["next_action"]
    assert len(block["cases"]) <= MAX_PROMPT_CASES
    assert block["constraints"] == {
        "paper_only": True,
        "live_requires_owner": True,
        "candidate_inputs_frozen_at_campaign_start": True,
        "finalist_requires_24h_forward_proposal": True,
    }
    assert len(load_starter_casebook()) > len(block["cases"])


def test_campaign_start_defers_while_intervention_worker_is_busy(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign.require_evolution_launch_headroom",
        lambda: {"ready": True},
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.worker.job_worker_session_busy", lambda *args: True
    )

    with pytest.raises(TransientInfrastructureError, match="intervention worker"):
        start_campaign(store, job_id, now=datetime(2026, 8, 25, 19, tzinfo=UTC))

    assert campaign_status(store, job_id) == {}


def test_runner_command_declares_control_and_heavy_resource_tiers() -> None:
    assert "--resource-tier=control" in op_runner_command("evolution_prepare")
    assert "--resource-tier=control" in op_runner_command("evolution_start")
    assert "--resource-tier=heavy" in op_runner_command("evolution_evaluate")
    assert "--resource-tier=heavy" in op_runner_command("backtest_job")


def test_due_campaign_defers_without_spawning_when_burst_credit_is_low(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")

    class FakeClient:
        def healthy(self) -> bool:
            return True

    monkeypatch.setattr("wayfinder_paths.jobs.worker.OPENCODE_CLIENT", FakeClient())
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign.campaign_due", lambda *args: True
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.resource_envelope.evolution_launch_readiness",
        lambda: {
            "ready": False,
            "source": "governor",
            "reason": "burst reserve is low",
        },
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.background.spawn_detached_op",
        lambda *args, **kwargs: pytest.fail("campaign start should not spawn"),
    )

    result = _queue_evolution_worker(store, job_id)

    assert result == {
        "queued": False,
        "starting": False,
        "deferred": True,
        "reason": "burst reserve is low",
    }


def test_active_evolution_suppresses_the_parallel_intervention_llm(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    monkeypatch.setattr("wayfinder_paths.jobs.worker.JobStore", lambda: store)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.worker._queue_evolution_worker",
        lambda *args: {"queued": True, "session_id": "evolution-session"},
    )

    report = run_job_worker(job_id, mode="intervene")

    assert report["queued"] is False
    assert report["session_id"] == "evolution-session"
    assert "deferred while evolution owns the lane" in report["summary"]


def test_governor_diagnostic_error_does_not_wedge_the_intervention_lane(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    normal_report = {"status": "green", "summary": "normal lane reached"}
    monkeypatch.setattr("wayfinder_paths.jobs.worker.JobStore", lambda: store)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.worker._queue_evolution_worker",
        lambda *args: {"queued": False, "error": "governor state is stale"},
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.worker.maybe_skip_wake",
        lambda *args, **kwargs: normal_report,
    )

    assert run_job_worker(job_id, mode="intervene") is normal_report


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

    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign._isolated_full_dev", fake_dev
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign._isolated_economic_gate", fake_gate
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
        "wayfinder_paths.jobs.evolution_campaign._isolated_full_dev",
        lambda *args, **kwargs: {"status": "dev_frontier", "evidence": "passed"},
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign._isolated_economic_gate",
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


def test_finalize_releases_campaign_state_lock_during_heavy_compute(
    tmp_path, monkeypatch
) -> None:
    import wayfinder_paths.jobs.evolution_campaign as campaign_module

    store, job_id = _job(tmp_path, "majors-5m-lab")
    started = datetime(2026, 8, 25, 12, tzinfo=UTC)
    start_campaign(store, job_id, now=started)
    prepare_candidate(
        store,
        job_id,
        family="lock-probe",
        summary="prove compute does not pin campaign state",
        now=started + timedelta(hours=1),
    )
    state = campaign_status(store, job_id)
    state["candidates"][0]["status"] = "quick_complete"
    state["candidates"][0]["objective"] = {"net_log_growth": 1.0}
    state["counts"]["quick_evaluated"] = 1
    store.write_json(job_id, "state/evolution_campaign.json", state)

    real_lock = campaign_module.job_state_lock
    depth = 0

    @contextmanager
    def tracked_lock(*args, **kwargs):
        nonlocal depth
        tracks_campaign_state = kwargs.get("name") == "evolution_campaign"
        with real_lock(*args, **kwargs):
            depth += int(tracks_campaign_state)
            try:
                yield
            finally:
                depth -= int(tracks_campaign_state)

    def fake_dev(*args, **kwargs):
        assert depth == 0
        return {"status": "low_fidelity_rejected", "evidence": "probe complete"}

    monkeypatch.setattr(campaign_module, "job_state_lock", tracked_lock)
    monkeypatch.setattr(campaign_module, "_isolated_full_dev", fake_dev)

    result = finalize_campaign(store, job_id)

    assert result["status"] == "complete"
    assert result["counts"]["full_dev"] == 1


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


def test_next_action_pipelines_authoring_against_detached_evaluation(tmp_path) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    started = datetime(2026, 8, 25, 12, tzinfo=UTC)
    start_campaign(store, job_id, now=started)
    working = started.replace(hour=13)
    prepare_candidate(
        store, job_id, family="breakout", summary="pipeline probe", now=working
    )

    block = campaign_prompt_block(store, job_id, now=working)
    assert block is not None
    assert 'action="evolution_evaluate"' in block["next_action"]
    assert 'action="evolution_prepare"' in block["next_action"]
    assert f'job_id="{job_id}"' in block["next_action"]
    assert "background=true" in block["next_action"]
    assert "do not wait" in block["next_action"]

    for index in range(2):
        prepare_candidate(
            store, job_id, family="breakout", summary=f"queued {index}", now=working
        )
    drain = campaign_prompt_block(store, job_id, now=working)
    assert 'action="evolution_evaluate"' in drain["next_action"]
    assert 'action="evolution_prepare"' not in drain["next_action"]

    state = campaign_status(store, job_id)
    manifest = json.loads(
        (store.job_dir(job_id) / state["manifest"]).read_text(encoding="utf-8")
    )
    budget = int(manifest["policy"]["generated_programs"])
    for candidate in state["candidates"]:
        candidate["status"] = "quick_complete"
    while len(state["candidates"]) < budget:
        state["candidates"].append({"status": "quick_complete"})
    store.write_json(job_id, "state/evolution_campaign.json", state)
    done = campaign_prompt_block(store, job_id, now=working)
    assert 'action="evolution_finalize"' in done["next_action"]
    assert f'job_id="{job_id}"' in done["next_action"]
    assert "background=true" in done["next_action"]

    # Budget exhausted with a candidate still awaiting evaluation: drain only.
    state["candidates"][0]["status"] = "prepared"
    store.write_json(job_id, "state/evolution_campaign.json", state)
    exhausted = campaign_prompt_block(store, job_id, now=working)
    assert 'action="evolution_evaluate"' in exhausted["next_action"]
    assert 'action="evolution_prepare"' not in exhausted["next_action"]

    draining = campaign_prompt_block(
        store, job_id, now=datetime(2026, 8, 25, 15, 40, tzinfo=UTC)
    )
    assert draining == {
        "status": "blocked",
        "campaign_id": state["campaign_id"],
        "reason": "evolution campaign is draining before finalization",
    }

    elapsed = campaign_prompt_block(
        store, job_id, now=datetime(2026, 8, 25, 16, 30, tzinfo=UTC)
    )
    assert 'action="evolution_finalize"' in elapsed["next_action"]
    assert f'job_id="{job_id}"' in elapsed["next_action"]
    assert "background=true" in elapsed["next_action"]
    assert elapsed["deadline_elapsed"] is True


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

        def session_exists(self, session_id):
            return False

        def session_statuses(self):
            return {}

        def find_child_session(self, *, parent_id, title):
            assert title == f"job/{job_id}/evolution/campaign-1"
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
    assert "PAPER-ONLY evolution campaign" in client.prompts[0][1]
    assert "do not wait on detached evaluation" in client.prompts[0][1]
    assert client.prompts[0][2] == "wayfinder-evolution-worker"
    session = store.read_json(job_id, "reports/evolution/session.json")
    assert session["session_id"] == "evolution-session"


class _FakeEvolutionClient:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.prompts: list[tuple[str, str, str]] = []

    def healthy(self) -> bool:
        return True

    def session_exists(self, session_id: str) -> bool:
        return False

    def session_statuses(self) -> dict[str, Any]:
        return {}

    def find_child_session(self, *, parent_id: Any, title: str) -> str:
        assert title == f"job/{self.job_id}/evolution/campaign-1"
        return "evolution-session"

    def create_session(self, **kwargs: Any) -> str:
        raise AssertionError("the stable evolution session should be reused")

    def prompt_async(self, *, session_id: str, text: str, agent: str) -> bool:
        self.prompts.append((session_id, text, agent))
        return True


def test_op_completion_nudge_reuses_session_and_respects_kill_switch(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    store.write_json(
        job_id,
        "state/evolution_campaign.json",
        {"campaign_id": "campaign-1", "status": "active"},
    )
    client = _FakeEvolutionClient(job_id)
    monkeypatch.setattr("wayfinder_paths.jobs.worker.OPENCODE_CLIENT", client)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign.campaign_prompt_block",
        lambda store, job_id: {
            "campaign_id": "campaign-1",
            "next_action": "prepare the next candidate",
        },
    )

    result = nudge_evolution_session(store, job_id)

    assert result == {"queued": True, "session_id": "evolution-session"}
    assert len(client.prompts) == 1
    session = store.read_json(job_id, "reports/evolution/session.json")
    assert session["session_id"] == "evolution-session"
    latest = store.read_json(job_id, "reports/evolution/latest.json")
    assert latest["source"] == "op_completion"
    journal = store.job_dir(job_id) / "journal.jsonl"
    entry = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["type"] == "evolution_worker_wakeup"
    assert entry["source"] == "op_completion"

    # No nudge without an active campaign.
    store.write_json(
        job_id,
        "state/evolution_campaign.json",
        {"campaign_id": "campaign-1", "status": "complete"},
    )
    assert nudge_evolution_session(store, job_id) is None
    assert len(client.prompts) == 1

    # Kill-switch disables op-completion nudges entirely.
    store.write_json(
        job_id,
        "state/evolution_campaign.json",
        {"campaign_id": "campaign-1", "status": "active"},
    )
    monkeypatch.setenv("WAYFINDER_EVOLUTION_NUDGE", "0")
    assert nudge_evolution_session(store, job_id) is None
    assert len(client.prompts) == 1


def test_parentless_evolution_nudges_reuse_persisted_campaign_session(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    store.write_json(
        job_id,
        "state/evolution_campaign.json",
        {"campaign_id": "campaign-1", "status": "active"},
    )
    actions = iter(["prepare c1", "prepare c2", "prepare c2"])
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign.campaign_prompt_block",
        lambda store, job_id: {
            "campaign_id": "campaign-1",
            "next_action": next(actions),
        },
    )
    monkeypatch.delenv("OPENCODE_SESSION_ID", raising=False)
    monkeypatch.delenv("OPENCODE_SESSIONID", raising=False)

    class ParentlessClient:
        def __init__(self) -> None:
            self.created = 0
            self.prompts: list[str] = []
            self.sessions: set[str] = set()

        def healthy(self) -> bool:
            return True

        def session_exists(self, session_id: str) -> bool:
            return session_id in self.sessions

        def session_statuses(self) -> dict[str, Any]:
            return {}

        def find_child_session(self, *, parent_id: Any, title: str) -> None:
            assert parent_id is None
            return None

        def create_session(self, **kwargs: Any) -> str:
            self.created += 1
            session_id = f"session-{self.created}"
            self.sessions.add(session_id)
            return session_id

        def prompt_async(self, *, session_id: str, text: str, agent: str) -> bool:
            self.prompts.append(session_id)
            return True

    client = ParentlessClient()
    monkeypatch.setattr("wayfinder_paths.jobs.worker.OPENCODE_CLIENT", client)

    first = _queue_evolution_worker(store, job_id)
    second = nudge_evolution_session(store, job_id)
    duplicate = nudge_evolution_session(store, job_id)

    assert first == {"queued": True, "session_id": "session-1"}
    assert second == {"queued": True, "session_id": "session-1"}
    assert duplicate == {
        "queued": False,
        "session_id": "session-1",
        "deduplicated": True,
    }
    assert client.created == 1
    assert client.prompts == ["session-1", "session-1"]


def test_evolution_session_retirement_exports_before_exact_delete(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    store.write_json(
        job_id,
        "reports/evolution/session.json",
        {
            "campaign_id": "campaign-1",
            "session_id": "session-1",
            "created_at": "2026-08-25T12:00:00+00:00",
        },
    )

    class RetireClient:
        def __init__(self) -> None:
            self.aborted: list[str] = []
            self.deleted: list[str] = []

        def session_statuses(self) -> dict[str, Any]:
            return {"session-1": {"type": "busy"}}

        def abort_session(self, session_id: str) -> bool:
            self.aborted.append(session_id)
            return True

        def session_exists(self, session_id: str) -> bool:
            return True

        def delete_session(self, session_id: str) -> bool:
            self.deleted.append(session_id)
            return True

    client = RetireClient()
    monkeypatch.setattr("wayfinder_paths.jobs.worker.OPENCODE_CLIENT", client)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.benchmarks.agent_adapter.meter_session_ids",
        lambda session_ids: {
            "sessions": 1,
            "tokens_in": 123,
            "tokens_out": 7,
            "tool_result_bytes": 456,
        },
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.benchmarks.agent_adapter.session_diagnostic_summary",
        lambda session_id: {
            "schema_version": "1.0",
            "tool_calls": [
                {
                    "tool": "wayfinder_core_jobs",
                    "action": "evolution_prepare",
                    "status": "completed",
                }
            ],
            "omitted_tool_calls": 0,
            "final_assistant_text": "candidate prepared",
        },
    )

    retired = retire_evolution_session(store, job_id, "campaign-1")

    assert retired and retired["retired"] is True
    assert client.aborted == ["session-1"]
    assert client.deleted == ["session-1"]
    archive = store.read_json(job_id, "reports/evolution/sessions.json")
    assert archive["sessions"][0]["metrics"]["tool_result_bytes"] == 456
    assert archive["sessions"][0]["diagnostics"]["tool_calls"][0]["action"] == (
        "evolution_prepare"
    )
    assert archive["sessions"][0]["retired_at"]


def test_evolution_session_retirement_refuses_unresolved_metering(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    store.write_json(
        job_id,
        "reports/evolution/session.json",
        {"campaign_id": "campaign-1", "session_id": "session-1"},
    )

    class RetireClient:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def session_statuses(self) -> dict[str, Any]:
            return {}

        def session_exists(self, session_id: str) -> bool:
            return True

        def delete_session(self, session_id: str) -> bool:
            self.deleted.append(session_id)
            return True

    client = RetireClient()
    monkeypatch.setattr("wayfinder_paths.jobs.worker.OPENCODE_CLIENT", client)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.benchmarks.agent_adapter.meter_session_ids",
        lambda session_ids: {"sessions": 0},
    )

    retired = retire_evolution_session(store, job_id, "campaign-1")

    assert retired and retired["retired"] is False
    assert "exact persisted session" in retired["error"]
    assert client.deleted == []
    assert not (store.job_dir(job_id) / "reports/evolution/sessions.json").exists()


def test_evolution_session_retirement_refuses_failed_diagnostic_export(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    store.write_json(
        job_id,
        "reports/evolution/session.json",
        {"campaign_id": "campaign-1", "session_id": "session-1"},
    )

    class RetireClient:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def session_statuses(self) -> dict[str, Any]:
            return {}

        def session_exists(self, session_id: str) -> bool:
            return True

        def delete_session(self, session_id: str) -> bool:
            self.deleted.append(session_id)
            return True

    client = RetireClient()
    monkeypatch.setattr("wayfinder_paths.jobs.worker.OPENCODE_CLIENT", client)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.benchmarks.agent_adapter.meter_session_ids",
        lambda session_ids: {"sessions": 1},
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.benchmarks.agent_adapter.session_diagnostic_summary",
        lambda session_id: (_ for _ in ()).throw(RuntimeError("database locked")),
    )

    retired = retire_evolution_session(store, job_id, "campaign-1")

    assert retired and retired["retired"] is False
    assert retired["error"] == "session diagnostic export failed: database locked"
    assert client.deleted == []
    assert not (store.job_dir(job_id) / "reports/evolution/sessions.json").exists()


_EVAL_SPEC = {
    "market_kind": "perp",
    "data_contract": {"bar_interval": "1h", "symbols": ["IMX"]},
}


def _hourly_bars(count: int) -> list[dict[str, Any]]:
    rows = []
    for index in range(count):
        price = 10.0 + index * 0.05
        stamp = datetime(2026, 8, 1, tzinfo=UTC) + timedelta(hours=index)
        rows.append(
            {
                "timestamp": stamp.isoformat(),
                "symbol": "IMX",
                "open": price,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "volume": 100.0,
            }
        )
    return rows


def _evaluatable_job(
    tmp_path: Any, *, source_params: dict[str, Any] | None = None
) -> tuple[JobStore, str]:
    """Like _job but with a real spec, runnable strategy, and enough bars for
    the low-fidelity screen — so evaluate_candidate runs end to end."""
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "majors-5m-lab",
        name="Majors momentum lab",
        goal="find robust momentum and factor strategies",
        script="workspace/src/strategy.py",
        agent_mode="intervene",
        execution_contract="jobs_v1",
        interval_seconds=3600,
    )
    job.execution_spec = dict(_EVAL_SPEC)
    job.execution_params = {
        "symbols": ["IMX"],
        "initial_capital": 1_000.0,
        **(source_params or {}),
    }
    store.save(job)
    root = store.job_dir(job.id)
    script = root / "workspace" / "src" / "strategy.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("def decide(ctx):\n    return []\n", encoding="utf-8")
    bars = root / "results" / "backtest" / "input_bars.json"
    bars.parent.mkdir(parents=True, exist_ok=True)
    bars.write_text(
        json.dumps({"metadata": {"days": 120}, "bars": _hourly_bars(60)}),
        encoding="utf-8",
    )
    return store, job.id


def _read_bundle_params(store: JobStore, job_id: str, bundle: str) -> dict[str, Any]:
    data = yaml.safe_load(
        (store.job_dir(job_id) / bundle / "job.yaml").read_text(encoding="utf-8")
    )
    return dict(data["execution_params"])


def test_prepare_candidate_seeds_declared_window(tmp_path) -> None:
    store, job_id = _evaluatable_job(tmp_path / "default")
    start_campaign(store, job_id, now=datetime(2026, 8, 25, 12, tzinfo=UTC))
    candidate = prepare_candidate(
        store,
        job_id,
        family="breakout",
        summary="seed default",
        now=datetime(2026, 8, 25, 13, tzinfo=UTC),
    )
    params = _read_bundle_params(store, job_id, candidate["bundle"])
    assert params["warmup_bars"] == DEFAULT_WARMUP_BARS
    assert candidate["warmup_bars"] == DEFAULT_WARMUP_BARS
    # The frozen source was seeded identically, so an UNEDITED candidate still
    # matches the baseline revision (the identical-to-source guard holds).
    state = campaign_status(store, job_id)
    manifest = json.loads(
        (store.job_dir(job_id) / state["manifest"]).read_text(encoding="utf-8")
    )
    bundle_root = store.job_dir(job_id) / candidate["bundle"]
    assert (
        compute_workspace_revision(bundle_root) == manifest["source_bundle"]["revision"]
    )

    # A source that already declared a window (legacy lookback_bars) is
    # inherited, never overwritten with the default.
    inherit_store, inherit_id = _evaluatable_job(
        tmp_path / "inherit", source_params={"lookback_bars": 48}
    )
    start_campaign(inherit_store, inherit_id, now=datetime(2026, 8, 25, 12, tzinfo=UTC))
    inherited = prepare_candidate(
        inherit_store,
        inherit_id,
        family="breakout",
        summary="seed inherited",
        now=datetime(2026, 8, 25, 13, tzinfo=UTC),
    )
    params = _read_bundle_params(inherit_store, inherit_id, inherited["bundle"])
    assert params["warmup_bars"] == 48


def test_evaluate_rejects_undeclared_window_candidate(tmp_path) -> None:
    """A candidate that dodges the bounded-window contract (full_history)
    is invalid with the bounded-window hint as the reason."""
    store, job_id = _evaluatable_job(tmp_path)
    start_campaign(store, job_id, now=datetime(2026, 8, 25, 12, tzinfo=UTC))
    candidate = prepare_candidate(
        store,
        job_id,
        family="breakout",
        summary="undeclared window",
        now=datetime(2026, 8, 25, 13, tzinfo=UTC),
    )
    bundle = store.job_dir(job_id) / candidate["bundle"]
    data = yaml.safe_load((bundle / "job.yaml").read_text(encoding="utf-8"))
    data["execution_params"].pop("warmup_bars")
    data["execution_params"]["full_history"] = True
    data["execution_params"]["lookback_bars"] = 20
    (bundle / "job.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )

    result = evaluate_candidate(store, job_id, candidate["candidate_id"])

    assert result["status"] == "invalid"
    evidence = json.dumps(result["evidence"])
    assert "warmup_bars" in evidence
    assert "cannot exist in production" in evidence


def test_evaluate_accepts_seeded_candidate(tmp_path) -> None:
    store, job_id = _evaluatable_job(tmp_path)
    start_campaign(store, job_id, now=datetime(2026, 8, 25, 12, tzinfo=UTC))
    candidate = prepare_candidate(
        store,
        job_id,
        family="breakout",
        summary="seeded and edited",
        now=datetime(2026, 8, 25, 13, tzinfo=UTC),
    )
    script = (
        store.job_dir(job_id)
        / candidate["bundle"]
        / "workspace"
        / "src"
        / "strategy.py"
    )
    script.write_text(
        script.read_text(encoding="utf-8") + "\nSTRUCTURAL_PROBE = True\n",
        encoding="utf-8",
    )

    result = evaluate_candidate(store, job_id, candidate["candidate_id"])

    assert result["status"] == "quick_complete", result.get("evidence")


def test_evaluate_releases_claim_when_compute_is_transiently_busy(
    tmp_path, monkeypatch
) -> None:
    import wayfinder_paths.jobs.evolution_campaign as campaign_module

    store, job_id = _evaluatable_job(tmp_path)
    start_campaign(store, job_id, now=datetime(2026, 8, 25, 12, tzinfo=UTC))
    candidate = prepare_candidate(
        store,
        job_id,
        family="breakout",
        summary="transient compute probe",
        now=datetime(2026, 8, 25, 13, tzinfo=UTC),
    )

    @contextmanager
    def busy_compute(*args, **kwargs):
        raise ComputeLockBusy("box busy")
        yield

    monkeypatch.setattr(campaign_module, "experiment_compute_lock", busy_compute)

    with pytest.raises(ComputeLockBusy, match="box busy"):
        evaluate_candidate(store, job_id, candidate["candidate_id"])

    state = campaign_status(store, job_id)
    recovered = state["candidates"][0]
    assert recovered["status"] == "quick_failed"
    assert "evaluation_claim_id" not in recovered
    assert state["counts"]["quick_evaluated"] == 0


def test_full_dev_runs_real_small_simulations_in_disposable_child(tmp_path) -> None:
    store, job_id = _evaluatable_job(tmp_path)
    start_campaign(store, job_id, now=datetime(2026, 8, 25, 12, tzinfo=UTC))
    candidate = prepare_candidate(
        store,
        job_id,
        family="breakout",
        summary="real isolated full-dev probe",
        now=datetime(2026, 8, 25, 13, tzinfo=UTC),
    )
    script = (
        store.job_dir(job_id)
        / candidate["bundle"]
        / "workspace"
        / "src"
        / "strategy.py"
    )
    script.write_text(
        script.read_text(encoding="utf-8") + "\nFULL_DEV_PROBE = True\n",
        encoding="utf-8",
    )

    outcome = _isolated_full_dev(store, job_id, candidate, tune=False)

    assert outcome["status"] in {"dev_frontier", "low_fidelity_rejected"}
    resources = store.read_json(job_id, "reports/evolution/resources.json")
    assert resources["latest"]["phase"] == "full_dev"
    assert resources["latest"]["candidate_id"] == candidate["candidate_id"]


def test_casebook_includes_bounded_window_parity_case() -> None:
    cases = {case["id"] for case in load_starter_casebook()}
    assert "bounded-window-parity" in cases
    selected = select_starter_cases({"warmup", "performance"})
    assert len(selected) <= MAX_PROMPT_CASES
    assert any(case["id"] == "bounded-window-parity" for case in selected)


def test_op_runner_nudges_after_evolution_ops_and_contains_failures(
    monkeypatch, capsys
) -> None:
    from wayfinder_paths.jobs.execution import op_runner

    nudged: list[str] = []
    monkeypatch.setattr(op_runner, "_lower_priority", lambda: None)
    monkeypatch.setattr(op_runner, "_run", lambda op, kwargs: {"ok": True})
    monkeypatch.setattr(
        "wayfinder_paths.jobs.worker.nudge_evolution_session",
        lambda store, job_id: nudged.append(job_id),
    )

    def run_main(op: str, kwargs: dict[str, Any]) -> None:
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(json.dumps({"op": op, "kwargs": kwargs}))
        )
        op_runner.main()

    run_main("evolution_evaluate", {"job_id": "majors-5m-lab", "candidate_id": "c1"})
    # evolution_start completion nudges too: the first campaign prompt no
    # longer waits out a full hourly wake interval.
    run_main("evolution_start", {"job_id": "majors-5m-lab"})
    run_main("backtest_job", {"job_id": "majors-5m-lab"})
    assert nudged == ["majors-5m-lab", "majors-5m-lab"]

    def boom(store: Any, job_id: str) -> None:
        raise RuntimeError("nudge failed")

    monkeypatch.setattr("wayfinder_paths.jobs.worker.nudge_evolution_session", boom)
    run_main("evolution_evaluate", {"job_id": "majors-5m-lab", "candidate_id": "c1"})
    # The op result was written all four times; the failed nudge stayed inside
    # its guard instead of failing the op.
    assert capsys.readouterr().out.count('{"ok": true}') == 4


def test_cli_evolution_start_nudges_the_session(monkeypatch, tmp_path) -> None:
    """An inline CLI force-start must nudge the evolution session itself —
    it never passes through the op runner whose completion hook nudges."""
    from click.testing import CliRunner

    from wayfinder_paths.jobs import cli as cli_module

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        cli_module, "start_campaign", lambda store, job_id, force: {"status": "active"}
    )
    monkeypatch.setattr(
        cli_module,
        "nudge_evolution_session",
        lambda store, job_id: calls.append(("nudge", job_id)) or {"queued": True},
    )
    outcome = CliRunner().invoke(
        cli_module.job_cli, ["evolution-start", "job-nudge-demo", "--force"]
    )
    assert outcome.exit_code == 0, outcome.output
    assert calls == [("nudge", "job-nudge-demo")]
    assert '"queued": true' in outcome.output
