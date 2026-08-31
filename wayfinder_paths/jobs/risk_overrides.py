"""Fast, monotone per-symbol entry blocks for regime incidents.

This rail is deliberately separate from strategy promotion.  It can only
remove risk: new entries and pending entry orders for a named symbol are
blocked while reduce-only exits continue.  Re-arming is owner-only.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from wayfinder_paths.jobs.compute_lock import job_state_lock
from wayfinder_paths.jobs.evidence import verify_job_evidence_refs
from wayfinder_paths.jobs.halt import request_halt
from wayfinder_paths.jobs.store import JobStore

RISK_OVERRIDES_PATH = "state/risk_overrides.json"
RISK_OVERRIDES_CORRUPTION_PATH = "state/risk_overrides_corruption.json"
EVIDENCE_MAX_AGE = timedelta(hours=6)
_UNREADABLE_KEY = "_unreadable"


def load_risk_overrides(store: JobStore, job_id: str) -> dict[str, Any]:
    path = store.job_dir(job_id) / RISK_OVERRIDES_PATH
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        doc: dict[str, Any] = {}
    except (OSError, UnicodeError) as exc:
        return _unreadable_overrides(f"{type(exc).__name__}: {exc}")
    else:
        try:
            loaded = json.loads(raw)
        except (TypeError, ValueError) as exc:
            return _unreadable_overrides(f"{type(exc).__name__}: {exc}")
        if not isinstance(loaded, dict) or not isinstance(
            loaded.get("symbols", {}), dict
        ):
            return _unreadable_overrides("risk override document has invalid shape")
        doc = loaded
    doc.setdefault("schema_version", "1.0")
    doc.setdefault("symbols", {})
    return doc


def active_symbol_blocks(store: JobStore, job_id: str) -> dict[str, dict[str, Any]]:
    doc = load_risk_overrides(store, job_id)
    if doc.get(_UNREADABLE_KEY):
        return _fail_closed_blocks(store, job_id, reason=str(doc.get("reason") or ""))
    return _active_blocks_from_doc(doc)


def risk_overrides_snapshot(store: JobStore, job_id: str) -> dict[str, Any]:
    """Return the same fail-closed override state enforced by execution."""
    doc = load_risk_overrides(store, job_id)
    if not doc.get(_UNREADABLE_KEY):
        return doc
    reason = str(doc.get("reason") or "unreadable risk override file")
    return {
        "schema_version": "1.0",
        "symbols": _fail_closed_blocks(store, job_id, reason=reason),
        "unreadable": True,
        "reason": reason,
    }


def enforced_symbol_blocks(store: JobStore, job_id: str) -> dict[str, dict[str, Any]]:
    """Resolve blocks for execution and latch unreadable state as a halt."""
    doc = load_risk_overrides(store, job_id)
    if not doc.get(_UNREADABLE_KEY):
        return _active_blocks_from_doc(doc)
    reason = str(doc.get("reason") or "unreadable risk override file")
    _journal_corruption_once(store, job_id, reason=reason)
    request_halt(
        store,
        job_id,
        reason="unreadable risk override file; symbol entry blocks fail closed",
        flatten=False,
        source="symbol_risk_override",
    )
    return _fail_closed_blocks(store, job_id, reason=reason)


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
    verified = verify_job_evidence_refs(
        store.job_dir(job_id),
        evidence_refs,
        allowed_roots=("results/research", "state"),
        now=current,
        max_age=EVIDENCE_MAX_AGE,
    )
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
        _require_readable(doc)
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
        _require_readable(doc)
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


def _unreadable_overrides(reason: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "symbols": {},
        _UNREADABLE_KEY: True,
        "reason": reason[:500],
    }


def _active_blocks_from_doc(doc: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(symbol): dict(block)
        for symbol, block in doc["symbols"].items()
        if isinstance(block, Mapping) and block.get("status") == "blocked"
    }


def _fail_closed_blocks(
    store: JobStore, job_id: str, *, reason: str
) -> dict[str, dict[str, Any]]:
    job = store.load(job_id)
    return {
        str(symbol): {
            "status": "blocked",
            "blocked_by": "fail_closed",
            "reason": f"unreadable risk override file: {reason}"[:500],
            "effect": "block_new_entries_allow_reduce_only_exits",
        }
        for symbol in job.execution_params.get("symbols") or []
    }


def _journal_corruption_once(store: JobStore, job_id: str, *, reason: str) -> None:
    path = store.job_dir(job_id) / RISK_OVERRIDES_PATH
    try:
        stat = path.stat()
        fingerprint = f"{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        fingerprint = reason
    marker = store.read_json(job_id, RISK_OVERRIDES_CORRUPTION_PATH, default={}) or {}
    if isinstance(marker, dict) and marker.get("fingerprint") == fingerprint:
        return
    store.write_json(
        job_id,
        RISK_OVERRIDES_CORRUPTION_PATH,
        {
            "fingerprint": fingerprint,
            "reason": reason,
            "detected_at": datetime.now(UTC).isoformat(),
        },
    )
    store.append_journal(
        job_id,
        {"type": "risk_overrides_unreadable", "reason": reason},
    )


def _require_readable(doc: Mapping[str, Any]) -> None:
    if doc.get(_UNREADABLE_KEY):
        raise ValueError("risk override state is unreadable; owner repair is required")


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
