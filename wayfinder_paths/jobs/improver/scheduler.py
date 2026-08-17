"""Deterministic search-island scheduler (L3c).

One worker, one wake at a time — but WHICH kind of search a wake runs was
left to the model's mood, and observed behavior collapsed to exploit-only:
refine the incumbent, report healthy, sleep. The scheduler makes allocation
a property of the SYSTEM: each routine research wake is assigned one island
by weighted-deficit rotation against the ImproverSpec's allocation weights.
No RNG — the assignment is a pure function of on-disk state, so any
sequence is auditable and replayable.

Islands:
- exploit         — refine the incumbent's neighborhood (grids, exits, params)
- adjacent        — validated family, new cell (regime, symbol, timeframe)
- divergent       — new basin (family not in dead map, universe, sizing axis)
- diversification — behaviorally different candidates from the live book
- falsifier       — attack the incumbent; a confirmed refutation is a WIN
- historian       — mine the archive (revive frontier branches, autopsy dead)

Boosts are deterministic weight multipliers derived from visible state:
verdict stuck-streak -> divergent; opportunity_recall.missed -> historian;
high fleet correlation (portfolio report, PR 6) -> diversification. The
exploration floor guarantees the non-exploit islands can never be starved
below the spec's minimum share.

Trigger wakes (verdict_matured, restage, risk events) bypass assignment —
they exist to handle their event, not to run the rotation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.improver.spec import ImproverSpec
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore

ISLANDS = (
    "exploit",
    "adjacent",
    "divergent",
    "diversification",
    "falsifier",
    "historian",
)
EXPLORE_ISLANDS = ("divergent", "diversification", "falsifier", "historian")
SCHEDULER_STATE_PATH = "state/scheduler.json"
# A trigger wake stamps state/agent_wake_state.json immediately before the
# worker runs; a stamp this fresh means THIS wake is the trigger's wake.
_TRIGGER_WINDOW_S = 180

ISLAND_DIRECTIVES: dict[str, str] = {
    "exploit": (
        "Refine the incumbent's neighborhood: parameter/exit-structure "
        "experiments on the ACTIVE families (grid for attribution, optuna "
        "for wide spaces), promote-params on validated winners. Cite the "
        "trial lineage before sampling — re-running a flat region is spent "
        "budget."
    ),
    "adjacent": (
        "Extend a VALIDATED family to a new cell: another regime "
        "(--condition-regime), symbol, or timeframe. The family's evidence "
        "transfers as a prior, not as a result — the new cell earns its own "
        "scan + walk-forward before any proposal."
    ),
    "divergent": (
        "Jump basins: a family not in the dead map, a universe-scan, or the "
        "sizing/leverage axis. Wide typed search spaces with "
        "optimizer=optuna (multi-objective for structural sweeps). Do NOT "
        "refine the incumbent this wake — that is exploit's job."
    ),
    "diversification": (
        "Hunt candidates whose BEHAVIOR differs from the live book: holding "
        "period, direction bias, entry frequency (trial behavior "
        "descriptors + archive behavior fields). A book of clones is one "
        "bet wearing several names — score candidates by behavioral "
        "distance, not just return."
    ),
    "falsifier": (
        "Attack the incumbent: re-run replication, probe regime-slice decay "
        "(t_recent within active cells), re-test the weakest live "
        "assumption on fresh data. You are trying to REFUTE — a confirmed "
        "refutation (with a revert/kill proposal) is a successful wake."
    ),
    "historian": (
        "Mine the archive: investigate opportunity_recall.missed candidates "
        "FIRST (cite candidate_id), autopsy refuted branches for the "
        "condition that killed them, map flat regions from trial lineage. "
        "Output: revive with evidence, or bury with a written reason."
    ),
}


def load_scheduler_state(store: JobStore, job_id: str) -> dict[str, Any]:
    doc = store.read_json(job_id, SCHEDULER_STATE_PATH) or {}
    if not isinstance(doc, dict) or not isinstance(doc.get("assigned"), dict):
        return {"assigned": {}, "history": [], "total": 0}
    return doc


def assign_island(
    store: JobStore,
    job_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assign this wake's island and persist the rotation state. Returns a
    bypass marker (island=None) for trigger wakes."""
    root = store.job_dir(job_id)
    trigger = _fresh_trigger(root, now=now)
    if trigger:
        return {"island": None, "bypass": trigger}

    spec = ImproverSpec.load(root)
    state = load_scheduler_state(store, job_id)
    assigned: dict[str, int] = {
        island: int(state["assigned"].get(island, 0)) for island in ISLANDS
    }
    total = sum(assigned.values())

    weights = {island: spec.island_weights.get(island, 0.0) for island in ISLANDS}
    boosts: list[str] = []
    if _verdict_stuck_streak(store, job_id, spec.stuck_same_family_non_wins):
        weights["divergent"] *= 2.0
        boosts.append("stuck: recent verdicts all neutral/hurt -> divergent x2")
    if _opportunity_recall_missed(store, job_id):
        weights["historian"] *= 2.0
        boosts.append("opportunity_recall.missed -> historian x2")
    if _fleet_correlation_high(store, job_id):
        weights["diversification"] *= 2.0
        boosts.append("fleet correlation high -> diversification x2")
    scale = sum(weights.values()) or 1.0
    weights = {island: value / scale for island, value in weights.items()}

    # Weighted deficit: the island furthest below its target share goes next.
    # Ties break on fixed ISLANDS order — fully deterministic.
    def deficit(island: str) -> float:
        return (total + 1) * weights[island] - assigned[island]

    candidates = list(ISLANDS)
    explore_assigned = sum(assigned[island] for island in EXPLORE_ISLANDS)
    floor = spec.exploration_floor
    floored = total > 0 and (explore_assigned / total) < floor
    if floored:
        candidates = list(EXPLORE_ISLANDS)

    island = max(candidates, key=lambda name: (deficit(name), -ISLANDS.index(name)))

    reasons = [f"weighted-deficit rotation (target {weights[island]:.0%})"]
    if floored:
        reasons.insert(
            0,
            f"exploration floor: explore share {explore_assigned}/{total} "
            f"< {floor:.0%} — restricted to explore islands",
        )
    reasons.extend(boosts)

    assigned[island] += 1
    state = {
        "assigned": assigned,
        "total": total + 1,
        "history": (state.get("history") or [])[-19:]
        + [{"island": island, "ts": utc_now_iso(), "reason": reasons[0]}],
    }
    store.write_json(job_id, SCHEDULER_STATE_PATH, state)
    store.append_journal(
        job_id, {"type": "island_assigned", "island": island, "reasons": reasons}
    )
    return {
        "island": island,
        "reasons": reasons,
        "directive": ISLAND_DIRECTIVES[island],
        "agenda": f"research/islands/{island}.md",
        "assigned_counts": assigned,
        "target_weights": {k: round(v, 3) for k, v in weights.items()},
    }


