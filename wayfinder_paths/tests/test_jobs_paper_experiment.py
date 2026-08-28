from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.paper_experiment import (
    _normalize_control_revision,
    _proposal_verdict,
    _resource_cost,
    _verdict_report,
    current_job_token_usage,
    ensure_paper_experiment,
    experiment_status,
    harvest_hourly_control_candidates,
    maybe_adjudicate_proposals,
    maybe_finalize_experiment,
    record_evidence,
    stage_paper_proposal,
)
from wayfinder_paths.jobs.probation import load_probation
from wayfinder_paths.jobs.store import JobStore


def _job(tmp_path: Path, *, now: datetime) -> tuple[JobStore, str, dict]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("majors-5m-lab", script="workspace/src/strategy.py")
    store.save(job)
    script = store.job_dir(job.id) / "workspace" / "src" / "strategy.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("def decide(ctx):\n    return []\n", encoding="utf-8")
    state = ensure_paper_experiment(store, job.id, now=now)
    assert state is not None
    return store, job.id, state


def _candidate_source(store: JobStore, job_id: str, name: str = "candidate-source"):
    root = store.job_dir(job_id)
    source = root / name
    (source / "workspace" / "src").mkdir(parents=True)
    (source / "workspace" / "src" / "strategy.py").write_text(
        "def decide(ctx):\n    return []\n", encoding="utf-8"
    )
    (source / "job.yaml").write_bytes((root / "job.yaml").read_bytes())
    return source, compute_workspace_revision(source)


def test_archived_evolution_sessions_remain_in_resource_accounting(
    tmp_path: Path,
) -> None:
    started = datetime(2026, 8, 1, tzinfo=UTC)
    store, job_id, state = _job(tmp_path, now=started)
    store.write_json(
        job_id,
        "reports/evolution/sessions.json",
        {
            "sessions": [
                {
                    "campaign_id": "campaign-1",
                    "session_id": "deleted-session",
                    "created_at": started.isoformat(),
                    "retired_at": (started + timedelta(hours=4)).isoformat(),
                    "metrics": {
                        "sessions": 1,
                        "tokens_in": 100,
                        "tokens_out": 10,
                        "tool_calls": 3,
                        "tool_result_bytes": 500,
                        "tool_result_bytes_by_tool": {"read": 400, "bash": 100},
                    },
                }
            ]
        },
    )

    usage = current_job_token_usage(store, job_id, arm="evolution", state=state)

    assert usage["sessions"] == 1
    assert usage["tokens_in"] == 100
    assert usage["tool_result_bytes"] == 500
    assert usage["tool_result_bytes_by_tool"] == {"read": 400, "bash": 100}


def _write_days(
    store: JobStore,
    job_id: str,
    state: dict,
    *,
    evolution_pnl: float,
) -> None:
    for arm in ("control", "evolution"):
        champion = state["arms"][arm]["champion"]
        stream = store.job_dir(job_id) / champion["stream"]
        stream.mkdir(parents=True, exist_ok=True)
        ticks = []
        trades = []
        for offset in range(14):
            stamp = datetime(2026, 8, 1, tzinfo=UTC) + timedelta(days=offset)
            ticks.append(json.dumps({"bar_ts": stamp.isoformat()}))
            if arm == "evolution" and evolution_pnl:
                trades.append(
                    json.dumps(
                        {"timestamp": stamp.isoformat(), "net_pnl": evolution_pnl}
                    )
                )
        (stream / "ticks.jsonl").write_text("\n".join(ticks) + "\n", encoding="utf-8")
        if trades:
            (stream / "trades.jsonl").write_text(
                "\n".join(trades) + "\n", encoding="utf-8"
            )


@pytest.mark.parametrize(
    ("evolution_pnl", "expected"),
    [(10.0, "accrete"), (-10.0, "kill"), (0.0, "inconclusive")],
)
def test_fixed_horizon_paper_verdict_is_pre_registered_and_paper_only(
    tmp_path: Path, evolution_pnl: float, expected: str
) -> None:
    started = datetime(2026, 8, 1, tzinfo=UTC)
    store, job_id, state = _job(tmp_path, now=started)
    state["status"] = "active"
    state["started_at"] = started.isoformat()
    state["ends_at"] = (started + timedelta(days=14)).isoformat()
    store.write_json(job_id, "state/evolution_experiment.json", state)
    original_job = (store.job_dir(job_id) / "job.yaml").read_bytes()
    assert state["protocol"]["primary_endpoint"] == (
        "paired_daily_forward_log_utility_delta_lcb"
    )
    assert state["protocol"]["paper_only"] is True
    assert state["protocol"]["parity_policy"] == {
        "resource_budget": "reported_only",
        "admissions": "reported_only",
    }
    assert (
        state["arms"]["control"]["champion"]["stream"]
        != (state["arms"]["evolution"]["champion"]["stream"])
    )
    _write_days(store, job_id, state, evolution_pnl=evolution_pnl)

    report = maybe_finalize_experiment(store, job_id, now=started + timedelta(days=14))

    assert report is not None
    assert report["verdict"] == expected
    assert report["paired_days"] == 14
    assert report["paper_only"] is True
    assert experiment_status(store, job_id)["status"] == "complete"
    assert (store.job_dir(job_id) / "job.yaml").read_bytes() == original_job


