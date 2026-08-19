"""Memory-flat HTTP view server inside the runner daemon.

The Strategies UI used to fetch starters/backtest/forward payloads by exec'ing
a cold `wayfinder` CLI on the box (~90 CPU-s per request under throttle). This
server keeps those reads warm: a loopback-only ThreadingHTTPServer started by
the daemon (like `RunnerControlServer`) that serves the exact CLI envelope
`{"ok": true, "result": ...}` / `{"ok": false, "error": "..."}` so backend
callers can swap `wayfinder job backtest-view ...` for
`curl http://127.0.0.1:8646/backtest-view?...` byte-for-byte.

Memory contract (the reason for the fork-per-request shape): runnerd
supervises LIVE trading loops and must stay small. Computing views in-process
(pandas + multi-MB JSON graphs per request, across handler threads) grew
runnerd's RSS past 1.4GB and got it kernel-OOM-killed on a 4GB box. So:

- `/backtest-view` and `/forward-view` are computed in a SHORT-LIVED CHILD
  forked from the warm forkserver (`runner/warm_spawn.py`, pandas + jobs
  stack preloaded). The child writes the exact response body bytes to a
  tempfile and exits; the handler streams the file to the socket through a
  64KB buffer. runnerd retains nothing per request.
- At most `MAX_CONCURRENT_VIEW_CHILDREN` children run at once; excess
  requests get a 503 `{"ok": false}` envelope after a short wait and the
  backend falls back to the cold CLI.
- `/starters` and `/health` are tiny and stay in-process; the starters
  catalog is serialized immediately and the object graph dropped.
- The forward view's venue price fetch is cached parent-side as FLAT BYTES
  only (`_PriceSeriesByteCache`): JSON-serialized series, hard-capped per
  entry and in entry count, TTL'd — so UI polling cannot hammer the venue,
  and runnerd's retained footprint stays bounded at a few MB of bytes
  objects (no pandas frames, no dict graphs).

Query params mirror the `wayfinder job backtest-view` / `forward-view` CLI
options 1:1 (same names, same defaults, same coercions), validated in the
parent so bad requests never cost a fork. Bind failure is warn-and-continue:
a box without a free port keeps running the scheduler, and the backend falls
back to the cold CLI.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from loguru import logger

from wayfinder_paths import __version__

DEFAULT_VIEW_PORT = 8646
PRICE_CACHE_TTL_SECONDS = 90.0
PRICE_CACHE_MAX_ENTRY_BYTES = 2_000_000
PRICE_CACHE_MAX_ENTRIES = 8
MAX_CONCURRENT_VIEW_CHILDREN = 2
VIEW_CHILD_BUSY_WAIT_SECONDS = 2.0
VIEW_CHILD_TIMEOUT_SECONDS = 120.0

_CHILD_EXIT_OK = 0
_CHILD_EXIT_ERROR = 5
_RESPONSE_STREAM_CHUNK_BYTES = 64 * 1024


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


def _envelope_bytes(payload: dict[str, Any]) -> bytes:
    # Same serialization as the CLI's _echo_json -> byte-shape parity.
    return json.dumps(payload, indent=2, default=str).encode("utf-8")


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


def _common_request(params: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "job_id": _require_job_id(params),
        "view": _scalar(params, "view") or "all",
        "series_names": list(params.get("series") or []),
        "from_ts": _scalar(params, "from"),
        "to_ts": _scalar(params, "to"),
        "max_points": _int_param(params, "max_points", 1500),
    }


def _backtest_request(params: dict[str, list[str]]) -> dict[str, Any]:
    return {
        **_common_request(params),
        "proposal_id": _scalar(params, "proposal") or None,
    }


def _forward_request(params: dict[str, list[str]]) -> dict[str, Any]:
    return {
        **_common_request(params),
        "include_prices": not _flag_param(params, "no_prices"),
    }


def _replay_price_fetcher(
    cached_prices: bytes,
) -> Callable[..., list[dict[str, Any]]]:
    def _replay(
        job_id: str, ticks: list[dict[str, Any]], *, store: Any
    ) -> list[dict[str, Any]]:
        series: list[dict[str, Any]] = json.loads(cached_prices)
        return series

    return _replay


def _write_price_sidecar(path: str, series: list[dict[str, Any]]) -> None:
    """Serialize the freshly fetched price series for the parent's byte cache.

    Oversize or unserializable series are simply not written — the parent then
    caches nothing and the next poll re-fetches, which is the safe default.
    """
    try:
        data = json.dumps(series, default=str).encode("utf-8")
    except (TypeError, ValueError):
        return
    if len(data) > PRICE_CACHE_MAX_ENTRY_BYTES:
        return
    try:
        Path(path).write_bytes(data)
    except OSError:
        pass


def _view_child_entry(
    *,
    route: str,
    repo_root: str,
    out_path: str,
    request: dict[str, Any],
    cached_prices: bytes | None = None,
    prices_out_path: str | None = None,
) -> int:
    """Compute one view payload. Normally runs inside a short-lived forked
    child (via `_view_child_main`); also runnable inline, which is how tests
    exercise the parent<->child contract without a forkserver.

    Writes the exact HTTP body bytes (CLI envelope) to `out_path` and returns
    the child exit code: `_CHILD_EXIT_OK` for a 200 payload, `_CHILD_EXIT_ERROR`
    for a 500 error envelope.
    """
    try:
        from wayfinder_paths.jobs.store import JobStore

        store = JobStore(repo_root=Path(repo_root))
        if route == "/backtest-view":
            from wayfinder_paths.jobs.backtest_artifacts import load_backtest_view

            result = load_backtest_view(
                request["job_id"],
                store=store,
                view=request["view"],
                series_names=request["series_names"],
                from_ts=request["from_ts"],
                to_ts=request["to_ts"],
                max_points=request["max_points"],
                proposal_id=request["proposal_id"],
            )
        elif route == "/forward-view":
            from wayfinder_paths.jobs.forward_artifacts import load_forward_view

            captured: list[dict[str, Any]] | None = None
            price_fetcher: Callable[..., list[dict[str, Any]]] | None = None
            if request["include_prices"]:
                if cached_prices is not None:
                    price_fetcher = _replay_price_fetcher(cached_prices)
                else:

                    def _capturing_fetch(
                        job_id: str, ticks: list[dict[str, Any]], *, store: Any
                    ) -> list[dict[str, Any]]:
                        nonlocal captured
                        # Module-attribute call keeps the seam monkeypatchable
                        # when the entry runs inline in tests.
                        from wayfinder_paths.jobs import forward_artifacts

                        captured = forward_artifacts._fetch_price_series(
                            job_id, ticks, store=store
                        )
                        return captured

                    price_fetcher = _capturing_fetch
            result = load_forward_view(
                request["job_id"],
                store=store,
                view=request["view"],
                series_names=request["series_names"],
                from_ts=request["from_ts"],
                to_ts=request["to_ts"],
                max_points=request["max_points"],
                include_prices=request["include_prices"],
                price_fetcher=price_fetcher,
            )
            if captured is not None and prices_out_path is not None:
                _write_price_sidecar(prices_out_path, captured)
        else:
            raise KeyError(route)
        Path(out_path).write_bytes(_envelope_bytes({"ok": True, "result": result}))
        return _CHILD_EXIT_OK
    except Exception as exc:  # noqa: BLE001 — envelope, never a stack page
        try:
            Path(out_path).write_bytes(
                _envelope_bytes({"ok": False, "error": str(exc)})
            )
        except OSError:
            pass
        return _CHILD_EXIT_ERROR


def _view_child_main(**kwargs: Any) -> None:
    """Forkserver Process target: run the entry, exit with its code."""
    raise SystemExit(_view_child_entry(**kwargs))


class _PriceSeriesByteCache:
    """Bytes-only TTL cache of the forward view's fetched venue price series.

    The UI polls /forward-view every few seconds and each poll runs in a fresh
    child, so without parent-side state every poll would re-fetch the OHLC
    window from the venue. The parent keeps ONLY the JSON-serialized series
    bytes — hard-capped per entry and in entry count — so runnerd's retained
    footprint is a few flat bytes objects, never pandas frames or dict graphs.
    Failures are never cached: the child only writes a sidecar after a
    successful fetch, so degraded responses retry on the next poll.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = PRICE_CACHE_TTL_SECONDS,
        max_entry_bytes: int = PRICE_CACHE_MAX_ENTRY_BYTES,
        max_entries: int = PRICE_CACHE_MAX_ENTRIES,
    ) -> None:
        self._ttl_seconds = float(ttl_seconds)
        self._max_entry_bytes = int(max_entry_bytes)
        self._max_entries = int(max_entries)
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, bytes]] = {}

    def get(self, job_id: str) -> bytes | None:
        with self._lock:
            entry = self._entries.get(job_id)
            if entry is None:
                return None
            if time.monotonic() - entry[0] >= self._ttl_seconds:
                del self._entries[job_id]
                return None
            return entry[1]

    def put(self, job_id: str, data: bytes) -> None:
        if len(data) > self._max_entry_bytes:
            return
        with self._lock:
            now = time.monotonic()
            self._entries = {
                key: value
                for key, value in self._entries.items()
                if now - value[0] < self._ttl_seconds
            }
            while len(self._entries) >= self._max_entries:
                oldest = min(self._entries, key=lambda key: self._entries[key][0])
                del self._entries[oldest]
            self._entries[job_id] = (now, data)


