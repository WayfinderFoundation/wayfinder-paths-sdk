"""Durable regime remediation keeps incumbent alarms actionable."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.remediation import (
    handle_remediation_application,
    handle_remediation_rejection,
    link_remediation_proposal,
    load_remediation,
    proposal_remediation_stamp,
    sync_remediation_with_health,
    update_remediation_progress,
)
from wayfinder_paths.jobs.store import JobStore


def _store(tmp_path: Path) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("remediation-demo", agent_mode="intervene")
    store.save(job)
    return store, job.id


def _journal_types(store: JobStore, job_id: str) -> list[str]:
    path = store.job_dir(job_id) / "journal.jsonl"
    if not path.exists():
        return []
    return [
        str(json.loads(line).get("type"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _record_closed_trades(store: JobStore, job_id: str, count: int) -> None:
    summary = store.read_json(job_id, "results/forward/summary.json", default={}) or {}
    trades = dict(summary.get("trades") or {})
    trades["closed_count"] = count
    trades["last_trade_at"] = f"2026-08-21T12:00:{count:02d}+00:00"
    summary["trades"] = trades
    store.write_json(job_id, "results/forward/summary.json", summary)


def _health(*, score: int = 4, fingerprint: str = "health-a") -> dict:
    return {
        "status": "critical",
        "score": score,
        "computed_at": "2026-08-21T12:00:00+00:00",
        "evidence_fingerprint": fingerprint,
        "incumbent": {"workspace_revision": "rev-active"},
        "signals": [
            {
                "kind": "drawdown",
                "severity": 2,
                "value": 0.12,
                "window_days": 7,
            }
        ],
    }


def test_case_opens_once_and_retries_until_accountable_outcome(
    tmp_path: Path,
) -> None:
    store, job_id = _store(tmp_path)
    now = dt.datetime(2026, 8, 21, 12, tzinfo=dt.UTC)

    opened = sync_remediation_with_health(store, job_id, _health(), now=now)
    assert opened and opened["event"] == "regime_shift"
    # Quiet retries are debounced for 6 hours: the 30-minute cadence churned
    # (~every scheduled tick re-woke the agent over unchanged evidence).
    for minutes in (10, 31, 5 * 60):
        assert (
            sync_remediation_with_health(
                store, job_id, _health(), now=now + dt.timedelta(minutes=minutes)
            )
            is None
        )

    retry = sync_remediation_with_health(
        store, job_id, _health(), now=now + dt.timedelta(hours=6, minutes=1)
    )
    assert retry and retry["event"] == "regime_remediation_due"
    assert load_remediation(store, job_id)["attempts"] == 2  # type: ignore[index]


def test_recorded_progress_defers_the_retry_wake(tmp_path: Path) -> None:
    """A bounded progress note is the agent already working the case — the
    next quiet retry anchors on the note, not the original wake."""
    store, job_id = _store(tmp_path)
    now = dt.datetime(2026, 8, 21, 12, tzinfo=dt.UTC)
    sync_remediation_with_health(store, job_id, _health(), now=now)

    # Progress recorded 5h50m in (recorded_at = real now; anchor on max()).
    update_remediation_progress(
        store,
        job_id,
        state="evaluating",
        note="Running HYPE ablation on all OOS folds",
    )

    # 6h+ after the wake but minutes after the progress note: no re-wake.
    assert (
        sync_remediation_with_health(
            store, job_id, _health(), now=dt.datetime.now(dt.UTC)
        )
        is None
    )

    # 6h+ after the progress note: the quiet retry fires again.
    retry = sync_remediation_with_health(
        store,
        job_id,
        _health(),
        now=dt.datetime.now(dt.UTC) + dt.timedelta(hours=6, minutes=1),
    )
    assert retry and retry["event"] == "regime_remediation_due"


def test_material_evidence_still_wakes_through_the_debounce(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path)
    now = dt.datetime(2026, 8, 21, 12, tzinfo=dt.UTC)
    sync_remediation_with_health(store, job_id, _health(), now=now)
    update_remediation_progress(
        store, job_id, state="evaluating", note="ablating", artifact_path=None
    )

    refreshed = sync_remediation_with_health(
        store,
        job_id,
        _health(score=6, fingerprint="health-b"),
        now=now + dt.timedelta(minutes=2),
    )

    assert refreshed and refreshed["event"] == "regime_shift"


def test_material_evidence_bypasses_retry_debounce(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path)
    now = dt.datetime(2026, 8, 21, 12, tzinfo=dt.UTC)
    sync_remediation_with_health(store, job_id, _health(), now=now)

    refreshed = sync_remediation_with_health(
        store,
        job_id,
        _health(score=6, fingerprint="health-b"),
        now=now + dt.timedelta(minutes=1),
    )

    assert refreshed and refreshed["event"] == "regime_shift"
    assert "score_increased" in refreshed["material_reasons"]


def test_green_proposal_waits_for_owner_and_rejections_have_provenance(
    tmp_path: Path,
) -> None:
    store, job_id = _store(tmp_path)
    sync_remediation_with_health(store, job_id, _health())
    stamp = proposal_remediation_stamp(store, job_id)
    proposal = {
        "proposal_id": "prop-disable-hype",
        "remediation": stamp,
        "candidate_report": {
            "gate": {"live_ready": True},
            "economic": {"ready": True},
        },
    }

    link_remediation_proposal(store, job_id, proposal)
    assert load_remediation(store, job_id)["state"] == "proposal_pending"  # type: ignore[index]

    agent_rejection = {
        **proposal,
        "rejection": {"by": "agent", "kind": "process", "reason": "superseded"},
    }
    handle_remediation_rejection(store, job_id, agent_rejection)
    assert load_remediation(store, job_id)["state"] == "open"  # type: ignore[index]

    link_remediation_proposal(store, job_id, proposal)
    owner_rejection = {
        **proposal,
        "rejection": {"by": "owner", "kind": "substantive", "reason": "accept risk"},
    }
    handle_remediation_rejection(store, job_id, owner_rejection)
    assert load_remediation(store, job_id)["state"] == "owner_accepted_risk"  # type: ignore[index]


def test_red_candidate_and_bounded_evaluation_keep_case_open(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path)
    sync_remediation_with_health(store, job_id, _health())
    proposal = {
        "proposal_id": "prop-red",
        "remediation": proposal_remediation_stamp(store, job_id),
        "candidate_report": {
            "gate": {"live_ready": False},
            "economic": {"ready": False, "reasons": ["negative OOS"]},
        },
    }
    link_remediation_proposal(store, job_id, proposal)
    assert load_remediation(store, job_id)["state"] == "blocked"  # type: ignore[index]

    update_remediation_progress(
        store,
        job_id,
        state="evaluating",
        note="Running HYPE ablation on all OOS folds",
        artifact_path="results/backtest/hype_ablation.json",
    )
    case = load_remediation(store, job_id)
    assert case and case["state"] == "evaluating"
    assert case["progress"]["artifact_path"].endswith("hype_ablation.json")


def test_quiet_recheck_backs_off_and_journals_compact_heartbeat(
    tmp_path: Path,
) -> None:
    """A no-change re-check doubles the next due time toward the cap and
    leaves a compact heartbeat in the journal, not a full feed entry."""
    store, job_id = _store(tmp_path)
    now = dt.datetime(2026, 8, 21, 12, tzinfo=dt.UTC)
    sync_remediation_with_health(store, job_id, _health(), now=now)

    wake = now + dt.timedelta(hours=6, minutes=1)
    retry = sync_remediation_with_health(store, job_id, _health(), now=wake)
    assert retry and retry["event"] == "regime_remediation_due"
    assert retry["material_reasons"] == ["open_case_without_proposal"]
    case = load_remediation(store, job_id)
    assert case and case["recheck"]["next_retry_seconds"] == 12 * 3600
    assert _journal_types(store, job_id).count("remediation_recheck_quiet") == 1

    # Backed off: the old flat 6h cadence no longer fires…
    assert (
        sync_remediation_with_health(
            store, job_id, _health(), now=wake + dt.timedelta(hours=6, minutes=1)
        )
        is None
    )
    # …but the doubled interval does, and stays pinned at the 12h cap.
    third = sync_remediation_with_health(
        store, job_id, _health(), now=wake + dt.timedelta(hours=12, minutes=1)
    )
    assert third and third["event"] == "regime_remediation_due"
    case = load_remediation(store, job_id)
    assert case and case["attempts"] == 3
    assert case["recheck"]["next_retry_seconds"] == 12 * 3600


def test_new_closed_trades_reset_backoff_to_prompt_recheck(tmp_path: Path) -> None:
    """Material forward evidence wakes the case promptly (30m base) through
    any accumulated backoff, with a full evidence-updated journal entry."""
    store, job_id = _store(tmp_path)
    now = dt.datetime(2026, 8, 21, 12, tzinfo=dt.UTC)
    sync_remediation_with_health(store, job_id, _health(), now=now)

    _record_closed_trades(store, job_id, 1)
    # Well inside the 6h quiet window — the evidence reset fires anyway.
    woke = sync_remediation_with_health(
        store, job_id, _health(), now=now + dt.timedelta(minutes=31)
    )
    assert woke and woke["event"] == "regime_remediation_due"
    assert "forward_trades_advanced" in woke["material_reasons"]
    assert "regime_remediation_evidence_updated" in _journal_types(store, job_id)
    case = load_remediation(store, job_id)
    assert case and case["recheck"]["next_retry_seconds"] == 3600

    # The ladder restarts from the prompt interval: quiet at +30m, due at +1h.
    wake = now + dt.timedelta(minutes=31)
    assert (
        sync_remediation_with_health(
            store, job_id, _health(), now=wake + dt.timedelta(minutes=31)
        )
        is None
    )
    quiet = sync_remediation_with_health(
        store, job_id, _health(), now=wake + dt.timedelta(hours=1, minutes=1)
    )
    assert quiet and quiet["material_reasons"] == ["open_case_without_proposal"]
    case = load_remediation(store, job_id)
    assert case and case["recheck"]["next_retry_seconds"] == 2 * 3600


def test_backoff_cap_env_and_max_quiet_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap is env-tunable, and even a mis-tuned cap can never silence a
    case past the 24h max-quiet bound."""
    monkeypatch.setenv("WAYFINDER_REMEDIATION_BACKOFF_CAP_SECONDS", str(48 * 3600))
    store, job_id = _store(tmp_path)
    now = dt.datetime(2026, 8, 21, 12, tzinfo=dt.UTC)
    sync_remediation_with_health(store, job_id, _health(), now=now)

    wake = now + dt.timedelta(hours=6, minutes=1)
    assert sync_remediation_with_health(store, job_id, _health(), now=wake)
    wake += dt.timedelta(hours=12, minutes=1)
    assert sync_remediation_with_health(store, job_id, _health(), now=wake)
    wake += dt.timedelta(hours=24, minutes=1)
    assert sync_remediation_with_health(store, job_id, _health(), now=wake)
    case = load_remediation(store, job_id)
    assert case and case["recheck"]["next_retry_seconds"] == 48 * 3600

    # next_retry says 48h, but the hard bound wakes the case at 24h.
    assert (
        sync_remediation_with_health(
            store, job_id, _health(), now=wake + dt.timedelta(hours=23)
        )
        is None
    )
    bounded = sync_remediation_with_health(
        store, job_id, _health(), now=wake + dt.timedelta(hours=24, minutes=1)
    )
    assert bounded and bounded["event"] == "regime_remediation_due"