def test_candidate_is_frozen_but_does_not_replace_champion_before_forward_day(
    tmp_path: Path,
) -> None:
    started = datetime(2026, 8, 1, tzinfo=UTC)
    store, job_id, state = _job(tmp_path, now=started)
    root = store.job_dir(job_id)
    source, revision = _candidate_source(store, job_id)
    control_before = dict(state["arms"]["control"]["champion"])
    with pytest.raises(ValueError, match="path-safe"):
        stage_paper_proposal(
            store,
            job_id,
            arm="evolution",
            candidate_id="candidate-escape",
            candidate_root=source,
            revision="../../escape",
            source="evolution_campaign",
            now=started + timedelta(minutes=30),
        )

    staged = stage_paper_proposal(
        store,
        job_id,
        arm="evolution",
        candidate_id="candidate-1",
        candidate_root=source,
        revision=revision,
        source="evolution_campaign",
        now=started + timedelta(hours=1),
    )

    updated = experiment_status(store, job_id)
    assert updated["arms"]["control"]["champion"] == control_before
    assert (
        updated["arms"]["evolution"]["champion"]
        == state["arms"]["evolution"]["champion"]
    )
    assert staged["candidate"]["stream"].startswith(
        "results/forward/experiment/proposals/evolution/"
    )
    assert staged["reference"]["source"] == "incumbent"
    assert load_probation(store, job_id).get("legs") == []
    copied = root / staged["bundle"] / "workspace" / "src" / "strategy.py"
    source.joinpath("workspace/src/strategy.py").write_text(
        "raise RuntimeError('changed source')\n", encoding="utf-8"
    )
    assert "changed source" not in copied.read_text(encoding="utf-8")


