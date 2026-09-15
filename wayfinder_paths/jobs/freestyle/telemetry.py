"""What a freestyle (or Path-hosted freestyle) job is doing, as one snapshot
block: the script's declared limits, the last tick's reads and actions, the
forward counts and the validation dry run. Read from one-row files and the
forward summary only — never from the per-tick ledgers — so a sync stays
cheap however long the job has run."""

from __future__ import annotations

from typing import Any

from wayfinder_paths.jobs.health import FORWARD_SUMMARY_PATH, FREESTYLE_LAST_TICK_PATH
from wayfinder_paths.jobs.readout import NO_CLAIM_SENTENCE
from wayfinder_paths.jobs.store import JobStore

VALIDATION_PATH = "reports/validation/latest.json"
ACTION_CAP = 20
GUARD_CAP = 10
LOG_CAP = 20


def _actions(rows: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in list(rows or [])[-ACTION_CAP:]:
        if not isinstance(row, dict):
            continue
        intent = row.get("intent")
        if not isinstance(intent, dict):
            intent = {}
        out.append(
            {
                "status": row.get("status"),
                "reason": row.get("reason"),
                "venue": row.get("venue") or intent.get("venue"),
                "symbol": row.get("symbol") or intent.get("symbol"),
                "kind": row.get("kind") or intent.get("action"),
                "side": row.get("side") or intent.get("side"),
                "notional": row.get("notional") or intent.get("notional"),
                "size": row.get("size") or intent.get("size"),
            }
        )
    return out


def compact_tick(payload: dict[str, Any], *, revision: str | None) -> dict[str, Any]:
    """The one-row last-tick record the runtime writes after every real tick."""
    return {
        "ts": payload.get("ts"),
        "status": payload.get("status"),
        "ok": payload.get("ok"),
        "summary": payload.get("summary"),
        "error": payload.get("error"),
        "timed_out": bool(payload.get("timed_out")),
        "mode": payload.get("mode"),
        "revision": revision or None,
        "equity": payload.get("equity"),
        "realized_pnl": payload.get("realized_pnl"),
        "unrealized_pnl": payload.get("unrealized_pnl"),
        "positions": dict(payload.get("positions") or {}),
        "reads": {
            "marks": dict(payload.get("marks") or {}),
            "funding": dict(payload.get("funding") or {}),
            "token_values": dict(payload.get("token_values") or {}),
            "yields": dict(payload.get("yields") or {}),
        },
        "actions": _actions(payload.get("actions")),
        "guard_events": list(payload.get("guard_events") or [])[-GUARD_CAP:],
        "logs": [
            str(line)[:300] for line in list(payload.get("logs") or [])[-LOG_CAP:]
        ],
        "notifications": [
            {"title": n.get("title"), "delivery": n.get("delivery")}
            for n in payload.get("notifications") or []
            if isinstance(n, dict)
        ],
        "unpapered_actions": list(payload.get("unpapered_actions") or []),
        "venues_used": list(payload.get("venues_used") or []),
        "halted": bool(payload.get("halted")),
    }


def _dry_run_view(
    dry: dict[str, Any] | None, revision: str | None
) -> dict[str, Any] | None:
    if not dry:
        return None
    return {
        "ok": dry.get("ok"),
        "ticks": dry.get("ticks"),
        "revision": revision,
        "actions": _actions(dry.get("actions")),
        "fills": len(dry.get("fills") or []),
        "equity": dry.get("equity"),
        "marks": dict(dry.get("marks") or {}),
        "funding": dict(dry.get("funding") or {}),
        "token_values": dict(dry.get("token_values") or {}),
        "yields": dict(dry.get("yields") or {}),
        "guard_events": list(dry.get("guard_events") or [])[-GUARD_CAP:],
        "unpapered_actions": list(dry.get("unpapered_actions") or []),
        "notifications": [
            n.get("title") if isinstance(n, dict) else str(n)
            for n in dry.get("notifications") or []
        ],
        "error": dry.get("error"),
        "no_claim": NO_CLAIM_SENTENCE,
    }


def freestyle_snapshot(store: JobStore, job_id: str, job: Any) -> dict[str, Any] | None:
    contract = str(job.execution_contract or "legacy")
    if contract not in {"freestyle_v1", "path_v1"}:
        return None
    validation = store.read_json(job_id, VALIDATION_PATH, default={}) or {}
    section = validation.get("freestyle") or {}
    path_section = validation.get("path") or {}
    if (
        contract == "path_v1"
        and str(path_section.get("component_kind") or "") != "freestyle"
    ):
        return None
    from wayfinder_paths.jobs.launch import LAUNCH_STATE_PATH

    last = store.read_json(job_id, FREESTYLE_LAST_TICK_PATH, default=None)
    last = last if isinstance(last, dict) else None
    summary = store.read_json(job_id, FORWARD_SUMMARY_PATH, default={}) or {}
    runs = summary.get("runs") or {}
    ticks = summary.get("ticks") or {}
    fills = summary.get("fills") or {}
    launch = store.read_json(job_id, LAUNCH_STATE_PATH, default=None)
    spec = section.get("spec") or path_section.get("spec") or {}
    return {
        "contract": contract,
        "mode": (last or {}).get("mode") or str(job.script_loop.mode or "paper"),
        "launched": bool(launch),
        "spec": {
            "venues": list(spec.get("venues") or []),
            "symbols": list(spec.get("symbols") or []),
            "max_notional_per_tick": spec.get("max_notional_per_tick"),
            "max_loss_usd": spec.get("max_loss_usd"),
            "halt_when": dict(spec.get("halt_when") or {}),
            "quote_interval": spec.get("quote_interval"),
        },
        "last_tick": last,
        "counts": {
            "ticks": int(ticks.get("count") or runs.get("count") or 0),
            "runs": int(runs.get("count") or 0),
            "errors": int(runs.get("error_count") or 0),
            "fills": int(fills.get("count") or 0),
            "last_fill_at": fills.get("last_fill_at"),
        },
        "dry_run": _dry_run_view(
            section.get("dry_run") or path_section.get("dry_run"),
            validation.get("revision"),
        ),
    }


def path_snapshot(store: JobStore, job_id: str, job: Any) -> dict[str, Any] | None:
    if str(job.execution_contract or "legacy") != "path_v1":
        return None
    from wayfinder_paths.jobs.paths_runtime import UPGRADE_STATE_PATH

    validation = store.read_json(job_id, VALIDATION_PATH, default={}) or {}
    section = validation.get("path") or {}
    pin = (
        section.get("pin") or (job.source if isinstance(job.source, dict) else {}) or {}
    )
    kind = str(section.get("component_kind") or pin.get("component_kind") or "exec")
    return {
        "pin": {
            key: pin.get(key)
            for key in (
                "slug",
                "version",
                "component",
                "component_kind",
                "bundle_sha256",
                "dry_run",
            )
        },
        "component_kind": kind,
        "dry_run_declared": kind == "freestyle"
        or str(pin.get("dry_run") or "unsupported") == "supported",
        "upgrade": store.read_json(job_id, UPGRADE_STATE_PATH, default=None),
    }


__all__ = ["compact_tick", "freestyle_snapshot", "path_snapshot"]
