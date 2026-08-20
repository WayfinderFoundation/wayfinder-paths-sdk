"""Evolution ledger: the full update path of a job's self-improvement loop,
derived from artifacts that already exist (proposals, journals, promotion
verdicts, replication). A terminal score cannot distinguish retained
improvement from temporary adaptation — this report can: it shows what was
proposed, what survived which gate, what got promoted, and whether promoted
changes actually beat the incumbent forward (promotion reliability)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore

_VERDICTS_PATH = "state/promotion_verdicts.json"


def build_evolution_report(store: JobStore, job_id: str) -> dict[str, Any]:
    root = store.job_dir(job_id)
    proposals = _load_proposals(root)
    verdicts = store.read_json(job_id, _VERDICTS_PATH) or {}

    by_family: dict[str, dict[str, int]] = {}
    rows: list[dict[str, Any]] = []
    for proposal in proposals:
        family = _family(proposal)
        outcome = _outcome(proposal)
        bucket = by_family.setdefault(
            family,
            {
                "proposed": 0,
                "gate_rejected": 0,
                "owner_rejected": 0,
                "agent_rejected": 0,
                "promoted": 0,
                "pending": 0,
            },
        )
        bucket["proposed"] += 1
        bucket[outcome] += 1
        pid = str(proposal.get("proposal_id") or "")
        verdict = (verdicts.get(pid) or {}).get("verdict")
        rows.append(
            {
                "proposal_id": pid,
                "family": family,
                "outcome": outcome,
                "summary": str(
                    (proposal.get("proposed_change") or {}).get("summary") or ""
                )[:120],
                "created_at": proposal.get("created_at"),
                "forward_verdict": verdict,
            }
        )

    verdict_counts = {
        "beat": 0,
        "neutral": 0,
        "hurt": 0,
        "pending": 0,
        "censored_by_next_change": 0,
        "insufficient_evidence": 0,
    }
    deltas: list[float] = []
    strategy_effects: list[float] = []
    execution_effects: list[float] = []
    for record in verdicts.values():
        verdict = str(record.get("verdict") or "")
        if verdict in verdict_counts:
            verdict_counts[verdict] += 1
        if verdict in ("beat", "neutral", "hurt"):
            deltas.append(float(record.get("delta_net_pnl") or 0.0))
            if record.get("strategy_effect") is not None:
                strategy_effects.append(float(record["strategy_effect"]))
            if record.get("execution_effect") is not None:
                execution_effects.append(float(record["execution_effect"]))
    judged = verdict_counts["beat"] + verdict_counts["neutral"] + verdict_counts["hurt"]
    beat_rate = (verdict_counts["beat"] / judged) if judged else None

    # A raw beat_rate on 2 datapoints reads as certainty it does not have —
    # the Wilson interval keeps small samples honest.
    ci = _wilson_interval(verdict_counts["beat"], judged) if judged else None

    replication = _replication_summary(root)
    promoted_total = sum(bucket["promoted"] for bucket in by_family.values())
    return {
        "job_id": job_id,
        "proposals_total": len(proposals),
        "by_family": by_family,
        "promotion_reliability": {
            **verdict_counts,
            "judged": judged,
            # Of promotions old enough to judge, how many actually beat the
            # incumbent forward — the single trackable scalar for whether the
            # improve loop is earning its keep.
            "beat_rate": beat_rate,
            "beat_rate_ci95": ci,
            "mean_judged_delta_usd": (sum(deltas) / len(deltas) if deltas else None),
            # Three-book split (when recorded): a hurt-leaning mean driven by
            # execution_effect indicts the fill path, not the strategy loop.
            "mean_strategy_effect_usd": (
                sum(strategy_effects) / len(strategy_effects)
                if strategy_effects
                else None
            ),
            "mean_execution_effect_usd": (
                sum(execution_effects) / len(execution_effects)
                if execution_effects
                else None
            ),
        },
        # A high beat_rate with near-zero promotions is not a working loop —
        # yield pairs reliability with throughput.
        "improvement_yield": {
            "promoted": promoted_total,
            "promoted_per_proposal": (
                promoted_total / len(proposals) if proposals else None
            ),
        },
        "opportunity_recall": _opportunity_recall(store, job_id),
        "research_staleness": _research_staleness(root, proposals),
        "replication": replication,
        "proposals": rows[-50:],
        "generated_at": utc_now_iso(),
    }


def _wilson_interval(successes: int, n: int, z: float = 1.96) -> list[float]:
    if n == 0:
        return [0.0, 1.0]
    p = successes / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    margin = (z / denominator) * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return [round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4)]


def _research_staleness(root: Path, proposals: list[dict[str, Any]]) -> dict[str, Any]:
    """How long the improvement machinery has been idle — the visible metric
    behind the idle-wake research mandate. Observed live: ~450 healthy wakes
    across a 10-day proposal/experiment drought, with the archive and trial
    lineage never fired because everything upstream was starved."""
    import pandas as pd

    now = pd.Timestamp.now(tz="UTC")

    def _days_since(ts: str | None) -> float | None:
        if not ts:
            return None
        try:
            stamp = pd.Timestamp(ts)
        except ValueError:
            return None
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        return round((now - stamp).total_seconds() / 86400.0, 2)

    last_proposal_ts = str(proposals[-1].get("created_at") or "") if proposals else None

    last_experiment_ts = None
    experiments_path = root / "results" / "backtest" / "experiments.jsonl"
    if experiments_path.exists():
        lines = experiments_path.read_text(encoding="utf-8").strip().splitlines()
        if lines:
            try:
                last_experiment_ts = str(json.loads(lines[-1]).get("ts") or "") or None
            except ValueError:
                last_experiment_ts = None

    wakes_since = 0
    journal_path = root / "journal.jsonl"
    if journal_path.exists() and last_proposal_ts:
        for line in journal_path.read_text(encoding="utf-8").splitlines():
            if '"agent_wakeup"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if str(row.get("ts") or "") > last_proposal_ts:
                wakes_since += 1

    from wayfinder_paths.jobs.improver.spec import ImproverSpec

    spec = ImproverSpec.load(root)
    days_experiment = _days_since(last_experiment_ts)
    wakes = wakes_since if last_proposal_ts else None
    stale = bool(
        days_experiment is None
        or days_experiment > spec.staleness_experiment_days
        or (wakes is not None and wakes > spec.staleness_wakes)
    )
    return {
        "days_since_last_proposal": _days_since(last_proposal_ts),
        "days_since_last_experiment": days_experiment,
        "wakes_since_last_proposal": wakes,
        # Computed against the ACTIVE improver spec — a spec change moves this
        # flag, which moves the wake mandate. Prose thresholds no longer bind.
        "stale": stale,
        "thresholds": {
            "experiment_days": spec.staleness_experiment_days,
            "wakes_since_proposal": spec.staleness_wakes,
            "improver_revision": spec.revision,
        },
    }


def research_staleness_report(store: JobStore, job_id: str) -> dict[str, Any]:
    """Standalone staleness view for mechanical consumers (watchdog impasse
    check) — same computation the wake context carries, without the cost of
    the full evolution report."""
    root = store.job_dir(job_id)
    return _research_staleness(root, _load_proposals(root))


def _opportunity_recall(store: JobStore, job_id: str) -> dict[str, Any] | None:
    """Selection-regret telemetry, CONSTRAINED: a raw net_log_growth max
    flags high-growth candidates the constitution would never promote
    (drawdown/tail violators) as missed opportunities. Filter by the hard
    constraints, score with the constitution's utility weights, and flag
    missed only when the best passing candidate utility-beats or
    Pareto-dominates the incumbent."""
    try:
        from wayfinder_paths.jobs.archive import _dominates, load_archive
        from wayfinder_paths.jobs.constitution import load_constitution

        doc = load_archive(store, job_id)
        candidates = [
            entry
            for entry in doc.get("candidates") or []
            if isinstance(entry.get("objective"), dict)
            and entry.get("status") not in ("refuted", "retired")
        ]
        if not candidates:
            return None
        constitution = load_constitution(store.job_dir(job_id))
        hard = constitution.get("hard_constraints") or {}
        weights = (constitution.get("objective") or {}).get("weights") or {}

        def _violates(entry: dict[str, Any]) -> bool:
            vector = entry["objective"]
            checks = (
                ("max_drawdown_pct", hard.get("max_drawdown_pct")),
                ("tail_loss", hard.get("max_tail_loss")),
            )
            for axis, ceiling in checks:
                value = vector.get(axis)
                if ceiling is not None and value is not None:
                    if float(value) > float(ceiling):
                        return True
            return False

        def _utility(entry: dict[str, Any]) -> float:
            vector = entry["objective"]

            def axis(name: str) -> float:
                return float(vector.get(name) or 0.0)

            return (
                axis("net_log_growth")
                - float(weights.get("downside", 0.0)) * axis("downside_deviation")
                - float(weights.get("tail", 0.0)) * axis("tail_loss")
                - float(weights.get("turnover", 0.0)) * axis("fee_load")
            )

        incumbent = next(
            (e for e in candidates if e.get("status") == "incumbent"), None
        )
        passing = [e for e in candidates if not _violates(e)]
        excluded = len(candidates) - len(passing)
        if not passing:
            return {"missed": False, "violating_excluded": excluded}
        best = max(passing, key=_utility)
        if incumbent is None or best is incumbent:
            return {"missed": False, "violating_excluded": excluded}
        utility_gap = _utility(best) - _utility(incumbent)
        missed = utility_gap > 0 or _dominates(best, incumbent)
        return {
            "missed": missed,
            "best_candidate_id": best.get("candidate_id"),
            "utility_gap": round(utility_gap, 6),
            "violating_excluded": excluded,
            "basis": "constitution_utility+pareto over constraint-passing candidates",
        }
    except Exception:  # noqa: BLE001 — telemetry never breaks the report
        return None


def evolution_snapshot_block(store: JobStore, job_id: str) -> dict[str, Any]:
    """Compact per-wake view: family scoreboard + reliability, no row list."""
    report = build_evolution_report(store, job_id)
    return {
        "by_family": report["by_family"],
        "promotion_reliability": report["promotion_reliability"],
        "research_staleness": report["research_staleness"],
        # A LIVE archived candidate outscoring the incumbent is a measured
        # missed opportunity — the branch-revival lane reads this.
        "opportunity_recall": report["opportunity_recall"],
        "_basis": (
            "Full update path + forward promotion verdicts. Low beat_rate in a "
            "family means that family's evidence bar is too weak — raise your "
            "own bar before re-proposing, do not re-litigate the gate."
        ),
    }


def _load_proposals(root: Path) -> list[dict[str, Any]]:
    proposals_dir = root / "proposals"
    if not proposals_dir.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(proposals_dir.glob("*.json")):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(loaded, dict):
            rows.append(loaded)
    rows.sort(key=lambda row: str(row.get("created_at") or ""))
    return rows


def _family(proposal: dict[str, Any]) -> str:
    change = proposal.get("proposed_change") or {}
    if change.get("probation"):
        return "probation"
    if change.get("execution_params"):
        return "params"
    kind = str(proposal.get("kind") or change.get("kind") or "").strip()
    if kind:
        return kind
    return "code"


def _outcome(proposal: dict[str, Any]) -> str:
    status = str(proposal.get("status") or "")
    application = proposal.get("application") or {}
    if application.get("status") == "applied":
        return "promoted"
    if status == "rejected":
        rejected_by = str((proposal.get("rejection") or {}).get("by") or "")
        if rejected_by == "agent":
            return "agent_rejected"
        if rejected_by in {"owner", "user", "human"}:
            return "owner_rejected"
        return "gate_rejected"
    return "pending"


def _replication_summary(root: Path) -> dict[str, Any] | None:
    for relative in (
        "reports/replication/latest.json",
        "results/research/replication.json",
    ):
        path = root / relative
        if not path.exists():
            continue
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(loaded, dict):
            return {
                "decayed": loaded.get("decayed"),
                "computed_at": loaded.get("computed_at") or loaded.get("generated_at"),
            }
    return None
