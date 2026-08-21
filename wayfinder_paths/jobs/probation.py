"""Probation registry: durable, synced state for reduced-size trial legs.

Probation is only honest if its bookkeeping is visible: each leg's size cap,
pre-registered graduate/kill criteria, and progress live in one structured
file (`probation.json`) that rides the job snapshot to the backend — so the
owner watches the same numbers the worker updates, and graduation/kill are
journaled events, not prose."""

from __future__ import annotations

from typing import Any

from wayfinder_paths.jobs.improver.spec import ImproverSpec, revision_stamp
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore

PROBATION_PATH = "probation.json"
PROBATION_STATUSES = {"active", "graduated", "killed"}
PAPER_TIER = "paper"


def load_probation(store: JobStore, job_id: str) -> dict[str, Any]:
    return store.read_json(job_id, PROBATION_PATH, default={"legs": []}) or {"legs": []}


def record_probation_leg(
    store: JobStore,
    job_id: str,
    *,
    name: str,
    symbol: str,
    size_fraction: float,
    graduate_criterion: str,
    kill_criterion: str,
    graduate_rules: dict[str, Any] | None = None,
    kill_rules: dict[str, Any] | None = None,
    proposal_id: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    spec = ImproverSpec.load(store.job_dir(job_id))
    max_fraction = spec.probation_max_size_fraction
    max_legs = spec.probation_max_active_legs
    if not 0 < size_fraction <= max_fraction:
        raise ValueError(f"probation size_fraction must be in (0, {max_fraction}]")
    doc = load_probation(store, job_id)
    active = [leg for leg in doc["legs"] if leg.get("status") == "active"]
    if len(active) >= max_legs:
        raise ValueError(
            f"max {max_legs} concurrent probation legs — graduate or kill one first"
        )
    if any(leg.get("name") == name for leg in doc["legs"]):
        raise ValueError(f"probation leg {name!r} already exists")
    leg = {
        "name": name,
        "symbol": symbol,
        "status": "active",
        "deployed_at": utc_now_iso(),
        "size_fraction": float(size_fraction),
        "proposal_id": proposal_id,
        # criterion = human-readable rendering; rules = the machine-evaluable
        # predicates the lifecycle controller actually enforces. A leg without
        # rules is legacy: visible but never auto-graduated/killed.
        "graduate": {
            "criterion": graduate_criterion,
            "rules": dict(graduate_rules or {}),
            "progress": None,
        },
        "kill": {
            "criterion": kill_criterion,
            "rules": dict(kill_rules or {}),
            "status": None,
        },
        "notes": notes,
        **revision_stamp(store.job_dir(job_id)),
    }
    doc["legs"].append(leg)
    store.write_json(job_id, PROBATION_PATH, doc)
    store.append_journal(
        job_id,
        {"type": "probation_leg_opened", "leg": name, "proposal_id": proposal_id},
    )
    return leg


def paper_regression_budget(
    baseline_net: float, *, budget_pct: float, budget_frac: float
) -> float:
    """Allowed net_return giveback for paper entry: the larger of an absolute
    floor and a fraction of the baseline's own magnitude."""
    return max(float(budget_pct), float(budget_frac) * abs(float(baseline_net)))


def paper_entry_check(
    *,
    candidate_net: float,
    baseline_net: float,
    backtest_trades: int,
    spec: ImproverSpec,
) -> dict[str, Any]:
    """Mechanical "not clearly worse" test for the paper probation tier."""
    budget = paper_regression_budget(
        baseline_net,
        budget_pct=spec.paper_regression_budget_pct,
        budget_frac=spec.paper_regression_budget_frac,
    )
    min_trades = spec.paper_min_backtest_trades
    reasons: list[str] = []
    if int(backtest_trades) < min_trades:
        reasons.append(
            f"backtest trade count {backtest_trades} below floor {min_trades}"
        )
    floor = float(baseline_net) - budget
    if float(candidate_net) < floor:
        reasons.append(
            f"candidate net_return {candidate_net} clearly worse than baseline "
            f"{baseline_net} (allowed floor {round(floor, 6)})"
        )
    return {
        "eligible": not reasons,
        "budget": round(budget, 6),
        "floor": round(floor, 6),
        "reasons": reasons,
    }


def _comparison_nets(
    store: JobStore, job_id: str, proposal_id: str
) -> tuple[float, float, int]:
    proposal = store.load_proposal(job_id, proposal_id)
    comparison = (proposal.get("candidate_report") or {}).get("comparison") or {}
    candidate_stats = (comparison.get("candidate") or {}).get("stats") or {}
    baseline_stats = (comparison.get("baseline") or {}).get("stats") or {}
    if "net_return" not in candidate_stats or "net_return" not in baseline_stats:
        raise ValueError(
            f"proposal {proposal_id} has no baseline-vs-candidate net_return "
            "comparison — paper entry needs the propose-time backtest"
        )
    return (
        float(candidate_stats["net_return"]),
        float(baseline_stats["net_return"]),
        int(candidate_stats.get("trade_count") or 0),
    )


def open_paper_probation_leg(
    store: JobStore,
    job_id: str,
    *,
    name: str,
    symbol: str,
    kill_criterion: str,
    kill_rules: dict[str, Any] | None = None,
    graduate_criterion: str = "full strict gate + owner approval (unchanged)",
    graduate_rules: dict[str, Any] | None = None,
    proposal_id: str | None = None,
    candidate_net: float | None = None,
    baseline_net: float | None = None,
    backtest_trades: int | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Open a PAPER probation leg for a candidate that is "not clearly worse"
    than baseline — no baseline beat and no owner approval required, because
    probation is the containment: paper only (size_fraction 0.0, never live
    sizing), capped concurrency, and outcome-driven retirement (registered
    kill predicates PLUS the mechanical flat-zero floor the lifecycle
    controller enforces on every paper leg). Graduation to live is UNCHANGED:
    a paper leg's results feed a normal proposal through the full strict gate
    with owner approval — nothing in probation.json confers live execution.

    Entry evidence comes from the proposal's propose-time comparison
    (`proposal_id`) or explicit candidate/baseline `net_return` figures on
    the same full-history window.
    """
    if proposal_id is not None and candidate_net is None:
        candidate_net, baseline_net, backtest_trades = _comparison_nets(
            store, job_id, proposal_id
        )
    if candidate_net is None or baseline_net is None:
        raise ValueError(
            "paper entry needs candidate/baseline net_return — pass "
            "proposal_id or explicit figures"
        )
    spec = ImproverSpec.load(store.job_dir(job_id))
    check = paper_entry_check(
        candidate_net=candidate_net,
        baseline_net=baseline_net,
        backtest_trades=int(backtest_trades or 0),
        spec=spec,
    )
    if not check["eligible"]:
        # A mechanical refusal is a completed candidate evaluation, not an
        # unattempted paper deployment. Keep it durable so coverage audits do
        # not repeatedly mandate a candidate the entry gate already rejected.
        store.append_journal(
            job_id,
            {
                "type": "paper_probation_entry_refused",
                "leg": name,
                "symbol": symbol,
                "proposal_id": proposal_id,
                "entry": {
                    "candidate_net_return": float(candidate_net),
                    "baseline_net_return": float(baseline_net),
                    "backtest_trades": int(backtest_trades or 0),
                    **check,
                },
            },
        )
        raise ValueError(
            "paper probation entry refused: " + "; ".join(check["reasons"])
        )
    doc = load_probation(store, job_id)
    paper_active = [
        leg
        for leg in doc["legs"]
        if leg.get("status") == "active" and leg.get("tier") == PAPER_TIER
    ]
    if len(paper_active) >= spec.paper_max_active_legs:
        raise ValueError(
            f"max {spec.paper_max_active_legs} concurrent paper probation "
            "legs — retire one first"
        )
    if any(leg.get("name") == name for leg in doc["legs"]):
        raise ValueError(f"probation leg {name!r} already exists")
    leg = {
        "name": name,
        "symbol": symbol,
        "status": "active",
        "tier": PAPER_TIER,
        "opened_by": "improver",
        "deployed_at": utc_now_iso(),
        # Paper legs never carry live sizing; retirement/graduation evidence
        # is the forward paper stream, adjudicated by the controller.
        "size_fraction": 0.0,
        "proposal_id": proposal_id,
        "entry": {
            "candidate_net_return": float(candidate_net),
            "baseline_net_return": float(baseline_net),
            "backtest_trades": int(backtest_trades or 0),
            **check,
        },
        "graduate": {
            "criterion": graduate_criterion,
            "rules": dict(graduate_rules or {}),
            "progress": None,
        },
        "kill": {
            "criterion": kill_criterion,
            "rules": dict(kill_rules or {}),
            "status": None,
        },
        "notes": notes,
        **revision_stamp(store.job_dir(job_id)),
    }
    doc["legs"].append(leg)
    store.write_json(job_id, PROBATION_PATH, doc)
    store.append_journal(
        job_id,
        {
            "type": "paper_probation_opened",
            "leg": name,
            "symbol": symbol,
            "proposal_id": proposal_id,
            "entry": leg["entry"],
        },
    )
    return leg


def update_probation_leg(
    store: JobStore,
    job_id: str,
    name: str,
    *,
    progress: str | None = None,
    kill_status: str | None = None,
    status: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    doc = load_probation(store, job_id)
    leg = next((leg for leg in doc["legs"] if leg.get("name") == name), None)
    if leg is None:
        raise ValueError(f"unknown probation leg {name!r}")
    if progress is not None:
        leg["graduate"]["progress"] = progress
    if kill_status is not None:
        leg["kill"]["status"] = kill_status
    if notes is not None:
        leg["notes"] = notes
    if status is not None:
        if status not in PROBATION_STATUSES:
            raise ValueError(f"status must be one of {sorted(PROBATION_STATUSES)}")
        previous = leg.get("status")
        leg["status"] = status
        if status != previous and status in {"graduated", "killed"}:
            leg["closed_at"] = utc_now_iso()
            store.append_journal(
                job_id, {"type": f"probation_leg_{status}", "leg": name}
            )
    leg["updated_at"] = utc_now_iso()
    store.write_json(job_id, PROBATION_PATH, doc)
    return leg
