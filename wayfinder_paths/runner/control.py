from __future__ import annotations

import os
import socketserver
import threading
from pathlib import Path

from loguru import logger

from wayfinder_paths.runner.api import dispatch
from wayfinder_paths.runner.protocol import (
    MAX_LINE_BYTES,
    ProtocolError,
    decode_request_line,
    encode_response,
)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:  # noqa: D401
        """Handle a single JSON-line request and respond with a JSON line."""
        raw = self.rfile.readline(MAX_LINE_BYTES)
        if not raw:
            return
        try:
            method, params = decode_request_line(raw)
        except ProtocolError as exc:
            self.wfile.write(encode_response({"ok": False, "error": exc.code}))
            return

        daemon = getattr(self.server, "daemon", None)
        if daemon is None:
            self.wfile.write(
                encode_response({"ok": False, "error": "daemon_unavailable"})
            )
            return

        try:
            resp = dispatch(daemon, method=method, params=params)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"Control dispatch error: {exc}")
            resp = {"ok": False, "error": str(exc)}

        self.wfile.write(encode_response(resp))


class RunnerControlServer:
    def __init__(self, *, sock_path: Path, daemon) -> None:
        self._sock_path = Path(sock_path)
        self._daemon = daemon
        self._thread: threading.Thread | None = None
        self._server: socketserver.UnixStreamServer | None = None

    @property
    def sock_path(self) -> Path:
        return self._sock_path

    def start(self) -> None:
        self._sock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self._sock_path.exists():
                self._sock_path.unlink()
        except OSError:
            pass

        class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
            allow_reuse_address = True

        self._server = _Server(str(self._sock_path), _Handler)
        self._server.daemon = self._daemon  # type: ignore[attr-defined]

        try:
            os.chmod(self._sock_path, 0o600)
        except OSError:
            pass

        def _serve() -> None:
            assert self._server is not None
            self._server.serve_forever(poll_interval=0.5)

        self._thread = threading.Thread(
            target=_serve, name="runner-control", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._server.server_close()
            except Exception:  # noqa: BLE001
                pass
            self._server = None

        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

        try:
            if self._sock_path.exists():
                self._sock_path.unlink()
        except OSError:
            pass
