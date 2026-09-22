from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wayfinder_paths.jobs.archive import find_candidate
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.probation import (
    cancel_probation_trial,
    load_probation,
    promote_probation_trial_early,
    stage_probation_trial,
)
from wayfinder_paths.jobs.store import JobStore

STARTED = datetime(2026, 8, 1, tzinfo=UTC)


def _job(tmp_path: Path) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "majors-5m-lab",
        script="workspace/src/strategy.py",
        agent_mode="intervene",
        execution_contract="jobs_v1",
    )
    job.execution_params = {"symbols": ["BTC", "HYPE"], "venue": "hyperliquid"}
    store.save(job)
    script = store.job_dir(job.id) / "workspace/src/strategy.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("def decide(ctx):\n    return []\n", encoding="utf-8")
    return store, job.id


def _candidate(store: JobStore, job_id: str, name: str) -> tuple[Path, str]:
    root = store.job_dir(job_id)
    candidate = root / "research" / "candidates" / name
    (candidate / "workspace/src").mkdir(parents=True)
    (candidate / "workspace/src/strategy.py").write_text(
        f"# {name}\ndef decide(ctx):\n    return []\n", encoding="utf-8"
    )
    (candidate / "job.yaml").write_bytes((root / "job.yaml").read_bytes())
    return candidate, compute_workspace_revision(candidate)


@pytest.fixture
def green_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    # A bare candidate has no execution spec/dataset; the verbs only care
    # that the contract check ran and passed.
    monkeypatch.setattr(
        "wayfinder_paths.jobs.probation.validate_execution_job",
        lambda *args, **kwargs: {"status": "passed", "checks": []},
    )


def _journal(store: JobStore, job_id: str) -> list[dict]:
    path = store.job_dir(job_id) / "journal.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _stage(store: JobStore, job_id: str, name: str, **kwargs) -> dict:
    candidate, revision = _candidate(store, job_id, name)
    return stage_probation_trial(
        store,
        job_id,
        candidate_dir=candidate,
        revision=revision,
        family=kwargs.pop("family", f"{name}-family"),
        summary=kwargs.pop("summary", f"{name} hypothesis"),
        by=kwargs.pop("by", "owner"),
        now=STARTED,
        **kwargs,
    )


def _forward_trial(store: JobStore, job_id: str, trial_id: str) -> dict:
    doc = load_probation(store, job_id)
    trial = next(item for item in doc["trials"] if item["trial_id"] == trial_id)
    trial["status"] = "active"
    trial["phase"] = "forward"
    trial["burn_in"]["status"] = "passed"
    trial["forward"]["started_at"] = STARTED.isoformat()
    trial["forward"]["deadline_at"] = (STARTED + timedelta(days=14)).isoformat()
    for role in ("candidate", "reference"):
        trial[role]["stream"] = f"results/forward/probation/{trial_id}/forward/{role}"
    store.write_json(job_id, "probation.json", doc)
    return trial


def _write_forward_days(
    store: JobStore, job_id: str, trial: dict, *, days: int, candidate_trades: int
) -> None:
    stamps = [STARTED + timedelta(days=offset) for offset in range(days)]
    for role in ("candidate", "reference"):
        stream = store.job_dir(job_id) / trial[role]["stream"]
        stream.mkdir(parents=True, exist_ok=True)
        (stream / "ticks.jsonl").write_text(
            "".join(
                json.dumps({"bar_ts": stamp.isoformat()}) + "\n" for stamp in stamps
            ),
            encoding="utf-8",
        )
    candidate_stream = store.job_dir(job_id) / trial["candidate"]["stream"]
    with (candidate_stream / "trades.jsonl").open("w", encoding="utf-8") as handle:
        for stamp in stamps[:candidate_trades]:
            handle.write(
                json.dumps({"timestamp": stamp.isoformat(), "net_pnl": 10.0}) + "\n"
            )


