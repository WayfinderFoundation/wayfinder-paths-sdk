"""Generic Wayfinder WorkPack persistence.

WorkPacks are durable handoff/audit artifacts. They are deliberately not live
execution truth: packs that contain executable surfaces must declare when they
must be rehydrated before use.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

PackType = Literal[
    "surfacePack",
    "contextPack",
    "featurePack",
    "analysisPack",
    "decisionPack",
    "validationReport",
]

PACK_TYPES: set[str] = {
    "surfacePack",
    "contextPack",
    "featurePack",
    "analysisPack",
    "decisionPack",
    "validationReport",
}
STAGE_BY_TYPE = {
    "surfacePack": "surface",
    "contextPack": "context",
    "featurePack": "feature",
    "analysisPack": "analysis",
    "decisionPack": "decision",
    "validationReport": "validation",
}
EXECUTION_SENSITIVE_TYPES = {"surfacePack", "decisionPack"}
DEFAULT_REHYDRATE_ACTIONS = ["execute", "place_order", "swap", "recommend_buy"]
PACKS_ROOT_ENV = "WAYFINDER_PACKS_ROOT"


def _packs_root() -> Path:
    value = os.environ.get(PACKS_ROOT_ENV)
    return Path(value) if value else Path(".wayfinder_runs") / "packs"


def _index_path() -> Path:
    return _packs_root() / "index.jsonl"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).astimezone(UTC)


def _scope_hash(scope: Mapping[str, Any] | None) -> str:
    encoded = json.dumps(scope or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:8]


def _safe(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value)


def _json_dump(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as tmp:
        json.dump(data, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        temp_name = tmp.name
    Path(temp_name).replace(path)


def _iter_index() -> list[dict[str, Any]]:
    path = _index_path()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _append_index(row: Mapping[str, Any]) -> None:
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(dict(row), sort_keys=True, separators=(",", ":")) + "\n")


def _rewrite_index(rows: list[dict[str, Any]]) -> None:
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    )
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as tmp:
        tmp.write(text)
        temp_name = tmp.name
    Path(temp_name).replace(path)


def _normalize_pack(pack: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(pack)
    pack_type = str(normalized.get("packType") or "")
    if pack_type not in PACK_TYPES:
        raise ValueError(f"invalid packType {pack_type!r}")

    domain = str(normalized.get("domain") or "").strip()
    if not domain:
        raise ValueError("pack.domain is required")

    stage = str(normalized.get("stage") or STAGE_BY_TYPE[pack_type])
    if stage != STAGE_BY_TYPE[pack_type]:
        raise ValueError(f"stage {stage!r} does not match {pack_type}")

    now = _utc_now()
    observed_at = str(normalized.get("observedAt") or _iso(now))
    reuse_policy = dict(normalized.get("reusePolicy") or {})
    ttl = reuse_policy.get("ttlSeconds")
    valid_until = normalized.get("validUntil")
    if valid_until is None:
        if ttl is None:
            raise ValueError("pack.validUntil or reusePolicy.ttlSeconds is required")
        valid_until = _iso(_parse_iso(observed_at) + timedelta(seconds=int(ttl)))

    if pack_type in EXECUTION_SENSITIVE_TYPES:
        must_rehydrate = reuse_policy.get("mustRehydrateBefore")
        if not must_rehydrate:
            raise ValueError(f"{pack_type} requires reusePolicy.mustRehydrateBefore")
    elif "mustRehydrateBefore" not in reuse_policy:
        reuse_policy["mustRehydrateBefore"] = []

    if "canReuseFor" not in reuse_policy:
        reuse_policy["canReuseFor"] = ["analysis", "final_answer"]
    if ttl is not None:
        reuse_policy["ttlSeconds"] = int(ttl)

    normalized.setdefault("schemaVersion", "1.0")
    normalized.setdefault("intent", "unspecified")
    normalized["stage"] = stage
    normalized["observedAt"] = observed_at
    normalized["validUntil"] = str(valid_until)
    normalized["scope"] = dict(normalized.get("scope") or {})
    normalized.setdefault("inputPacks", [])
    normalized.setdefault("sourceRefs", [])
    normalized.setdefault("artifactRefs", [])
    normalized.setdefault("summary", "")
    normalized.setdefault("payload", {})
    normalized["reusePolicy"] = reuse_policy
    normalized.setdefault("sensitivity", "public")
    normalized.setdefault("redactions", [])
    normalized.setdefault(
        "lineage",
        {"createdBy": "unknown", "consumedPacks": [], "refreshedFields": []},
    )

    scope_hash = str(normalized.get("scopeHash") or _scope_hash(normalized["scope"]))
    normalized["scopeHash"] = scope_hash
    if not normalized.get("packId"):
        stamp = _parse_iso(observed_at).strftime("%Y%m%dT%H%M%SZ")
        base = f"pack_{_safe(domain)}_{stage}_{stamp}_{scope_hash}"
        normalized["packId"] = base

    return normalized


def _pack_path(pack: Mapping[str, Any]) -> Path:
    pack_id = _safe(str(pack["packId"]))
    return (
        _packs_root()
        / _safe(str(pack["domain"]))
        / str(pack["stage"])
        / f"{pack_id}.json"
    )


def pack_ref(pack: Mapping[str, Any]) -> dict[str, Any]:
    """Return compact pack metadata suitable for subagent handoff."""

    path = str(pack.get("path") or _pack_path(pack))
    return {
        "packId": pack.get("packId"),
        "packType": pack.get("packType"),
        "domain": pack.get("domain"),
        "path": path,
        "observedAt": pack.get("observedAt"),
        "validUntil": pack.get("validUntil"),
        "summary": pack.get("summary", ""),
    }


def write_pack(pack: dict[str, Any]) -> dict[str, Any]:
    """Validate, persist, index, and return a compact packRef."""

    normalized = _normalize_pack(pack)
    path = _pack_path(normalized)
    if path.exists() and not pack.get("packId"):
        suffix = hashlib.sha256(
            json.dumps(normalized, sort_keys=True).encode()
        ).hexdigest()[:6]
        normalized["packId"] = f"{normalized['packId']}_{suffix}"
        path = _pack_path(normalized)
    normalized["path"] = str(path)
    _json_dump(path, normalized)
    ref = pack_ref(normalized)
    _append_index(
        {
            **ref,
            "stage": normalized["stage"],
            "intent": normalized.get("intent"),
            "scopeHash": normalized.get("scopeHash"),
            "stale": False,
            "indexedAt": _iso(_utc_now()),
        }
    )
    return ref


def read_pack(pack_id_or_path: str) -> dict[str, Any]:
    """Load a pack by filesystem path or packId."""

    candidate = Path(pack_id_or_path)
    if candidate.exists():
        return json.loads(candidate.read_text("utf-8"))
    for row in reversed(_iter_index()):
        if row.get("packId") == pack_id_or_path:
            path = Path(str(row.get("path")))
            if path.exists():
                return json.loads(path.read_text("utf-8"))
    raise FileNotFoundError(pack_id_or_path)


def latest_pack(
    *,
    domain: str,
    pack_type: PackType,
    scope_hash: str | None = None,
) -> dict[str, Any] | None:
    """Return latest non-stale pack matching domain/type/scope."""

    for row in reversed(_iter_index()):
        if row.get("stale"):
            continue
        if row.get("domain") != domain or row.get("packType") != pack_type:
            continue
        if scope_hash is not None and row.get("scopeHash") != scope_hash:
            continue
        path = Path(str(row.get("path")))
        if not path.exists():
            continue
        pack = json.loads(path.read_text("utf-8"))
        if not is_stale(pack):
            return pack
    return None


def is_stale(pack: Mapping[str, Any], *, now_iso: str | None = None) -> bool:
    """Check explicit stale flag and validUntil timestamp."""

    if pack.get("stale"):
        return True
    valid_until = pack.get("validUntil")
    if not valid_until:
        return True
    now = _parse_iso(now_iso) if now_iso else _utc_now()
    return _parse_iso(str(valid_until)) <= now


def mark_pack_stale(pack_id: str, *, reason: str) -> None:
    """Mark a pack stale in both its JSON file and index rows."""

    pack = read_pack(pack_id)
    pack["stale"] = True
    pack["staleReason"] = reason
    pack["staleAt"] = _iso(_utc_now())
    path = Path(str(pack.get("path") or _pack_path(pack)))
    _json_dump(path, pack)

    rows = _iter_index()
    for row in rows:
        if row.get("packId") == pack_id:
            row["stale"] = True
            row["staleReason"] = reason
            row["staleAt"] = pack["staleAt"]
    _rewrite_index(rows)
