"""Progress constitution: escalating staleness, successor re-arm,
owner-adjudicated exhaustion, and the paper probation entry tier.

Motivating incident: all three production research jobs' lanes ended in agent
SELF-rejections, then froze. Staleness computed correctly every wake, but the
mandate's "state why research is not warranted" hatch let the agent close
every stale wake with prose; the successor watchdog notified once and counted
a self-rejected proposal as delivery; and the dead map recorded self-rejected
lanes as FULLY SETTLED behind a "requires named new evidence" bar while
experiments — the only generator of such evidence — were withheld. The
asymmetry under test: nothing inside the loop may control the only evidence
used for its own acceptance, and a forced-progress quota always ships with a
retirement floor.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wayfinder_paths.jobs.exhaustion import (
    adjudicate_exhaustion_claim,
    claim_settles_lane,
    file_exhaustion_claim,
    list_exhaustion_claims,
)
from wayfinder_paths.jobs.models import WayfinderJob, utc_now_iso
from wayfinder_paths.jobs.probation import (
    load_probation,
    open_paper_probation_leg,
    paper_entry_check,
)
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.triggers import ALWAYS_WAKE_EVENTS
from wayfinder_paths.jobs.watchdog import recover_stalled_applications
from wayfinder_paths.jobs.worker import prepare_job_worker_prompt


def _make_store(tmp_path: Path, job_id: str = "progress-demo") -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        job_id,
        script="workspace/src/strategy.py",
        agent_mode="intervene",
        execution_contract="jobs_v1",
    )
    store.save(job)
    return store, job.id


def _journal_events(store: JobStore, job_id: str, event_type: str) -> list[dict]:
    path = store.job_dir(job_id) / "journal.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    return [row for row in rows if row.get("type") == event_type]


@pytest.fixture
def wakes(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_worker(job_id: str, *, mode: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"job_id": job_id, "mode": mode, **kwargs})
        return {"status": "queued"}

    monkeypatch.setattr("wayfinder_paths.jobs.worker.run_job_worker", fake_worker)
    return calls


def _append_wakes(store: JobStore, job_id: str, count: int) -> None:
    for _ in range(count):
        store.append_journal(job_id, {"type": "agent_wakeup"})


def _write_experiment_row(store: JobStore, job_id: str, ts: str) -> None:
    path = store.job_dir(job_id) / "results" / "backtest" / "experiments.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": ts, "run_id": "exp-1"}) + "\n")


# ---------------------------------------------------------------------------
# Piece 1: escalating staleness — research_impasse standing check


def test_research_impasse_fires_once_and_wakes_agent(
    tmp_path: Path, wakes: list[dict[str, Any]]
) -> None:
    store, job_id = _make_store(tmp_path, "impasse-fire")
    _append_wakes(store, job_id, 3)

    result = recover_stalled_applications(store=store)
    events = [e for e in result["recovered"] if e.get("action") == "research_impasse"]
    assert len(events) == 1
    assert len(_journal_events(store, job_id, "research_impasse")) == 1
    assert wakes and wakes[0]["job_id"] == job_id
    assert "research_impasse" in ALWAYS_WAKE_EVENTS
    marker = store.read_json(job_id, "state/research_impasse.json")
    assert marker["alerted_at"]

    # Second pass inside the re-alert window: debounced, no duplicate.
    result = recover_stalled_applications(store=store)
    assert not [
        e for e in result["recovered"] if e.get("action") == "research_impasse"
    ]
    assert len(_journal_events(store, job_id, "research_impasse")) == 1


def test_research_impasse_needs_k_stale_wakes(
    tmp_path: Path, wakes: list[dict[str, Any]]
) -> None:
    store, job_id = _make_store(tmp_path, "impasse-few")
    _append_wakes(store, job_id, 2)  # below the K=3 default
    result = recover_stalled_applications(store=store)
    assert not [
        e for e in result["recovered"] if e.get("action") == "research_impasse"
    ]
    assert not wakes


def test_research_impasse_respects_staleness_thresholds(
    tmp_path: Path, wakes: list[dict[str, Any]]
) -> None:
    store, job_id = _make_store(tmp_path, "impasse-fresh")
    # An experiment BEFORE the recent wakes (no progress since them) but well
    # inside the staleness window: quiet, not an impasse.
    _write_experiment_row(
        store, job_id, (datetime.now(UTC) - timedelta(days=1)).isoformat()
    )
    _append_wakes(store, job_id, 3)
    result = recover_stalled_applications(store=store)
    assert not [
        e for e in result["recovered"] if e.get("action") == "research_impasse"
    ]


def test_research_impasse_resolves_on_progress(
    tmp_path: Path, wakes: list[dict[str, Any]]
) -> None:
    store, job_id = _make_store(tmp_path, "impasse-resolve")
    _append_wakes(store, job_id, 3)
    recover_stalled_applications(store=store)
    assert store.read_json(job_id, "state/research_impasse.json")["alerted_at"]

    # A real progress artifact appears (probation leg opened after the wakes).
    store.append_journal(job_id, {"type": "probation_leg_opened", "leg": "x"})
    result = recover_stalled_applications(store=store)
    resolved = [
        e
        for e in result["recovered"]
        if e.get("action") == "research_impasse_resolved"
    ]
    assert len(resolved) == 1
    assert not store.read_json(job_id, "state/research_impasse.json")
    assert _journal_events(store, job_id, "research_impasse_resolved")


def test_impasse_wake_carries_hatch_stripped_mandate(tmp_path: Path) -> None:
    store, job_id = _make_store(tmp_path, "impasse-prompt")
    store.write_json(
        job_id,
        "state/research_impasse.json",
        {"alerted_at": utc_now_iso(), "stale_wakes": 3},
    )
    sections = prepare_job_worker_prompt(store=store, job_id=job_id, mode="intervene")
    assert "RESEARCH IMPASSE" in sections["dynamic_context"]
    assert "DIVERSITY move" in sections["dynamic_context"]
    assert "exhaustion claim" in sections["dynamic_context"]
    # No marker → no directive.
    store.write_json(job_id, "state/research_impasse.json", {})
    sections = prepare_job_worker_prompt(store=store, job_id=job_id, mode="intervene")
    assert "RESEARCH IMPASSE" not in sections["dynamic_context"]


# ---------------------------------------------------------------------------
# Piece 2: successor re-arm — a self-rejected successor delivers nothing


def _expect_successor(store: JobStore, job_id: str, *, hours_ago: float) -> str:
    ts = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    store.write_json(
        job_id,
        "state/successor_expected.json",
        [{"proposal_id": "prop-old", "ts": ts, "reason": "superseded"}],
    )
    return ts


def _self_rejected_successor(
    store: JobStore, job_id: str, *, pid: str, hours_ago: float
) -> None:
    ts = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    store.write_proposal(
        job_id,
        {
            "proposal_id": pid,
            "status": "rejected",
            "application": {"status": "not_requested"},
            "candidate_report": {"generated_at": ts},
            "rejection": {"by": "agent", "kind": "process", "ts": ts},
        },
    )


def test_self_rejected_successor_rearms_then_overdue_fires_again(
    tmp_path: Path, wakes: list[dict[str, Any]]
) -> None:
    store, job_id = _make_store(tmp_path, "succ-rearm")
    _expect_successor(store, job_id, hours_ago=14)
    _self_rejected_successor(store, job_id, pid="prop-selfrej", hours_ago=13)

    # Pass 1: the self-rejection re-arms the expectation instead of counting
    # as delivery — the production thread died exactly here.
    result = recover_stalled_applications(store=store)
    rearms = [e for e in result["recovered"] if e.get("action") == "successor_rearmed"]
    assert len(rearms) == 1
    assert _journal_events(store, job_id, "successor_rearmed")
    entry = store.read_json(job_id, "state/successor_expected.json")[0]
    assert entry["rearms"] == 1
    assert entry["notified"] is False
    assert entry["rearmed_for"] == ["prop-selfrej"]
    assert not _journal_events(store, job_id, "successor_overdue")

    # Pass 2: the re-armed window (restarted at the 13h-old rejection) is
    # already overdue → the invitation wakes the agent again.
    result = recover_stalled_applications(store=store)
    overdue = [
        e for e in result["recovered"] if e.get("action") == "successor_overdue"
    ]
    assert len(overdue) == 1
    assert wakes and wakes[-1]["job_id"] == job_id

    # Pass 3: notified, no new self-rejections → silent.
    result = recover_stalled_applications(store=store)
    assert not [
        e
        for e in result["recovered"]
        if e.get("action") in {"successor_overdue", "successor_rearmed"}
    ]


def test_successor_rearm_bounded_then_abandoned(
    tmp_path: Path, wakes: list[dict[str, Any]]
) -> None:
    store, job_id = _make_store(tmp_path, "succ-abandon")
    _expect_successor(store, job_id, hours_ago=14)
    expected = store.read_json(job_id, "state/successor_expected.json")
    expected[0]["rearms"] = 3
    expected[0]["rearmed_for"] = ["prop-r1", "prop-r2", "prop-r3"]
    store.write_json(job_id, "state/successor_expected.json", expected)
    _self_rejected_successor(store, job_id, pid="prop-selfrej-4", hours_ago=1)

    result = recover_stalled_applications(store=store)
    abandoned = [
        e for e in result["recovered"] if e.get("action") == "successor_abandoned"
    ]
    assert len(abandoned) == 1
    events = _journal_events(store, job_id, "successor_abandoned")
    assert len(events) == 1
    assert "owner" in events[0]["owner_review_required"]
    assert store.read_json(job_id, "state/successor_expected.json")[0]["abandoned"]

    # Terminal: no duplicate on the next pass.
    result = recover_stalled_applications(store=store)
    assert not [
        e for e in result["recovered"] if e.get("action") == "successor_abandoned"
    ]
    assert len(_journal_events(store, job_id, "successor_abandoned")) == 1


def test_alive_successor_marks_delivered(
    tmp_path: Path, wakes: list[dict[str, Any]]
) -> None:
    store, job_id = _make_store(tmp_path, "succ-alive")
    _expect_successor(store, job_id, hours_ago=14)
    store.write_proposal(
        job_id,
        {
            "proposal_id": "prop-alive",
            "status": "pending",
            "application": {"status": "not_requested"},
            "candidate_report": {"generated_at": datetime.now(UTC).isoformat()},
        },
    )
    result = recover_stalled_applications(store=store)
    assert not [
        e for e in result["recovered"] if str(e.get("action", "")).startswith("successor")
    ]
    assert store.read_json(job_id, "state/successor_expected.json")[0]["delivered"]
    assert not wakes


def test_audit_progress_counts_as_successor_delivery(
    tmp_path: Path, wakes: list[dict[str, Any]]
) -> None:
    store, job_id = _make_store(tmp_path, "succ-audit")
    _expect_successor(store, job_id, hours_ago=14)
    _write_experiment_row(store, job_id, datetime.now(UTC).isoformat())
    result = recover_stalled_applications(store=store)
    assert not [
        e for e in result["recovered"] if str(e.get("action", "")).startswith("successor")
    ]
    assert store.read_json(job_id, "state/successor_expected.json")[0]["delivered"]
    assert not wakes


# ---------------------------------------------------------------------------
# Piece 3: owner-adjudicated exhaustion claims


def test_exhaustion_claim_lifecycle_and_owner_only_accept(tmp_path: Path) -> None:
    store, job_id = _make_store(tmp_path, "claims-demo")
    claim = file_exhaustion_claim(
        store,
        job_id,
        lane="IMX gap filter",
        evidence="34 grid cells + 4 WF folds all negative",
        provenance="holdout-refuted",
        next_region="cross-asset funding regime",
        refs=["results/backtest/experiments.jsonl"],
    )
    assert claim["status"] == "pending"
    assert _journal_events(store, job_id, "exhaustion_claim_filed")
    scorecard = store.read_json(job_id, "scorecard.json")
    assert scorecard["pending_exhaustion_claims"] == 1

    # Agents can FILE, never accept.
    with pytest.raises(PermissionError):
        adjudicate_exhaustion_claim(
            store, job_id, claim["claim_id"], status="accepted", by="agent"
        )
    assert (
        list_exhaustion_claims(store, job_id, status="pending")[0]["status"]
        == "pending"
    )
    assert _journal_events(store, job_id, "exhaustion_claim_accept_refused")

    accepted = adjudicate_exhaustion_claim(
        store, job_id, claim["claim_id"], status="accepted", by="owner", note="agreed"
    )
    assert accepted["status"] == "accepted"
    assert accepted["adjudication"]["by"] == "owner"
    assert _journal_events(store, job_id, "exhaustion_claim_accepted")
    assert store.read_json(job_id, "scorecard.json")["pending_exhaustion_claims"] == 0
    # Terminal: cannot re-adjudicate.
    with pytest.raises(ValueError, match="already accepted"):
        adjudicate_exhaustion_claim(
            store, job_id, claim["claim_id"], status="rejected", by="owner"
        )


def test_exhaustion_claim_requires_full_shape(tmp_path: Path) -> None:
    store, job_id = _make_store(tmp_path, "claims-shape")
    with pytest.raises(ValueError, match="provenance"):
        file_exhaustion_claim(
            store, job_id, lane="x", evidence="e", provenance="vibes", next_region="y"
        )
    with pytest.raises(ValueError, match="next region"):
        file_exhaustion_claim(
            store,
            job_id,
            lane="x",
            evidence="e",
            provenance="data-wall",
            next_region="",
        )
    with pytest.raises(ValueError, match="evidence"):
        file_exhaustion_claim(
            store, job_id, lane="x", evidence=" ", provenance="data-wall", next_region="y"
        )


def test_agent_self_rejected_provenance_never_settles(tmp_path: Path) -> None:
    store, job_id = _make_store(tmp_path, "claims-prov")
    settled = file_exhaustion_claim(
        store,
        job_id,
        lane="lane-a",
        evidence="holdout negative",
        provenance="holdout-refuted",
        next_region="lane-b",
    )
    assert claim_settles_lane(settled)  # pending settles
    self_rej = file_exhaustion_claim(
        store,
        job_id,
        lane="lane-c",
        evidence="I rejected my own proposals",
        provenance="agent-self-rejected",
        next_region="lane-d",
    )
    assert not claim_settles_lane(self_rej)
    accepted = adjudicate_exhaustion_claim(
        store, job_id, self_rej["claim_id"], status="accepted", by="owner"
    )
    # Even owner-accepted, self-rejection provenance cannot settle a lane.
    assert not claim_settles_lane(accepted)


def test_watchdog_surfaces_pending_claims_once(
    tmp_path: Path, wakes: list[dict[str, Any]]
) -> None:
    store, job_id = _make_store(tmp_path, "claims-surface")
    file_exhaustion_claim(
        store,
        job_id,
        lane="lane-a",
        evidence="e",
        provenance="data-wall",
        next_region="lane-b",
    )
    result = recover_stalled_applications(store=store)
    surfaced = [
        e for e in result["recovered"] if e.get("action") == "exhaustion_claims_pending"
    ]
    assert len(surfaced) == 1 and surfaced[0]["count"] == 1
    assert len(_journal_events(store, job_id, "exhaustion_claims_pending")) == 1
    # Unchanged queue → silent next pass.
    result = recover_stalled_applications(store=store)
    assert not [
        e for e in result["recovered"] if e.get("action") == "exhaustion_claims_pending"
    ]


# ---------------------------------------------------------------------------
# Piece 4: paper probation entry tier


def test_paper_entry_check_regression_budget() -> None:
    from wayfinder_paths.jobs.improver.spec import ImproverSpec

    spec = ImproverSpec.load(Path("/nonexistent"))  # defaults
    # budget = max(0.02, 0.25*|0.10|) = 0.025 → floor 0.075
    ok = paper_entry_check(
        candidate_net=0.08, baseline_net=0.10, backtest_trades=30, spec=spec
    )
    assert ok["eligible"] and ok["budget"] == 0.025
    worse = paper_entry_check(
        candidate_net=0.07, baseline_net=0.10, backtest_trades=30, spec=spec
    )
    assert not worse["eligible"]
    thin = paper_entry_check(
        candidate_net=0.09, baseline_net=0.10, backtest_trades=3, spec=spec
    )
    assert not thin["eligible"] and "floor" in thin["reasons"][0]


def _open_paper(
    store: JobStore, job_id: str, name: str, symbol: str = "HYPE", **overrides: Any
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "candidate_net": 0.09,
        "baseline_net": 0.10,
        "backtest_trades": 25,
        "kill_criterion": "kill on WR<20% after 10",
        "kill_rules": {"win_rate__lt": 0.2, "min_closed_trades": 10},
        **overrides,
    }
    return open_paper_probation_leg(store, job_id, name=name, symbol=symbol, **kwargs)


def test_paper_leg_opens_within_budget_and_caps(tmp_path: Path) -> None:
    store, job_id = _make_store(tmp_path, "paper-open")
    leg = _open_paper(store, job_id, "leg-a")
    assert leg["tier"] == "paper"
    assert leg["opened_by"] == "improver"
    assert leg["size_fraction"] == 0.0  # paper only — never live sizing
    assert _journal_events(store, job_id, "paper_probation_opened")
    assert load_probation(store, job_id)["legs"][0]["name"] == "leg-a"

    with pytest.raises(ValueError, match="clearly worse"):
        _open_paper(store, job_id, "leg-worse", candidate_net=0.05)
    with pytest.raises(ValueError, match="trade count"):
        _open_paper(store, job_id, "leg-thin", backtest_trades=2)

    _open_paper(store, job_id, "leg-b")
    _open_paper(store, job_id, "leg-c")
    with pytest.raises(ValueError, match="concurrent paper"):
        _open_paper(store, job_id, "leg-d")


def test_paper_leg_from_proposal_comparison(tmp_path: Path) -> None:
    store, job_id = _make_store(tmp_path, "paper-prop")
    store.write_proposal(
        job_id,
        {
            "proposal_id": "prop-paper",
            "status": "pending",
            "application": {"status": "not_requested"},
            "candidate_report": {
                "comparison": {
                    "candidate": {"stats": {"net_return": 0.09, "trade_count": 25}},
                    "baseline": {"stats": {"net_return": 0.10, "trade_count": 24}},
                }
            },
        },
    )
    leg = open_paper_probation_leg(
        store,
        job_id,
        name="leg-prop",
        symbol="HYPE",
        kill_criterion="registered rules + flat-zero floor",
        proposal_id="prop-paper",
    )
    assert leg["entry"]["candidate_net_return"] == 0.09
    assert leg["entry"]["backtest_trades"] == 25
    # No comparison → refused, never silently opened.
    store.write_proposal(
        job_id,
        {
            "proposal_id": "prop-bare",
            "status": "pending",
            "application": {"status": "not_requested"},
        },
    )
    with pytest.raises(ValueError, match="comparison"):
        open_paper_probation_leg(
            store,
            job_id,
            name="leg-bare",
            symbol="HYPE",
            kill_criterion="x",
            proposal_id="prop-bare",
        )


def _write_forward_trades(
    store: JobStore, job_id: str, symbol: str, pnls: list[float]
) -> None:
    path = store.job_dir(job_id) / "results" / "forward" / "trades.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).isoformat()
    with path.open("a", encoding="utf-8") as handle:
        for pnl in pnls:
            handle.write(
                json.dumps({"symbol": symbol, "net_pnl": pnl, "closed_at": ts}) + "\n"
            )


def test_paper_floor_retires_negative_leg_keeps_positive(
    tmp_path: Path, wakes: list[dict[str, Any]]
) -> None:
    store, job_id = _make_store(tmp_path, "paper-floor")
    _open_paper(store, job_id, "leg-neg", symbol="NEG")
    _open_paper(store, job_id, "leg-pos", symbol="POS")
    # NEG underperforms flat-zero past the floor window; POS is positive.
    _write_forward_trades(store, job_id, "NEG", [-1.0, -2.0, 1.0, -3.0, -1.0])
    _write_forward_trades(store, job_id, "POS", [1.0, 2.0, -0.5, 1.5, 0.5])

    recover_stalled_applications(store=store)
    legs = {leg["name"]: leg for leg in load_probation(store, job_id)["legs"]}
    assert legs["leg-neg"]["status"] == "killed"
    assert legs["leg-pos"]["status"] == "active"
    decisions = _journal_events(store, job_id, "lifecycle_decision")
    floor_checks = [
        check
        for event in decisions
        for check in event["checks"]
        if check and check.get("rule") == "paper_flat_zero_floor"
    ]
    assert floor_checks and floor_checks[0]["closed_trades"] >= 5


def test_paper_floor_waits_for_min_trades(
    tmp_path: Path, wakes: list[dict[str, Any]]
) -> None:
    store, job_id = _make_store(tmp_path, "paper-wait")
    _open_paper(store, job_id, "leg-early", symbol="EARLY")
    _write_forward_trades(store, job_id, "EARLY", [-1.0, -2.0])  # below floor window
    recover_stalled_applications(store=store)
    legs = {leg["name"]: leg for leg in load_probation(store, job_id)["legs"]}
    assert legs["leg-early"]["status"] == "active"


# ---------------------------------------------------------------------------
# Piece 5: the wake mandate text is pinned — the escape hatch stays dead


def test_wake_mandate_hatch_stripped_and_pinned(tmp_path: Path) -> None:
    store, job_id = _make_store(tmp_path, "mandate-pin")
    sections = prepare_job_worker_prompt(store=store, job_id=job_id, mode="intervene")
    prompt = sections["stable_prefix"]
    assert "PROGRESS CONSTITUTION" in prompt
    assert "exactly ONE of" in prompt
    assert "exhaustion claim FILED for owner adjudication" in prompt
    assert "Self-rejections are development evidence" in prompt
    assert "agent-self-" in prompt
    assert "NOT a legal outcome" in prompt
    # The escape hatch is gone: prose can no longer close a stale wake.
    assert "why research is not warranted" not in sections["prompt"]
    assert "MUST advance one research lane" not in sections["prompt"]
