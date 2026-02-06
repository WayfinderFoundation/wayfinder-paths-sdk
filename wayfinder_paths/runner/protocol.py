from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProtocolError(Exception):
    code: str


def encode_request(method: str, params: dict[str, Any] | None = None) -> bytes:
    req = {"method": str(method), "params": params or {}}
    return (json.dumps(req, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def decode_request_line(raw: bytes) -> tuple[str, dict[str, Any]]:
    if not raw:
        raise ProtocolError("empty_request")
    try:
        req = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:  # noqa: PERF203
        raise ProtocolError("invalid_json") from exc
    if not isinstance(req, dict):
        raise ProtocolError("invalid_request")

    method = str(req.get("method") or "")
    params = req.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    return method, params


def encode_response(resp: dict[str, Any]) -> bytes:
    data = json.dumps(resp, separators=(",", ":"), ensure_ascii=False, default=str)
    return (data + "\n").encode("utf-8")


def decode_response_bytes(raw: bytes) -> dict[str, Any]:
    line = raw.split(b"\n", 1)[0].strip()
    if not line:
        return {"ok": False, "error": "empty_response"}
    try:
        out = json.loads(line.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": "invalid_response",
            "raw": line.decode("utf-8", errors="replace"),
        }
    if isinstance(out, dict):
        return out
    return {"ok": False, "error": "invalid_response_type", "raw": out}
