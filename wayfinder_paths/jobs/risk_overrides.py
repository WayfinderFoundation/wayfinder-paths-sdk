"""Fast, monotone per-symbol entry blocks for regime incidents.

This rail is deliberately separate from strategy promotion.  It can only
remove risk: new entries and pending entry orders for a named symbol are
blocked while reduce-only exits continue.  Re-arming is owner-only.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.compute_lock import job_state_lock
from wayfinder_paths.jobs.store import JobStore

RISK_OVERRIDES_PATH = "state/risk_overrides.json"
EVIDENCE_MAX_AGE = timedelta(hours=6)


def load_risk_overrides(store: JobStore, job_id: str) -> dict[str, Any]:
    doc = store.read_json(job_id, RISK_OVERRIDES_PATH, default={}) or {}
    if not isinstance(doc, dict):
        doc = {}
    doc.setdefault("schema_version", "1.0")
    doc.setdefault("symbols", {})
    return doc


def active_symbol_blocks(store: JobStore, job_id: str) -> dict[str, dict[str, Any]]:
    return {
        symbol: dict(block)
        for symbol, block in load_risk_overrides(store, job_id)["symbols"].items()
        if isinstance(block, Mapping) and block.get("status") == "blocked"
    }


def risk_block_symbol(
    store: JobStore,
    job_id: str,
    *,
    symbol: str,
    reason: str,
    evidence_refs: list[str],
    wake_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Block one configured symbol when fresh regime/attribution evidence exists."""
    current = _aware(now or datetime.now(UTC))
    job = store.load(job_id)
    configured = {str(item) for item in job.execution_params.get("symbols") or []}
    selected = str(symbol).strip()
    if selected not in configured:
        raise ValueError(f"risk override symbol {selected!r} is not configured")
    verified = _verify_evidence_refs(store.job_dir(job_id), evidence_refs, now=current)
    if not verified:
        raise ValueError("risk block requires fresh regime/attribution evidence")
    wake = str(
        wake_id
        or os.environ.get("OPENCODE_SESSION_ID")
        or os.environ.get("OPENCODE_SESSIONID")
        or current.replace(minute=0, second=0, microsecond=0).isoformat()
    )
    with job_state_lock(store.repo_root, job_id, name="risk_overrides"):
        doc = load_risk_overrides(store, job_id)
        existing = doc["symbols"].get(selected) or {}
        if existing.get("status") == "blocked":
            return {
                "status": "duplicate",
                "symbol": selected,
                "block_status": existing.get("status"),
                "blocked_at": existing.get("blocked_at"),
                "reason": existing.get("reason"),
            }
        if doc.get("last_block_wake_id") == wake:
            raise ValueError("only one newly blocked symbol is allowed per sensor wake")
        block = {
            "status": "blocked",
            "blocked_at": current.isoformat(),
            "blocked_by": "sensor",
            "wake_id": wake,
            "reason": str(reason).strip()[:500],
            "evidence_refs": verified,
            "effect": "block_new_entries_allow_reduce_only_exits",
        }
        doc["symbols"][selected] = block
        doc["last_block_wake_id"] = wake
        store.write_json(job_id, RISK_OVERRIDES_PATH, doc)
    store.append_journal(
        job_id,
        {
            "type": "risk_symbol_blocked",
            "symbol": selected,
            "reason": block["reason"],
            "evidence_refs": verified,
            "effective_on": "next_tick",
        },
    )
    return {"symbol": selected, **block}


def risk_unblock_symbol(
    store: JobStore,
    job_id: str,
    *,
    symbol: str,
    by: str,
    reason: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if by != "owner":
        raise ValueError("only the owner can re-arm a symbol risk block")
    current = _aware(now or datetime.now(UTC))
    with job_state_lock(store.repo_root, job_id, name="risk_overrides"):
        doc = load_risk_overrides(store, job_id)
        block = doc["symbols"].get(symbol)
        if not isinstance(block, dict) or block.get("status") != "blocked":
            raise ValueError(f"symbol {symbol!r} is not blocked")
        block.update(
            {
                "status": "cleared",
                "cleared_at": current.isoformat(),
                "cleared_by": "owner",
                "clear_reason": str(reason or "owner re-armed symbol")[:500],
            }
        )
        store.write_json(job_id, RISK_OVERRIDES_PATH, doc)
    store.append_journal(
        job_id,
        {"type": "risk_symbol_unblocked", "symbol": symbol, "by": "owner"},
    )
    return {"symbol": symbol, **block}


def _verify_evidence_refs(root: Path, refs: list[str], *, now: datetime) -> list[str]:
    verified: list[str] = []
    allowed_roots = [
        (root / "results" / "research").resolve(),
        (root / "state").resolve(),
    ]
    for value in refs:
        relative = Path(str(value))
        if relative.is_absolute():
            continue
        path = (root / relative).resolve()
        if not any(path.is_relative_to(allowed) for allowed in allowed_roots):
            continue
        try:
            age = now - datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except OSError:
            continue
        if age <= EVIDENCE_MAX_AGE:
            verified.append(relative.as_posix())
    return verified


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