@dataclass
class _ViewResponse:
    status: int
    body_bytes: bytes | None = None
    # Child-written body streamed to the socket, then unlinked by the handler.
    body_path: Path | None = None


class _ViewHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        view_server: RunnerViewServer = self.server.view_server  # type: ignore[attr-defined]
        url = urlsplit(self.path)
        params = parse_qs(url.query, keep_blank_values=True)
        try:
            response = view_server.render(url.path, params)
        except _ViewRequestError as exc:
            self._respond_bytes(400, _envelope_bytes({"ok": False, "error": str(exc)}))
            return
        except KeyError:
            self._respond_bytes(
                404,
                _envelope_bytes({"ok": False, "error": f"unknown route: {url.path}"}),
            )
            return
        except Exception as exc:  # noqa: BLE001 - envelope, never a stack page
            logger.opt(exception=True).warning(f"View server error on {self.path}")
            self._respond_bytes(500, _envelope_bytes({"ok": False, "error": str(exc)}))
            return
        if response.body_path is not None:
            self._respond_file(response.status, response.body_path)
        else:
            self._respond_bytes(response.status, response.body_bytes or b"")

    def _respond_bytes(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_file(self, status: int, path: Path) -> None:
        # Stream the child-written body straight to the socket: runnerd never
        # holds a multi-MB view payload, only a 64KB copy buffer.
        try:
            size = path.stat().st_size
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with path.open("rb") as body:
                shutil.copyfileobj(
                    body, self.wfile, length=_RESPONSE_STREAM_CHUNK_BYTES
                )
        finally:
            path.unlink(missing_ok=True)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug(f"view-server {self.address_string()} {format % args}")


class RunnerViewServer:
    def __init__(
        self,
        *,
        repo_root: Path,
        host: str = "127.0.0.1",
        port: int | None = None,
        warm_spawner: Any | None = None,
        max_view_children: int = MAX_CONCURRENT_VIEW_CHILDREN,
        busy_wait_seconds: float = VIEW_CHILD_BUSY_WAIT_SECONDS,
        child_timeout_seconds: float = VIEW_CHILD_TIMEOUT_SECONDS,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._host = host
        self._port = _view_port_from_env() if port is None else int(port)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # Shared with the daemon's warm tick spawner when running inside
        # runnerd — one forkserver image (same preload) serves both tick forks
        # and view forks. Created lazily when standalone.
        self._warm_spawner = warm_spawner
        self._spawner_lock = threading.Lock()
        self._children = threading.BoundedSemaphore(int(max_view_children))
        self._busy_wait_seconds = float(busy_wait_seconds)
        self._child_timeout_seconds = float(child_timeout_seconds)
        self._price_cache = _PriceSeriesByteCache()

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
    def render(self, path: str, params: dict[str, list[str]]) -> _ViewResponse:
        if path == "/health":
            return _ViewResponse(
                200,
                body_bytes=_envelope_bytes(
                    {
                        "ok": True,
                        "result": {
                            "status": "ok",
                            "version": __version__,
                            "pid": os.getpid(),
                            "repo_root": str(self._repo_root),
                        },
                    }
                ),
            )
        if path == "/starters":
            from wayfinder_paths.jobs.starters import starter_catalog

            catalog = starter_catalog()
            body = _envelope_bytes({"ok": True, "result": catalog})
            # Serialize immediately and drop the object graph — the resident
            # daemon retains nothing between requests.
            del catalog
            return _ViewResponse(200, body_bytes=body)
        if path == "/backtest-view":
            return self._forked_view(path, _backtest_request(params))
        if path == "/forward-view":
            return self._forked_view(path, _forward_request(params))
        raise KeyError(path)

    def _forked_view(self, route: str, request: dict[str, Any]) -> _ViewResponse:
        if not self._children.acquire(timeout=self._busy_wait_seconds):
            return _ViewResponse(
                503,
                body_bytes=_envelope_bytes(
                    {
                        "ok": False,
                        "error": "view server busy: too many concurrent view builds",
                    }
                ),
            )
        try:
            out_path = _view_tempfile("wayfinder-view-")
            prices_out: Path | None = None
            try:
                kwargs: dict[str, Any] = {
                    "route": route,
                    "repo_root": str(self._repo_root),
                    "out_path": str(out_path),
                    "request": request,
                }
                if route == "/forward-view" and request["include_prices"]:
                    cached = self._price_cache.get(request["job_id"])
                    if cached is not None:
                        kwargs["cached_prices"] = cached
                    else:
                        prices_out = _view_tempfile("wayfinder-view-prices-")
                        kwargs["prices_out_path"] = str(prices_out)

                exitcode = self._run_child(kwargs)

                if prices_out is not None:
                    self._absorb_price_sidecar(request["job_id"], prices_out)
                    prices_out = None
                if exitcode is None:
                    return self._child_failure(
                        out_path,
                        f"view child timed out after "
                        f"{self._child_timeout_seconds:.0f}s",
                    )
                if exitcode not in (_CHILD_EXIT_OK, _CHILD_EXIT_ERROR):
                    return self._child_failure(
                        out_path, f"view child failed (exit={exitcode})"
                    )
                if not out_path.exists() or out_path.stat().st_size == 0:
                    return self._child_failure(
                        out_path, f"view child wrote no output (exit={exitcode})"
                    )
                status = 200 if exitcode == _CHILD_EXIT_OK else 500
                return _ViewResponse(status, body_path=out_path)
            except Exception:
                out_path.unlink(missing_ok=True)
                raise
            finally:
                if prices_out is not None:
                    prices_out.unlink(missing_ok=True)
        finally:
            self._children.release()

    @staticmethod
    def _child_failure(out_path: Path, message: str) -> _ViewResponse:
        out_path.unlink(missing_ok=True)
        return _ViewResponse(
            500, body_bytes=_envelope_bytes({"ok": False, "error": message})
        )

    def _run_child(self, kwargs: dict[str, Any]) -> int | None:
        """Fork the view child from the warm forkserver and wait for it.

        Returns the child's exit code, or None if it exceeded the timeout and
        was killed. Tests monkeypatch this seam to run `_view_child_entry`
        inline or to simulate crashes/stalls.
        """
        process = self._spawn_context().Process(
            target=_view_child_main,
            kwargs=kwargs,
            name="wayfinder-view-child",
            daemon=False,
        )
        process.start()
        process.join(self._child_timeout_seconds)
        if process.is_alive():
            process.kill()
            process.join(5)
            return None
        exitcode = process.exitcode
        return None if exitcode is None else int(exitcode)

    def _spawn_context(self) -> Any:
        with self._spawner_lock:
            if self._warm_spawner is None:
                from wayfinder_paths.runner.warm_spawn import WarmSpawner

                self._warm_spawner = WarmSpawner()
            return self._warm_spawner.context()

    def _absorb_price_sidecar(self, job_id: str, path: Path) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            return
        finally:
            path.unlink(missing_ok=True)
        if data:
            self._price_cache.put(job_id, data)


def _view_tempfile(prefix: str) -> Path:
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=".json")
    os.close(fd)
    return Path(name)
