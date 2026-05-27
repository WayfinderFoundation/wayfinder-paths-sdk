from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

DEFAULT_MAX_BYTES = 10_000
HEAD_ITEMS = 20
STRING_HEAD_CHARS = 400
STRING_TAIL_CHARS = 200
STRING_INLINE_LIMIT = 1024  # nested strings under this size pass through unsliced


def _scratch_dir() -> Path:
    base = os.environ.get("WAYFINDER_SCRATCH_DIR") or ".wayfinder_runs/.scratch"
    path = Path(base) / "ctx"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _max_bytes() -> int:
    raw = os.environ.get("WF_MAX_CONTEXT_BYTES")
    if not raw:
        return DEFAULT_MAX_BYTES
    try:
        return max(1024, int(raw))
    except ValueError:
        return DEFAULT_MAX_BYTES


def _shape(value: Any) -> Any:
    if isinstance(value, list):
        return {"type": "list", "len": len(value)}
    if isinstance(value, dict):
        return {
            "type": "dict",
            "keys": list(value.keys())[:40],
            "collection_sizes": {
                k: len(v) for k, v in value.items() if isinstance(v, (list, dict))
            },
        }
    return {"type": type(value).__name__}


def _slice_collection(value: Any, head: int) -> Any:
    """Slice a list or dict to its first `head` items, leaving scalars alone."""
    if isinstance(value, list):
        return {
            "_list_truncated": len(value) > head,
            "len": len(value),
            "head": value[:head],
        }
    if isinstance(value, dict):
        items = list(value.items())
        if len(items) > head:
            return {
                "_dict_truncated": True,
                "len": len(items),
                "head": dict(items[:head]),
            }
    return value


def _truncate_for_preview(value: Any, head: int) -> Any:
    """Shallow-preview: keep top-level scalars, slice large lists/dicts, recurse one level."""
    if isinstance(value, (list, dict)) and not isinstance(value, dict):
        return _slice_collection(value, head)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(v, (list, dict)):
                sliced = _slice_collection(v, head)
                # Recurse one level into dict children for nested heavy fields (e.g. state.positions)
                if isinstance(v, dict) and sliced is v:
                    out[k] = {sk: _slice_collection(sv, head) for sk, sv in v.items()}
                else:
                    out[k] = sliced
            else:
                out[k] = v
        return out
    return value


def guard_payload(
    payload: Any,
    *,
    name: str,
    max_bytes: int | None = None,
    head: int = HEAD_ITEMS,
) -> Any:
    """If payload serializes larger than the threshold, spill to scratch and return an envelope.

    Apply at MCP-boundary only — never inside clients/adapters whose returns are consumed
    programmatically. The envelope shape is: {_truncated, artifact, bytes, shape, head, hint}.
    """
    limit = max_bytes if max_bytes is not None else _max_bytes()
    try:
        serialized = json.dumps(payload, default=str)
    except (TypeError, ValueError):
        return payload

    if len(serialized) <= limit:
        return payload

    artifact = _scratch_dir() / f"{name}-{uuid.uuid4().hex[:8]}.json"
    artifact.write_text(serialized)

    return {
        "_truncated": True,
        "reason": f"output > {limit} bytes",
        "artifact": str(artifact),
        "bytes": len(serialized),
        "shape": _shape(payload),
        "head": _truncate_for_preview(payload, head),
        "hint": (
            f"`jq '.<field>[0:50]' {artifact}` or `json.load(open('{artifact}'))`."
        ),
    }
