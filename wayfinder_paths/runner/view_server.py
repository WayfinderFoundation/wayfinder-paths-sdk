"""Resident HTTP view server inside the runner daemon.

The Strategies UI used to fetch starters/backtest/forward payloads by exec'ing
a cold `wayfinder` CLI on the box (~90 CPU-s per request under throttle). This
server keeps those reads warm: a loopback-only ThreadingHTTPServer started by
the daemon (like `RunnerControlServer`) that serves the exact CLI envelope
`{"ok": true, "result": ...}` / `{"ok": false, "error": "..."}` so backend
callers can swap `wayfinder job backtest-view ...` for
`curl http://127.0.0.1:8646/backtest-view?...` byte-for-byte.

Routes (GET only):
- /health
- /starters                                   -> jobs.starters.starter_catalog()
- /backtest-view?job_id=...&view=...&series=...&from=...&to=...&max_points=...&proposal=...
- /forward-view?job_id=...&view=...&series=...&from=...&to=...&max_points=...&no_prices=...

Query params mirror the `wayfinder job backtest-view` / `forward-view` CLI
options 1:1 (same names, same defaults, same coercions). Heavy jobs modules
are lazy-imported inside handlers so daemon startup stays light. Bind failure
is warn-and-continue: a box without a free port keeps running the scheduler,
and the backend falls back to the cold CLI.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from loguru import logger

from wayfinder_paths import __version__

DEFAULT_VIEW_PORT = 8646
PRICE_CACHE_TTL_SECONDS = 90.0


def _view_port_from_env() -> int:
    raw = os.environ.get("WAYFINDER_VIEW_PORT")
    if raw is None or not str(raw).strip():
        return DEFAULT_VIEW_PORT
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            f"Invalid WAYFINDER_VIEW_PORT={raw!r}; using {DEFAULT_VIEW_PORT}"
        )
        return DEFAULT_VIEW_PORT


class _CachedPriceFetcher:
    """Single-flight, per-job TTL cache around the forward view's venue fetch.

    The UI polls the forward chart; without this every poll re-fetches the
    OHLC window from the venue. One in-flight fetch per job (concurrent
    requests wait and reuse the result), successes cached for the TTL,
    failures propagate uncached so `load_forward_view` degrades with its
    normal `price_note` and the next poll can retry.
    """

    def __init__(self, *, ttl_seconds: float = PRICE_CACHE_TTL_SECONDS) -> None:
        self._ttl_seconds = float(ttl_seconds)
        self._lock = threading.Lock()
        self._job_locks: dict[str, threading.Lock] = {}
        self._entries: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    def _job_lock(self, job_id: str) -> threading.Lock:
        with self._lock:
            lock = self._job_locks.get(job_id)
            if lock is None:
                lock = threading.Lock()
                self._job_locks[job_id] = lock
            return lock

    def __call__(
        self, job_id: str, ticks: list[dict[str, Any]], *, store: Any
    ) -> list[dict[str, Any]]:
        with self._job_lock(str(job_id)):
            now = time.monotonic()
            cached = self._entries.get(str(job_id))
            if cached is not None and now - cached[0] < self._ttl_seconds:
                return list(cached[1])

            # Late import + module-attribute call: keeps daemon startup light
            # and the seam monkeypatchable in tests.
            from wayfinder_paths.jobs import forward_artifacts

            series = forward_artifacts._fetch_price_series(job_id, ticks, store=store)
            self._entries[str(job_id)] = (time.monotonic(), series)
            return list(series)


class _ViewRequestError(ValueError):
    """Bad request (missing/invalid query param) -> 400 envelope."""


def _scalar(params: dict[str, list[str]], name: str) -> str | None:
    # Exact query-param names only (job_id, view, from, to, max_points,
    # proposal, series, no_prices) — the backend builds URLs with precisely
    # these spellings. Last occurrence wins, matching click's behavior.
    values = params.get(name)
    if not values:
        return None
    return values[-1]


def _int_param(params: dict[str, list[str]], name: str, default: int) -> int:
    raw = _scalar(params, name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise _ViewRequestError(f"{name} must be an integer, got {raw!r}") from exc


def _flag_param(params: dict[str, list[str]], name: str) -> bool:
    raw = _scalar(params, name)
    if raw is None:
        return False
    return raw.strip().lower() not in ("", "0", "false", "no")


def _require_job_id(params: dict[str, list[str]]) -> str:
    job_id = _scalar(params, "job_id")
    if not job_id:
        raise _ViewRequestError("job_id is required")
    return job_id


class _ViewHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        view_server: RunnerViewServer = self.server.view_server  # type: ignore[attr-defined]
        url = urlsplit(self.path)
        params = parse_qs(url.query, keep_blank_values=True)
        try:
            result = view_server.handle_route(url.path, params)
        except _ViewRequestError as exc:
            self._respond(400, {"ok": False, "error": str(exc)})
            return
        except KeyError:
            self._respond(404, {"ok": False, "error": f"unknown route: {url.path}"})
            return
        except Exception as exc:  # noqa: BLE001 - envelope, never a stack page
            logger.opt(exception=True).warning(f"View server error on {self.path}")
            self._respond(500, {"ok": False, "error": str(exc)})
            return
        self._respond(200, {"ok": True, "result": result})

    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        # Same serialization as the CLI's _echo_json -> byte-shape parity.
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug(f"view-server {self.address_string()} {format % args}")


class RunnerViewServer:
    def __init__(
        self,
        *,
        repo_root: Path,
        host: str = "127.0.0.1",
        port: int | None = None,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._host = host
        self._port = _view_port_from_env() if port is None else int(port)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._price_fetcher = _CachedPriceFetcher()

    @property
    def port(self) -> int | None:
        """The bound port (resolves 0 -> ephemeral), or None if not running."""
        if self._server is None:
            return None
        return int(self._server.server_address[1])

    def start(self) -> None:
        class _Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        try:
            server = _Server((self._host, self._port), _ViewHandler)
        except OSError as exc:
            # Warn + continue: the scheduler must keep running without the
            # warm view endpoint; backend callers fall back to the cold CLI.
            logger.warning(
                f"View server failed to bind {self._host}:{self._port}: {exc}; "
                "continuing without it"
            )
            return
        server.view_server = self  # type: ignore[attr-defined]
        self._server = server

        def _serve() -> None:
            server.serve_forever(poll_interval=0.5)

        self._thread = threading.Thread(
            target=_serve, name="wayfinder-view-server", daemon=True
        )
        self._thread.start()
        logger.info(f"View server listening on http://{self._host}:{self.port}")

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

    # Route handling (called from handler threads)
    def handle_route(self, path: str, params: dict[str, list[str]]) -> Any:
        if path == "/health":
            return {
                "status": "ok",
                "version": __version__,
                "pid": os.getpid(),
                "repo_root": str(self._repo_root),
            }
        if path == "/starters":
            from wayfinder_paths.jobs.starters import starter_catalog

            return starter_catalog()
        if path == "/backtest-view":
            return self._backtest_view(params)
        if path == "/forward-view":
            return self._forward_view(params)
        raise KeyError(path)

    def _store(self) -> Any:
        from wayfinder_paths.jobs.store import JobStore

        return JobStore(repo_root=self._repo_root)

    def _backtest_view(self, params: dict[str, list[str]]) -> dict[str, Any]:
        from wayfinder_paths.jobs.backtest_artifacts import load_backtest_view

        return load_backtest_view(
            _require_job_id(params),
            store=self._store(),
            view=_scalar(params, "view") or "all",
            series_names=list(params.get("series") or []),
            from_ts=_scalar(params, "from"),
            to_ts=_scalar(params, "to"),
            max_points=_int_param(params, "max_points", 1500),
            proposal_id=_scalar(params, "proposal") or None,
        )

    def _forward_view(self, params: dict[str, list[str]]) -> dict[str, Any]:
        from wayfinder_paths.jobs.forward_artifacts import load_forward_view

        return load_forward_view(
            _require_job_id(params),
            store=self._store(),
            view=_scalar(params, "view") or "all",
            series_names=list(params.get("series") or []),
            from_ts=_scalar(params, "from"),
            to_ts=_scalar(params, "to"),
            max_points=_int_param(params, "max_points", 1500),
            include_prices=not _flag_param(params, "no_prices"),
            price_fetcher=self._price_fetcher,
        )