def test_stage_puts_variant_on_probation_with_chat_evidence(
    tmp_path: Path, green_validation: None
) -> None:
    store, job_id = _job(tmp_path)

    trial = _stage(store, job_id, "breakout", family="breakout", by="owner")

    assert trial["status"] == "burn_in"
    assert trial["source"] == "chat"
    assert trial["family"] == "breakout"
    assert trial["summary"] == "breakout hypothesis"
    assert trial["evidence"] == {"source": "chat", "by": "owner"}
    stored = load_probation(store, job_id)["trials"][0]
    assert stored["trial_id"] == trial["trial_id"]
    archived = find_candidate(store, job_id, trial["candidate_id"])
    assert archived is not None and archived["status"] == "probation"
    row = next(
        r for r in _journal(store, job_id) if r["type"] == "probation_trial_staged"
    )
    assert row["trial_id"] == trial["trial_id"]
    assert row["by"] == "owner"
    assert row["source"] == "chat"


def test_stage_refuses_when_capacity_is_full(
    tmp_path: Path, green_validation: None
) -> None:
    store, job_id = _job(tmp_path)
    for index in range(6):
        _stage(store, job_id, f"capacity-{index}")

    with pytest.raises(ValueError, match="probation capacity full"):
        _stage(store, job_id, "capacity-6")


def test_stage_refuses_duplicate_revision(
    tmp_path: Path, green_validation: None
) -> None:
    store, job_id = _job(tmp_path)
    candidate, revision = _candidate(store, job_id, "twice")
    first = stage_probation_trial(
        store,
        job_id,
        candidate_dir=candidate,
        revision=revision,
        family="twice-family",
        summary=None,
        by="owner",
    )

    with pytest.raises(ValueError, match=first["trial_id"]):
        stage_probation_trial(
            store,
            job_id,
            candidate_dir=candidate,
            revision=revision,
            family="twice-family",
            summary=None,
            by="owner",
        )


def test_stage_refuses_incumbent_bytes_and_bad_revision(
    tmp_path: Path, green_validation: None
) -> None:
    store, job_id = _job(tmp_path)
    root = store.job_dir(job_id)
    same = root / "research/candidates/same"
    (same / "workspace/src").mkdir(parents=True)
    (same / "workspace/src/strategy.py").write_bytes(
        (root / "workspace/src/strategy.py").read_bytes()
    )
    (same / "job.yaml").write_bytes((root / "job.yaml").read_bytes())

    with pytest.raises(ValueError, match="byte-identical"):
        stage_probation_trial(
            store,
            job_id,
            candidate_dir=same,
            revision=compute_workspace_revision(same),
            family="same-family",
            summary=None,
            by="owner",
        )
    candidate, _revision = _candidate(store, job_id, "stale")
    with pytest.raises(ValueError, match="does not match"):
        stage_probation_trial(
            store,
            job_id,
            candidate_dir=candidate,
            revision="deadbeefdead",
            family="stale-family",
            summary=None,
            by="owner",
        )


def test_cancel_closes_trial_activates_queued_and_archives(
    tmp_path: Path, green_validation: None
) -> None:
    store, job_id = _job(tmp_path)
    trials = [_stage(store, job_id, f"slot-{index}") for index in range(4)]
    assert [trial["status"] for trial in trials] == ["burn_in"] * 3 + ["queued"]
    later = STARTED + timedelta(hours=2)

    result = cancel_probation_trial(
        store,
        job_id,
        trials[0]["trial_id"],
        by="owner",
        reason="owner changed their mind",
        now=later,
    )

    cancelled = result["trial"]
    assert cancelled["status"] == "cancelled"
    assert cancelled["phase"] == "complete"
    assert cancelled["verdict_reason"] == "owner changed their mind"
    assert cancelled["cancelled_by"] == "owner"
    assert cancelled["closed_at"] == later.isoformat()
    assert result["activated"] == [trials[3]["trial_id"]]
    doc = load_probation(store, job_id)
    by_id = {trial["trial_id"]: trial for trial in doc["trials"]}
    assert by_id[trials[0]["trial_id"]]["status"] == "cancelled"
    assert by_id[trials[3]["trial_id"]]["status"] == "burn_in"
    assert by_id[trials[3]["trial_id"]]["burn_in"]["started_at"] == later.isoformat()
    archived = find_candidate(store, job_id, trials[0]["candidate_id"])
    assert archived is not None and archived["status"] == "archived"
    rows = _journal(store, job_id)
    cancel_row = next(r for r in rows if r["type"] == "probation_trial_cancelled")
    assert cancel_row["trial_id"] == trials[0]["trial_id"]
    assert cancel_row["by"] == "owner"
    assert cancel_row["reason"] == "owner changed their mind"
    assert any(
        r["type"] == "evolution_probation_burn_in_started"
        and r["trial_id"] == trials[3]["trial_id"]
        and r["source"] == "probation_queue"
        for r in rows
    )

    with pytest.raises(ValueError, match="already cancelled"):
        cancel_probation_trial(
            store, job_id, trials[0]["trial_id"], by="owner", reason="again"
        )
    with pytest.raises(ValueError, match="unknown probation trial"):
        cancel_probation_trial(store, job_id, "nope", by="owner", reason="x")


