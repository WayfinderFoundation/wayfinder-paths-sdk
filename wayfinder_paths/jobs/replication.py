"""Backtest replication monitor: does the deploy-time edge still exist?

The majors-5m-lab deploy backtest promised +21% on its selection window;
five days later the same strategy re-backtested ~0% on the refreshed window
and ran -41bps/trade forward. That collapse sat unnoticed in artifacts —
nothing treated "the deploy-time backtest stopped replicating on fresh
data" as the alarm it is.

This monitor re-runs the ACTIVE strategy over the CURRENT dataset
(stamp-gated daily), pins the first run per revision as that revision's
baseline, and reports drift. It is the in-sample cousin of the shadow A/B:
the shadow asks "did the change help vs its predecessor", replication asks
"was the evidence that justified this revision real". Never raises — a
wake cannot die on a monitor.
"""

from __future__ import annotations

from typing import Any

from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore

REPLICATION_PATH = "results/backtest/replication.json"
_RECOMPUTE_AFTER_S = 24 * 3600
# Worker-safety: give up on the compute lock quickly (see _compute).
_LOCK_TIMEOUT_S = 60.0
# Relative collapse of net return vs the revision's own baseline that flags
# decay (sign flips always flag).
_DECAY_RELATIVE = 0.5


def load_replication(store: JobStore, job_id: str) -> dict[str, Any] | None:
    doc = store.read_json(job_id, REPLICATION_PATH)
    return doc if isinstance(doc, dict) else None


def replication_job(
    job_id: str, *, store: JobStore | None = None, force: bool = False
) -> dict[str, Any]:
    store = store or JobStore()
    cached = load_replication(store, job_id)
    if not force and cached and cached.get("available"):
        age = _age_seconds(str(cached.get("computed_at")))
        if age < _RECOMPUTE_AFTER_S:
            return cached
    try:
        artifact = _compute(store, job_id, cached)
    except Exception as exc:  # noqa: BLE001 — monitors degrade, never raise
        store.append_journal(
            job_id, {"type": "replication_failed", "error": str(exc)[:300]}
        )
        return {"available": False, "reason": f"compute failed: {exc}"}
    store.write_json(job_id, REPLICATION_PATH, artifact)
    return artifact


def _compute(
    store: JobStore, job_id: str, cached: dict[str, Any] | None
) -> dict[str, Any]:
    from wayfinder_paths.jobs.compute_lock import heavy_compute_lock
    from wayfinder_paths.jobs.execution.job import backtest_execution_job

    # Short lock wait for the same reason as the counterfactual monitor:
    # this runs in a runner worker on the wake path — skip the cycle rather
    # than hold a worker behind a long-running sim. The inner acquire in
    # backtest_execution_job is reentrant and free once we hold it here.
    with heavy_compute_lock(
        repo_root=store.repo_root,
        label=f"replication:{job_id}",
        timeout_s=_LOCK_TIMEOUT_S,
    ):
        payload = backtest_execution_job(job_id, store=store)
    result = payload.get("result") or {}
    stats = result.get("stats") or {}
    revision = str(payload.get("revision") or "")
    dataset_meta = (payload.get("dataset") or {}) if isinstance(payload, dict) else {}
    current = {
        "net_return": stats.get("net_return"),
        "avg_trade_pnl": stats.get("avg_trade_pnl"),
        "total_trades": stats.get("total_trades") or stats.get("trade_count"),
        "win_rate": stats.get("win_rate"),
        "run_at": utc_now_iso(),
    }

    baseline = None
    if cached and cached.get("revision") == revision:
        baseline = cached.get("baseline")
    if not baseline:
        # First run for this revision pins its baseline — subsequent runs
        # measure drift against it. A new revision resets the baseline (its
        # own candidate report was its deploy evidence).
        baseline = dict(current)

    decayed = _decayed(baseline, current)
    return {
        "available": True,
        "revision": revision,
        "baseline": baseline,
        "current": current,
        "dataset": {
            key: dataset_meta.get(key)
            for key in ("days", "days_received", "source", "fetched_at")
            if isinstance(dataset_meta, dict)
        },
        "decayed": decayed,
        "_basis": (
            "Same ACTIVE strategy, re-backtested on the refreshed dataset and "
            "compared to this revision's first replication run. decayed=true "
            "means the in-sample edge that justified this revision is not "
            "reproducing on newer data — mechanical evidence of selection on "
            "window-local noise; treat it as grounds for a revert/kill or "
            "re-validation proposal, not something to explain away."
        ),
        "computed_at": utc_now_iso(),
    }


def _decayed(baseline: dict[str, Any], current: dict[str, Any]) -> bool:
    try:
        base = float(baseline.get("net_return") or 0.0)
        now = float(current.get("net_return") or 0.0)
    except (TypeError, ValueError):
        return False
    if base <= 0:
        return False  # baseline never showed an edge; nothing to decay
    if now <= 0:
        return True  # sign flip
    return (base - now) / base > _DECAY_RELATIVE


def _age_seconds(computed_at: str) -> float:
    import datetime as dt

    try:
        computed = dt.datetime.fromisoformat(computed_at)
    except ValueError:
        return float("inf")
    if computed.tzinfo is None:
        computed = computed.replace(tzinfo=dt.UTC)
    return (dt.datetime.now(dt.UTC) - computed).total_seconds()
