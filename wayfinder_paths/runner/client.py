from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from wayfinder_paths.runner.protocol import decode_response_bytes, encode_request
from wayfinder_paths.runner.transport import RunnerTransport, UnixSocketTransport

SESSION_ENV_KEYS = (
    "OPENCODE_SESSION_ID",
    "OPENCODE_SESSIONID",
)


def caller_session_id() -> str | None:
    # Read in the caller's process: the daemon is long-lived and its own env
    # holds whatever session happened to start it.
    return next(
        (os.environ[key] for key in SESSION_ENV_KEYS if os.environ.get(key)), None
    )


class RunnerControlClient:
    def __init__(
        self, *, sock_path: Path, transport: RunnerTransport | None = None
    ) -> None:
        self._sock_path = Path(sock_path)
        self._transport = transport or UnixSocketTransport(self._sock_path)

    @property
    def sock_path(self) -> Path:
        return self._sock_path

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = dict(params or {})
        session_id = caller_session_id()
        if session_id:
            params.setdefault("caller_session_id", session_id)
        try:
            payload = encode_request(method, params)
            buf = self._transport.roundtrip(payload)
        except OSError as exc:
            return {
                "ok": False,
                "error": "connect_failed",
                "message": str(exc),
                "sock_path": str(self._sock_path),
            }
        return decode_response_bytes(buf)
