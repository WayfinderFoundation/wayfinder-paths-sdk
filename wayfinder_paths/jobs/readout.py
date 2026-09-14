"""The honest readout: what the evidence says about a job before launch.

For a harnessed (jobs_v1) job the evidence is the backtest, the chronologically
last walk-forward fold (the small holdout), out-of-sample fold counts and
decay, replication against the refreshed dataset, robustness warnings and
cost coverage. The verdict names the rule that fired; it never blocks a paper
launch. For freestyle and path jobs there is no backtest and the readout says
so in one fixed sentence, then shows what the dry run actually did.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.execution.experiments import list_experiments
from wayfinder_paths.jobs.execution.op_process import recorded_process_alive
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.replication import load_replication
from wayfinder_paths.jobs.robustness import latest_robustness_summary
from wayfinder_paths.jobs.store import JobStore

READOUT_PATH = "reports/readout/latest.json"
NO_CLAIM_SENTENCE = (
    "no backtest exists for this script; nothing here is a performance claim"
)
FEES_TO_GROSS_LIMIT = 0.30
DECAY_FLOOR = 0.5
DEFAULT_INITIAL_CAPITAL = 10_000.0


def build_readout(job_id: str, *, store: JobStore | None = None) -> dict[str, Any]:
    store = store or JobStore()
    job = store.load(job_id)
    root = store.job_dir(job_id)
    contract = str(job.execution_contract or "legacy")
    validation = (
        store.read_json(job_id, "reports/validation/latest.json", default=None) or {}
    )
    pending = _pending_ops(root)
    readout: dict[str, Any] = {
        "job_id": job_id,
        "kind": contract,
        "revision": compute_workspace_revision(root),
        "validation": {
            "status": validation.get("status"),
            "revision": validation.get("revision"),
        },
        "launch_allowed": validation.get("status") == "passed",
        "pending_ops": pending,
        "reasons": [],
        "missing": [],
        "generated_at": utc_now_iso(),
    }
    if contract == "jobs_v1":
        readout.update(_jobs_v1_readout(store, job_id, root, job, pending))
    else:
        readout.update(_script_readout(validation, pending))
    store.write_json(job_id, READOUT_PATH, readout)
    return readout


def _jobs_v1_readout(
    store: JobStore, job_id: str, root: Path, job: Any, pending: list[str]
) -> dict[str, Any]:
    reasons: list[str] = []
    missing: list[str] = []
    backtest = (
        store.read_json(job_id, "results/backtest/latest.json", default=None) or {}
    )
    stats = dict(
        backtest.get("stats") or (backtest.get("result") or {}).get("stats") or {}
    )
    evidence: dict[str, Any] = {}
    if backtest:
        dataset = dict(backtest.get("dataset") or {})
        evidence["backtest"] = {
            "revision": backtest.get("revision"),
            "generated_at": backtest.get("generated_at"),
            "at_current_revision": backtest.get("revision")
            == compute_workspace_revision(root),
            "stats": stats,
            "window_days": dataset.get("days_received") or dataset.get("days"),
            "execution_valid": bool(
                (backtest.get("validation") or {}).get("execution_valid")
            ),
        }
    else:
        missing.append("backtest")
    walk_forward = _latest_walk_forward(store, job_id)
    if walk_forward:
        evidence["holdout"] = walk_forward
    else:
        missing.append("walk_forward")
    replication = load_replication(store, job_id) or {}
    if replication:
        evidence["replication"] = {
            "status": replication.get("status"),
            "decayed": bool(replication.get("decayed")),
            "baseline": replication.get("baseline"),
            "current": replication.get("current"),
        }
    else:
        missing.append("replication")
    try:
        robustness = latest_robustness_summary(
            store,
            job_id,
            candidate_revision=compute_workspace_revision(root),
            candidate_dir=root,
        )
    except Exception:  # noqa: BLE001 — advisory feed, never fatal
        robustness = {"status": "not_run", "advisory": True}
    evidence["robustness"] = {
        "status": robustness.get("status"),
        "warnings": robustness.get("warnings") or [],
    }
    if robustness.get("status") == "not_run":
        missing.append("robustness")
    capital = float(
        (job.execution_params or {}).get("initial_capital") or DEFAULT_INITIAL_CAPITAL
    )
    evidence["cost_coverage"] = _cost_coverage(stats, capital)
    starter_evidence = store.read_json(
        job_id, "results/backtest/starter_evidence.json", default=None
    )
    if starter_evidence:
        evidence["starter_evidence"] = starter_evidence

    verdict = _verdict(
        stats,
        walk_forward,
        replication,
        evidence["cost_coverage"],
        pending,
        backtest,
        reasons,
    )
    return {
        "verdict": verdict,
        "evidence": evidence,
        "reasons": reasons,
        "missing": missing,
    }


def _verdict(
    stats: dict[str, Any],
    walk_forward: dict[str, Any] | None,
    replication: dict[str, Any],
    cost: dict[str, Any],
    pending: list[str],
    backtest: dict[str, Any],
    reasons: list[str],
) -> str:
    if pending and not backtest:
        reasons.append(f"evidence is still being computed: {', '.join(pending)}")
        return "pending"
    if not backtest:
        reasons.append("no backtest artifact yet")
        return "no_backtest"
    net_return = _float(stats.get("net_return"))
    if net_return is not None and net_return <= 0:
        reasons.append(f"net return after costs is {net_return:.2%}")
        return "not_supported_by_backtest"
    fees_to_gross = _float(cost.get("fees_to_gross"))
    if fees_to_gross is not None and fees_to_gross >= FEES_TO_GROSS_LIMIT:
        reasons.append(f"fees take {fees_to_gross:.0%} of gross profit")
        return "not_supported_by_backtest"
    summary = dict((walk_forward or {}).get("summary") or {})
    oos_mean = _float(summary.get("oos_return_mean"))
    if walk_forward and oos_mean is not None and oos_mean <= 0:
        reasons.append(f"out-of-sample folds average {oos_mean:.2%}")
        return "not_supported_by_backtest"
    weak = False
    if not walk_forward:
        reasons.append("no walk-forward holdout yet: the backtest is in-sample only")
        weak = True
    else:
        folds = int(summary.get("fold_count") or 0)
        positive = int(summary.get("oos_positive_folds") or 0)
        if folds and positive * 2 <= folds:
            reasons.append(
                f"only {positive} of {folds} out-of-sample folds are positive"
            )
            weak = True
        decay = _float(summary.get("decay_ratio"))
        if decay is not None and decay < DECAY_FLOOR:
            reasons.append(f"out-of-sample return keeps {decay:.0%} of in-sample")
            weak = True
    if replication.get("decayed"):
        reasons.append(
            "replication on the refreshed dataset lost more than half the return"
        )
        weak = True
    if pending:
        reasons.append(f"newer evidence is still being computed: {', '.join(pending)}")
    if weak:
        return "weak"
    reasons.append(
        "net return positive after costs, out-of-sample folds mostly positive, replication holds"
    )
    return "supported"


def _script_readout(validation: dict[str, Any], pending: list[str]) -> dict[str, Any]:
    section = dict(validation.get("freestyle") or {})
    path_section = dict(validation.get("path") or {})
    dry_run = dict(section.get("dry_run") or path_section.get("dry_run") or {})
    evidence = {
        "dry_run": dry_run,
        "spec": section.get("spec") or {},
        "action_ledger_preview": [
            {
                "symbol": (a.get("intent") or {}).get("symbol"),
                "venue": (a.get("intent") or {}).get("venue"),
                "status": a.get("status"),
                "reason": a.get("reason"),
            }
            for a in dry_run.get("actions") or []
        ],
        "paper_capable": validation.get("paper_capable", True),
        "path": {
            k: v
            for k, v in path_section.items()
            if k in {"pin", "component_kind", "dry_run_declared"}
        },
    }
    reasons = [NO_CLAIM_SENTENCE]
    if validation.get("paper_capable") is False:
        reasons.append(
            "this component has no paper mode; it runs live only after the no_dry_run flag is acknowledged"
        )
    return {
        "verdict": "no_backtest",
        "performance_claim": None,
        "evidence": evidence,
        "reasons": reasons,
        "missing": [] if validation else ["validation"],
    }


def _latest_walk_forward(store: JobStore, job_id: str) -> dict[str, Any] | None:
    for row in reversed(list_experiments(job_id, store=store)):
        walk_forward = row.get("walk_forward")
        if not walk_forward:
            continue
        folds = list(walk_forward.get("folds") or [])
        last = folds[-1] if folds else {}
        return {
            "grid_id": row.get("grid_id"),
            "recorded_at": row.get("recorded_at") or row.get("created_at"),
            "summary": dict(walk_forward.get("summary") or {}),
            "last_fold": {
                "test_window": last.get("test_window") or last.get("test"),
                "test_stats": last.get("test_stats"),
            },
            "fold_count": len(folds),
        }
    return None


def _cost_coverage(stats: dict[str, Any], capital: float) -> dict[str, Any]:
    fees = _float(stats.get("total_fees"))
    net_return = _float(stats.get("net_return"))
    if fees is None or net_return is None or capital <= 0:
        return {"fees_usd": fees, "fees_to_gross": None, "fees_pct_of_capital": None}
    net_pnl = net_return * capital
    gross = net_pnl + fees
    return {
        "fees_usd": fees,
        "fees_pct_of_capital": fees / capital,
        "fees_to_gross": (fees / gross) if gross > 0 else None,
    }


def default_holdout_bars(
    store: JobStore, job_id: str, *, fraction: float = 0.15
) -> int:
    """The small holdout: the last ~15% of the dataset as one walk-forward test window."""
    backtest = (
        store.read_json(job_id, "results/backtest/latest.json", default=None) or {}
    )
    dataset = dict(backtest.get("dataset") or {})
    bars = _float(dataset.get("bars") or dataset.get("rows"))
    if bars is None or bars <= 0:
        return 500
    return max(48, int(bars * fraction))


def _pending_ops(root: Path) -> list[str]:
    ops_dir = root / "state" / "background_ops"
    if not ops_dir.is_dir():
        return []
    running: list[str] = []
    for path in sorted(ops_dir.glob("*.json")):
        if path.name.endswith(".result.json"):
            continue
        try:
            status = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            isinstance(status, dict)
            and status.get("state") == "running"
            and recorded_process_alive(status)
        ):
            running.append(path.stem)
    return running


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = ["NO_CLAIM_SENTENCE", "READOUT_PATH", "build_readout"]