def test_promote_early_refuses_without_paired_days_or_trades(
    tmp_path: Path, green_validation: None
) -> None:
    store, job_id = _job(tmp_path)
    trial = _stage(store, job_id, "early")

    with pytest.raises(ValueError, match="burn_in/burn_in"):
        promote_probation_trial_early(
            store, job_id, trial["trial_id"], by="owner", reason="looks good"
        )

    forward = _forward_trial(store, job_id, trial["trial_id"])
    with pytest.raises(
        ValueError, match=r"0 paired day\(s\) and 0 closed candidate trade\(s\)"
    ) as excinfo:
        promote_probation_trial_early(
            store,
            job_id,
            trial["trial_id"],
            by="owner",
            reason="looks good",
            now=STARTED + timedelta(days=3),
        )
    assert "at least 1 paired day and 3 closed trades" in str(excinfo.value)
    assert load_probation(store, job_id)["trials"][0]["status"] == "active"

    # Paired days without enough closes is still refused.
    _write_forward_days(store, job_id, forward, days=3, candidate_trades=2)
    with pytest.raises(ValueError, match=r"3 paired day\(s\) and 2 closed"):
        promote_probation_trial_early(
            store,
            job_id,
            trial["trial_id"],
            by="owner",
            reason="looks good",
            now=STARTED + timedelta(days=3),
        )


def test_promote_early_graduates_into_owner_approved_proposal(
    tmp_path: Path, green_validation: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job_id = _job(tmp_path)
    trial = _stage(store, job_id, "winner")
    forward = _forward_trial(store, job_id, trial["trial_id"])
    _write_forward_days(store, job_id, forward, days=3, candidate_trades=3)
    captured: dict = {}

    def fake_propose(*args, **kwargs):
        captured.update(kwargs)
        return {"proposal_id": kwargs["proposal_id"]}

    monkeypatch.setattr("wayfinder_paths.jobs.proposals.propose_change", fake_propose)
    promoted_at = STARTED + timedelta(days=3, hours=1)

    result = promote_probation_trial_early(
        store,
        job_id,
        trial["trial_id"],
        by="owner",
        reason="three green days is enough for me",
        now=promoted_at,
    )

    graduated = result["trial"]
    assert graduated["status"] == "graduated"
    assert graduated["phase"] == "complete"
    assert graduated["early_by"] == "owner"
    assert graduated["early_reason"] == "three green days is enough for me"
    assert graduated["early_at"] == promoted_at.isoformat()
    assert graduated["forward"]["metrics"]["paired_days"] == 3
    assert graduated["forward"]["metrics"]["candidate_trade_count"] == 3
    assert result["proposal_id"] == captured["proposal_id"]
    assert result["proposal_id"].startswith("prop-probation-")
    assert captured["allow_auto_apply"] is False
    assert captured["kind"] == "code_change"
    assert graduated["promotion"] == {
        "status": "owner_review",
        "proposal_id": result["proposal_id"],
        "created_at": graduated["promotion"]["created_at"],
    }
    archived = find_candidate(store, job_id, trial["candidate_id"])
    assert archived is not None and archived["status"] == "paper_experiment"
    rows = _journal(store, job_id)
    row = next(r for r in rows if r["type"] == "probation_trial_promoted_early")
    assert row["by"] == "owner"
    assert row["proposal_id"] == result["proposal_id"]
    assert row["paired_days"] == 3
    assert any(r["type"] == "probation_promotion_proposed" for r in rows)

    with pytest.raises(ValueError, match="graduated/complete"):
        promote_probation_trial_early(
            store, job_id, trial["trial_id"], by="owner", reason="again"
        )