def _fresh_trigger(root: Path, *, now: datetime | None) -> list[str] | None:
    path = root / "state" / "agent_wake_state.json"
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        last = datetime.fromisoformat(str(state.get("last_triggered_wake_ts")))
    except (ValueError, TypeError):
        return None
    current = now or datetime.now(UTC)
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    if (current - last).total_seconds() < _TRIGGER_WINDOW_S:
        return [str(t) for t in state.get("triggers") or []] or ["trigger"]
    return None


def _verdict_stuck_streak(store: JobStore, job_id: str, streak: int) -> bool:
    """True when the last `streak` matured verdicts are all neutral/hurt —
    the local basin is flat or hostile; boost the jump island."""
    verdicts = store.read_json(job_id, "state/promotion_verdicts.json") or {}
    if not isinstance(verdicts, dict):
        return False
    matured = [
        record
        for record in verdicts.values()
        if isinstance(record, dict)
        and record.get("verdict") in {"beat", "neutral", "hurt"}
    ]
    matured.sort(key=lambda record: str(record.get("recorded_at") or ""))
    if len(matured) < streak:
        return False
    return all(
        record.get("verdict") in {"neutral", "hurt"} for record in matured[-streak:]
    )


def _opportunity_recall_missed(store: JobStore, job_id: str) -> bool:
    try:
        from wayfinder_paths.jobs.evolution_ledger import _opportunity_recall

        recall = _opportunity_recall(store, job_id)
        return bool(recall and recall.get("missed"))
    except Exception:  # noqa: BLE001 — telemetry input, never blocks rotation
        return False


def _fleet_correlation_high(store: JobStore, job_id: str) -> bool:
    """Portfolio hook (PR 6 writes the report): this job's average forward
    correlation to the rest of the fleet above 0.7 raises diversification."""
    path = store.repo_root / ".wayfinder" / "portfolio" / "report.json"
    if not path.exists():
        return False
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        value = ((report.get("jobs") or {}).get(job_id) or {}).get("avg_correlation")
        return value is not None and float(value) > 0.7
    except (ValueError, TypeError):
        return False