def test_reaffirmation_notes_roll_into_single_note(tmp_path: Path) -> None:
    """Consecutive no-change re-affirmations update one rolling note and a
    compact heartbeat — they do not append full feed entries."""
    store, job_id = _store(tmp_path)
    sync_remediation_with_health(store, job_id, _health())

    update_remediation_progress(
        store, job_id, state="blocked", note="Waiting for forward evidence"
    )
    update_remediation_progress(
        store, job_id, state="blocked", note="Still waiting: book flat"
    )
    update_remediation_progress(
        store, job_id, state="blocked", note="Still waiting: book flat, no closes"
    )

    case = load_remediation(store, job_id)
    assert case and case["progress"]["reaffirmations"] == 2
    assert case["progress"]["note"].endswith("no closes")
    assert case["progress"]["first_recorded_at"]
    types = _journal_types(store, job_id)
    assert types.count("regime_remediation_progress") == 1
    assert types.count("remediation_recheck_quiet") == 2


def test_state_transition_or_new_evidence_writes_full_note(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path)
    sync_remediation_with_health(store, job_id, _health())

    update_remediation_progress(
        store, job_id, state="evaluating", note="Running ablation"
    )
    # evaluating → blocked is a transition: full entry.
    update_remediation_progress(
        store, job_id, state="blocked", note="Ablation blocked on evidence"
    )
    # New closed trades between notes: full entry even with the state unchanged.
    _record_closed_trades(store, job_id, 1)
    update_remediation_progress(
        store, job_id, state="blocked", note="One close landed; still short"
    )
    # No further change: compact.
    update_remediation_progress(
        store, job_id, state="blocked", note="No additional closes"
    )

    types = _journal_types(store, job_id)
    assert types.count("regime_remediation_progress") == 3
    assert types.count("remediation_recheck_quiet") == 1
    case = load_remediation(store, job_id)
    assert case and case["progress"]["reaffirmations"] == 1


def test_applied_treatment_monitors_until_health_recovers(tmp_path: Path) -> None:
    store, job_id = _store(tmp_path)
    sync_remediation_with_health(store, job_id, _health())
    proposal = {
        "proposal_id": "prop-disable-hype",
        "remediation": proposal_remediation_stamp(store, job_id),
        "candidate_report": {
            "gate": {"live_ready": True},
            "economic": {"ready": True},
        },
    }
    link_remediation_proposal(store, job_id, proposal)

    handle_remediation_application(store, job_id, proposal, status="applied")
    assert load_remediation(store, job_id)["state"] == "monitoring"  # type: ignore[index]

    sync_remediation_with_health(
        store,
        job_id,
        {**_health(), "status": "healthy", "score": 0, "signals": []},
    )
    assert load_remediation(store, job_id)["state"] == "resolved"  # type: ignore[index]
