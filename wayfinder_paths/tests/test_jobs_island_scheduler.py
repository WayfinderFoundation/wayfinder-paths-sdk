"""Island scheduler: deterministic weighted-deficit rotation, protected
exploration floor, stuck/recall boosts, trigger-wake bypass, and the wake
context integration (search_assignment block + quant dispatch rule)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import yaml

from wayfinder_paths.jobs.improver.scheduler import (
    EXPLORE_ISLANDS,
    ISLANDS,
    assign_island,
    load_scheduler_state,
)
from wayfinder_paths.jobs.improver.spec import IMPROVER_FILENAME
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore


def _job(tmp_path, job_id="isl-demo"):
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        job_id,
        script="workspace/src/strategy.py",
        agent_mode="intervene",
        execution_contract="jobs_v1",
    )
    store.save(job)
    return store, job.id


def test_rotation_is_deterministic_and_tracks_weights(tmp_path) -> None:
    sequences = []
    for run in ("a", "b"):
        store, job_id = _job(tmp_path / run)
        sequence = [assign_island(store, job_id)["island"] for _ in range(40)]
        sequences.append(sequence)
    assert sequences[0] == sequences[1]  # pure function of on-disk state

    counts = {island: sequences[0].count(island) for island in ISLANDS}
    assert sequences[0][0] == "exploit"  # highest target share leads
    assert 12 <= counts["exploit"] <= 18  # ~40% of 40
    explore = sum(counts[i] for i in EXPLORE_ISLANDS)
    assert explore / 40 >= 0.25  # spec floor holds in aggregate
    assert all(counts[island] > 0 for island in ISLANDS)  # nobody starves


def test_exploration_floor_binds_under_skewed_weights(tmp_path) -> None:
    store, job_id = _job(tmp_path)
    (store.job_dir(job_id) / IMPROVER_FILENAME).write_text(
        yaml.safe_dump(
            {
                "islands": {
                    "weights": {
                        "exploit": 0.90,
                        "adjacent": 0.05,
                        "divergent": 0.02,
                        "diversification": 0.01,
                        "falsifier": 0.01,
                        "historian": 0.01,
                    },
                    "exploration_floor": 0.3,
                }
            }
        )
    )
    sequence = [assign_island(store, job_id) for _ in range(20)]
    islands = [entry["island"] for entry in sequence]
    explore = sum(1 for island in islands if island in EXPLORE_ISLANDS)
    assert explore / 20 >= 0.25  # floor overrides the skew
    floored = [e for e in sequence if any("floor" in r for r in e["reasons"])]
    assert floored, "floor restriction must be visible in reasons"

    state = load_scheduler_state(store, job_id)
    assert state["total"] == 20
    assert sum(state["assigned"].values()) == 20


def test_stuck_streak_boosts_divergent(tmp_path) -> None:
    store, job_id = _job(tmp_path)
    store.write_json(
        job_id,
        "state/promotion_verdicts.json",
        {
            "prop-1": {"verdict": "neutral", "recorded_at": "2026-08-01T00:00:00Z"},
            "prop-2": {"verdict": "hurt", "recorded_at": "2026-08-02T00:00:00Z"},
        },
    )
    result = assign_island(store, job_id)
    assert any("stuck" in reason for reason in result["reasons"])
    assert result["target_weights"]["divergent"] > 0.2  # 0.15 doubled, renormed

    # A beat verdict at the tail clears the streak.
    store.write_json(
        job_id,
        "state/promotion_verdicts.json",
        {
            "prop-1": {"verdict": "hurt", "recorded_at": "2026-08-01T00:00:00Z"},
            "prop-2": {"verdict": "beat", "recorded_at": "2026-08-02T00:00:00Z"},
        },
    )
    result = assign_island(store, job_id)
    assert not any("stuck" in reason for reason in result["reasons"])


def test_trigger_wake_bypasses_rotation(tmp_path) -> None:
    store, job_id = _job(tmp_path)
    wake_path = store.job_dir(job_id) / "state" / "agent_wake_state.json"
    wake_path.parent.mkdir(parents=True, exist_ok=True)
    wake_path.write_text(
        json.dumps(
            {
                "last_triggered_wake_ts": datetime.now(UTC).isoformat(),
                "triggers": ["verdict_matured"],
            }
        )
    )
    result = assign_island(store, job_id)
    assert result == {"island": None, "bypass": ["verdict_matured"]}
    assert load_scheduler_state(store, job_id)["total"] == 0  # no state burn

    # A stale trigger stamp (old wake) does not bypass.
    wake_path.write_text(
        json.dumps(
            {
                "last_triggered_wake_ts": "2026-08-01T00:00:00+00:00",
                "triggers": ["verdict_matured"],
            }
        )
    )
    assert assign_island(store, job_id)["island"] is not None


def test_assignment_journaled_and_stamped(tmp_path) -> None:
    store, job_id = _job(tmp_path)
    result = assign_island(store, job_id)
    rows = [
        json.loads(line)
        for line in (store.job_dir(job_id) / "journal.jsonl").read_text().splitlines()
    ]
    events = [row for row in rows if row["type"] == "island_assigned"]
    assert events and events[-1]["island"] == result["island"]
    assert events[-1]["improver_revision"]  # PR 3 stamp rides along


def test_wake_context_carries_assignment_and_quant_rule(tmp_path) -> None:
    from wayfinder_paths.jobs.worker import prepare_job_worker_prompt

    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("isl-prompt", agent_mode="intervene")
    store.save(job)
    sections = prepare_job_worker_prompt(store=store, job_id=job.id, mode="intervene")
    assert "search_assignment" in sections["prompt"]
    assert "research/islands/" in sections["prompt"]
    assert "wayfinder-quant" in sections["stable_prefix"]
    assert load_scheduler_state(store, job.id)["total"] == 1

    # Monitor wakes are ops, not research: no assignment, no state burn.
    store2 = JobStore(repo_root=tmp_path / "b")
    job2 = WayfinderJob.new("isl-monitor", agent_mode="monitor")
    store2.save(job2)
    prepare_job_worker_prompt(store=store2, job_id=job2.id, mode="monitor")
    assert load_scheduler_state(store2, job2.id)["total"] == 0