def test_control_revision_migrates_only_known_operator_dial_hash(
    tmp_path: Path,
) -> None:
    started = datetime(2026, 8, 1, tzinfo=UTC)
    store, job_id, _ = _job(tmp_path, now=started)
    source, current = _candidate_source(store, job_id)
    legacy = compute_workspace_revision(source, retain_operator_dials=True)
    assert legacy != current

    normalized, migration = _normalize_control_revision(source, legacy)

    assert normalized == current
    assert migration == {
        "kind": "operator_dial_hash_hygiene",
        "recorded_revision": legacy,
        "current_revision": current,
    }
    source.joinpath("workspace/src/strategy.py").write_text(
        "def decide(ctx):\n    return [{'action': 'OPEN'}]\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="immutable bundle"):
        _normalize_control_revision(source, legacy)


def test_hourly_control_harvest_restamps_legacy_operator_dial_revision(
    tmp_path: Path,
) -> None:
    started = datetime(2026, 8, 1, tzinfo=UTC)
    store, job_id, _ = _job(tmp_path, now=started)
    source, current = _candidate_source(store, job_id)
    legacy = compute_workspace_revision(source, retain_operator_dials=True)
    assert legacy != current
    proposal_id = "prop-legacy-revision"
    store.write_json(
        job_id,
        f"proposals/{proposal_id}.json",
        {
            "proposal_id": proposal_id,
            "status": "approved",
            "created_at": started.isoformat(),
            "application": {
                "status": "applied",
                "candidate_dir": str(source.relative_to(store.repo_root)),
            },
            "candidate_report": {
                "revision": legacy,
                "validation_summary": {"status": "passed"},
                "gate": {"live_ready": True},
                "economic": {
                    "ready": True,
                    "paired_incumbent_delta": {"lcb": 0.1, "estimate": 0.2},
                },
            },
        },
    )

    staged = harvest_hourly_control_candidates(
        store, job_id, now=started + timedelta(hours=1)
    )

    assert staged is not None
    assert staged["revision"] == current
    assert staged["evidence"]["revision_migration"] == {
        "kind": "operator_dial_hash_hygiene",
        "recorded_revision": legacy,
        "current_revision": current,
    }
    assert (
        f"control:{staged['candidate_id']}:{current}"
        in experiment_status(store, job_id)["seen_candidates"]
    )


def test_experiment_retires_only_legacy_evolution_probation(tmp_path: Path) -> None:
    started = datetime(2026, 8, 1, tzinfo=UTC)
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("majors-5m-lab", script="workspace/src/strategy.py")
    store.save(job)
    script = store.job_dir(job.id) / "workspace" / "src" / "strategy.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("def decide(ctx):\n    return []\n", encoding="utf-8")
    leg_template = {
        "symbol": "portfolio",
        "status": "active",
        "tier": "paper",
        "graduate": {"criterion": "paper evidence", "progress": None},
        "kill": {"criterion": "safety breach", "status": None},
    }
    store.write_json(
        job.id,
        "probation.json",
        {
            "legs": [
                {
                    **leg_template,
                    "name": "ordinary-funnel-probation",
                    "candidate_bundle_id": "funnel-candidate",
                },
                {
                    **leg_template,
                    "name": "legacy-evolution-probation",
                    "candidate_bundle_id": "evolution-candidate",
                    "campaign_id": "campaign-1",
                },
            ]
        },
    )

    assert ensure_paper_experiment(store, job.id, now=started) is not None

    legs = {leg["name"]: leg for leg in load_probation(store, job.id).get("legs") or []}
    assert legs["ordinary-funnel-probation"]["status"] == "active"
    assert legs["legacy-evolution-probation"]["status"] == "killed"
    assert (
        legs["legacy-evolution-probation"]["graduate"]["progress"]
        == "migrated to the isolated paper A/B rail"
    )


def test_qualification_deadline_closes_without_an_evolution_survivor(
    tmp_path: Path,
) -> None:
    started = datetime(2026, 8, 1, tzinfo=UTC)
    store, job_id, _ = _job(tmp_path, now=started)

    report = maybe_finalize_experiment(store, job_id, now=started + timedelta(days=7))

    assert report is not None
    assert report["verdict"] == "kill"
    assert report["reason"] == "no_qualifier"
    assert experiment_status(store, job_id)["status"] == "complete"


def test_resource_meter_uses_arm_local_finalist_costs(tmp_path: Path) -> None:
    started = datetime(2026, 8, 1, tzinfo=UTC)
    store, job_id, state = _job(tmp_path, now=started)
    for arm in ("control", "evolution"):
        record_evidence(
            store,
            job_id,
            arm=arm,
            candidate_id=f"{arm}-candidate",
            revision=f"{arm}-revision",
            admitted=False,
            evidence={
                "token_usage": {"tokens_in": 80, "tokens_out": 20},
                "sim_wall_seconds": 60,
            },
        )

    costs = _resource_cost(store, job_id, state)
    assert costs["control"]["tokens"] == 100
    assert costs["evolution"]["tokens"] == 100
    assert costs["control"]["sim_wall_seconds"] == 60
    assert costs["evolution"]["sim_wall_seconds"] == 60


def test_resource_and_admissions_parity_are_reported_not_gating(
    tmp_path: Path,
) -> None:
    started = datetime(2026, 8, 1, tzinfo=UTC)
    store, job_id, state = _job(tmp_path, now=started)
    state["status"] = "active"
    state["started_at"] = started.isoformat()
    state["ends_at"] = (started + timedelta(days=14)).isoformat()
    state["admissions"] = {"control": 2, "evolution": 1}
    _write_days(store, job_id, state, evolution_pnl=10.0)
    record_evidence(
        store,
        job_id,
        arm="evolution",
        candidate_id="evolution-candidate",
        revision="evolution-revision",
        admitted=True,
        evidence={
            "token_usage": {"tokens_in": 80, "tokens_out": 20},
            "sim_wall_seconds": 60,
        },
    )

    report = _verdict_report(store, job_id, state)

    assert report["verdict"] == "accrete"
    assert report["resource_budget_balance"]["matched"] is False
    assert report["admissions_parity"] == {
        "matched": False,
        "control": 2,
        "evolution": 1,
    }


def test_non_incumbent_champion_requires_strict_forward_improvement(
    tmp_path: Path,
) -> None:
    started = datetime(2026, 8, 1, tzinfo=UTC)
    store, job_id, _ = _job(tmp_path, now=started)
    proposal = {
        "arm": "evolution",
        "candidate_id": "candidate-2",
        "revision": "revision-2",
        "candidate": {"error_count": 0},
        "reference": {"error_count": 0, "source": "evolution_campaign"},
        "evidence": {"objective": {"candidate": {"trade_count": 10}}},
    }
    stats = {"closed_trades": 1, "net_return": 0.01, "max_drawdown_pct": 0.0}

    tied = _proposal_verdict(
        store,
        job_id,
        proposal,
        candidate_stats=stats,
        reference_stats=stats,
        mature=True,
        coverage=1.0,
    )
    better = _proposal_verdict(
        store,
        job_id,
        proposal,
        candidate_stats={**stats, "net_return": 0.0101},
        reference_stats=stats,
        mature=True,
        coverage=1.0,
    )
    first_challenger = _proposal_verdict(
        store,
        job_id,
        {**proposal, "reference": {"error_count": 0, "source": "incumbent"}},
        candidate_stats={**stats, "net_return": 0.0},
        reference_stats=stats,
        mature=True,
        coverage=1.0,
    )

    assert tied["status"] == "rejected"
    assert "strictly improve" in tied["reasons"][-1]
    assert better["status"] == "qualified"
    assert first_challenger["status"] == "qualified"


def test_wall_clock_expiry_closes_proposal_without_forward_bars(
    tmp_path: Path,
) -> None:
    started = datetime(2026, 8, 1, tzinfo=UTC)
    store, job_id, _ = _job(tmp_path, now=started)
    source, revision = _candidate_source(store, job_id)
    proposal = stage_paper_proposal(
        store,
        job_id,
        arm="evolution",
        candidate_id="candidate-expiry",
        candidate_root=source,
        revision=revision,
        source="evolution_campaign",
        evidence={
            "objective": {"candidate": {"trade_count": 10}},
            "token_usage": {"tokens_in": 1, "tokens_out": 1},
        },
        now=started + timedelta(hours=1),
    )

    outcomes = maybe_adjudicate_proposals(
        store,
        job_id,
        now=datetime.fromisoformat(proposal["expires_at"]) + timedelta(minutes=1),
    )

    assert outcomes[0]["status"] == "rejected"
    assert "insufficient common-bar coverage" in outcomes[0]["reasons"][0]
    slot = experiment_status(store, job_id)["proposals"]["evolution"]
    assert slot["active"] is None
    assert slot["history"][-1]["status"] == "rejected"


def test_late_qualified_proposal_closes_after_experiment_completion(
    tmp_path: Path,
) -> None:
    started = datetime(2026, 8, 1, tzinfo=UTC)
    store, job_id, state = _job(tmp_path, now=started)
    source, revision = _candidate_source(store, job_id)
    proposal = stage_paper_proposal(
        store,
        job_id,
        arm="evolution",
        candidate_id="candidate-late",
        candidate_root=source,
        revision=revision,
        source="evolution_campaign",
        evidence={
            "objective": {"candidate": {"trade_count": 10}},
            "token_usage": {"tokens_in": 1, "tokens_out": 1},
        },
        now=started + timedelta(hours=1),
    )
    first_bar = started + timedelta(hours=1)
    ticks = [
        json.dumps({"bar_ts": (first_bar + timedelta(minutes=5 * i)).isoformat()})
        for i in range(289)
    ]
    for role, pnl in (("candidate", 10.0), ("reference", 1.0)):
        stream = store.job_dir(job_id) / proposal[role]["stream"]
        stream.mkdir(parents=True, exist_ok=True)
        (stream / "ticks.jsonl").write_text("\n".join(ticks) + "\n", encoding="utf-8")
        (stream / "trades.jsonl").write_text(
            json.dumps({"timestamp": first_bar.isoformat(), "net_pnl": pnl}) + "\n",
            encoding="utf-8",
        )
    state = experiment_status(store, job_id)
    champion_before = dict(state["arms"]["evolution"]["champion"])
    state["status"] = "complete"
    store.write_json(job_id, "state/evolution_experiment.json", state)

    outcomes = maybe_adjudicate_proposals(
        store, job_id, now=started + timedelta(hours=26)
    )

    assert outcomes[0]["status"] == "rejected"
    assert outcomes[0]["qualified_before_experiment_close"] is True
    assert "closed before candidate admission" in outcomes[0]["reasons"][-1]
    updated = experiment_status(store, job_id)
    assert updated["proposals"]["evolution"]["active"] is None
    assert updated["arms"]["evolution"]["champion"] == champion_before
