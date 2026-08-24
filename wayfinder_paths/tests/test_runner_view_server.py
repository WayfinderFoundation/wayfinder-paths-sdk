"""The view server must serve the EXACT payloads the cold CLI serves
(`wayfinder job backtest-view` / `forward-view` / catalog `starters`), in the
exact CLI envelope {"ok": true, "result": ...} — backend callers swap exec'ing
the CLI for a loopback curl byte-for-byte. Views are computed in short-lived
children forked from the warm forkserver so runnerd stays memory-flat: the
parent retains nothing per request beyond a capped bytes-only price cache.
Excess concurrency 503s, child failures become {"ok": false} envelopes (never
hangs), and bind failure is warn-and-continue: the daemon scheduler must keep
running without it. A parent-side RSS watchdog exits runnerd cleanly between
ticks before the kernel OOM killer can take the live loops down mid-write."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from wayfinder_paths.jobs.backtest_artifacts import load_backtest_view
from wayfinder_paths.jobs.forward_artifacts import load_forward_view
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.runner.view_server import (
    RunnerViewServer,
    _PriceSeriesByteCache,
    _view_child_entry,
)
from wayfinder_paths.runner.warm_spawn import WarmSpawner


def _get(port: int, path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}", timeout=60
        ) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


@pytest.fixture(scope="module")
def shared_spawner() -> WarmSpawner:
    # One forkserver (with the pandas/jobs preload) for the whole module —
    # exactly how the daemon shares its tick spawner with the view server.
    return WarmSpawner()


@pytest.fixture
def server(tmp_path: Path, shared_spawner: WarmSpawner):
    view_server = RunnerViewServer(
        repo_root=tmp_path, port=0, warm_spawner=shared_spawner
    )
    view_server.start()
    assert view_server.port is not None
    yield view_server
    view_server.stop()


def _inline_children(
    view_server: RunnerViewServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run view children inline (no fork) so monkeypatched seams inside the
    jobs modules are visible to the 'child' — the parent<->child contract
    (request kwargs, out file, price sidecar, exit codes) stays fully real."""
    monkeypatch.setattr(
        view_server, "_run_child", lambda kwargs: _view_child_entry(**kwargs)
    )


def _seed_backtest_job(tmp_path: Path) -> JobStore:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("carry", script="strategy.py", interval_seconds=3600)
    store.create_job(job)
    visualization = {
        "schema_version": "1.0",
        "symbols": ["TEST"],
        "series": [
            {
                "name": "TEST_price",
                "kind": "market_price",
                "symbol": "TEST",
                "points": [
                    {
                        "timestamp": f"2026-07-14T{hour:02d}:00:00+00:00",
                        "value": 1.0 + hour,
                    }
                    for hour in range(24)
                ],
            },
            {
                "name": "equity",
                "kind": "equity_curve",
                "symbol": None,
                "points": [
                    {
                        "timestamp": f"2026-07-14T{hour:02d}:00:00+00:00",
                        "value": 100.0 + hour,
                    }
                    for hour in range(24)
                ],
            },
        ],
        "markers": [
            {
                "timestamp": "2026-07-14T02:00:00+00:00",
                "symbol": "TEST",
                "kind": "entry",
            }
        ],
        "validation": {"status": "passed"},
    }
    latest = {
        "run_id": "run-1",
        "stats": {"net_pnl": 1.5},
        "trades": [{"symbol": "TEST", "net_pnl": 1.5}],
        "validation": {"status": "passed"},
    }
    store.write_json("carry", "results/backtest/visualization.json", visualization)
    store.write_json("carry", "results/backtest/latest.json", latest)
    return store


def _seed_forward_job(tmp_path: Path) -> JobStore:
    """Trimmed version of the forward-view artifact fixture
    (test_jobs_forward_view): fills + trades + ticks jsonl."""
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("carry", script="strategy.py", interval_seconds=3600)
    job.execution_params["initial_capital"] = 100.0
    store.create_job(job)
    forward = store.job_dir("carry") / "results" / "forward"
    forward.mkdir(parents=True, exist_ok=True)

    def write_jsonl(name: str, rows: list[dict]) -> None:
        (forward / name).write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )

    write_jsonl(
        "fills.jsonl",
        [
            {
                "kind": "fill",
                "timestamp": "2026-07-14T05:00:00+00:00",
                "symbol": "IMX",
                "side": "sell",
                "avg_price": 0.130,
                "filled_size": 700.0,
                "reduce_only": False,
                "status": "filled",
                "mode": "paper",
            },
            {
                "kind": "fill",
                "timestamp": "2026-07-15T10:00:00+00:00",
                "symbol": "IMX",
                "side": "buy",
                "avg_price": 0.128,
                "filled_size": 700.0,
                "reduce_only": True,
                "status": "filled",
                "mode": "paper",
            },
        ],
    )
    write_jsonl(
        "trades.jsonl",
        [
            {
                "kind": "trade",
                "symbol": "IMX",
                "side": "buy",
                "net_pnl": 1.4,
                "closed_at": "2026-07-15T10:00:00+00:00",
                "mode": "paper",
            }
        ],
    )
    write_jsonl(
        "ticks.jsonl",
        [
            {
                "kind": "tick",
                "bar_ts": f"2026-07-14T{hour:02d}:00:00+00:00",
                "mode": "paper",
                "ledger": {"realized_pnl": 0.0, "positions": {}},
            }
            for hour in range(4)
        ]
        + [
            {
                "kind": "tick",
                "bar_ts": "2026-07-15T10:00:00+00:00",
                "mode": "paper",
                "ledger": {"realized_pnl": 1.4, "positions": {}},
            }
        ],
    )
    return store


def test_health_route(server: RunnerViewServer) -> None:
    status, body = _get(server.port, "/health")
    assert status == 200
    assert body["ok"] is True
    assert body["result"]["status"] == "ok"


def test_starters_route_serves_the_bare_catalog_list(server: RunnerViewServer) -> None:
    from wayfinder_paths.jobs.starters import starter_catalog

    status, body = _get(server.port, "/starters")
    assert status == 200
    assert body["ok"] is True
    assert isinstance(body["result"], list)
    assert body["result"] == json.loads(json.dumps(starter_catalog(), default=str))


def test_backtest_view_parity_with_direct_loader(
    tmp_path: Path, server: RunnerViewServer
) -> None:
    """Served through a REAL forked child — the response must still match the
    direct loader byte-for-byte after JSON parsing."""
    _seed_backtest_job(tmp_path)
    query = (
        "job_id=carry&view=legs&series=TEST_price&max_points=100"
        "&from=2026-07-14T01:00:00%2B00:00&to=2026-07-14T20:00:00%2B00:00"
    )

    status, body = _get(server.port, f"/backtest-view?{query}")

    direct = load_backtest_view(
        "carry",
        store=JobStore(repo_root=tmp_path),
        view="legs",
        series_names=["TEST_price"],
        from_ts="2026-07-14T01:00:00+00:00",
        to_ts="2026-07-14T20:00:00+00:00",
        max_points=100,
        proposal_id=None,
    )
    assert status == 200
    assert body == {"ok": True, "result": json.loads(json.dumps(direct, default=str))}
    assert body["result"]["available"] is True
    assert [s["name"] for s in body["result"]["visualization"]["series"]] == [
        "TEST_price"
    ]


def test_forward_view_parity_with_direct_loader(
    tmp_path: Path, server: RunnerViewServer
) -> None:
    _seed_forward_job(tmp_path)

    status, body = _get(server.port, "/forward-view?job_id=carry&view=all&no_prices=1")

    direct = load_forward_view(
        "carry",
        store=JobStore(repo_root=tmp_path),
        view="all",
        include_prices=False,
    )
    assert status == 200
    assert body == {"ok": True, "result": json.loads(json.dumps(direct, default=str))}
    assert body["result"]["summary"]["pnl_by_mode"] == {"paper": 1.4, "live": 0.0}


def test_forward_view_price_fetch_is_byte_cached_across_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two polls inside the TTL hit the venue exactly once: the first child
    hands the serialized series back through the sidecar file, the parent
    caches ONLY those bytes, and the second child replays them without
    fetching. Children run inline so the fake venue fetch is visible."""
    import wayfinder_paths.jobs.forward_artifacts as forward_artifacts

    _seed_forward_job(tmp_path)
    calls: list[str] = []

    def _fake_fetch(job_id: str, ticks: list[dict], *, store) -> list[dict]:
        calls.append(job_id)
        return [
            {
                "name": "IMX_price",
                "kind": "market_price",
                "symbol": "IMX",
                "venue": "fake",
                "points": [],
            }
        ]

    monkeypatch.setattr(forward_artifacts, "_fetch_price_series", _fake_fetch)

    view_server = RunnerViewServer(repo_root=tmp_path, port=0)
    _inline_children(view_server, monkeypatch)
    view_server.start()
    try:
        for _ in range(2):
            status, body = _get(view_server.port, "/forward-view?job_id=carry")
            assert status == 200
            assert body["ok"] is True
            kinds = {s["kind"] for s in body["result"]["visualization"]["series"]}
            assert "market_price" in kinds

        assert calls == ["carry"]  # second request replayed the cached bytes
        assert view_server._price_cache.get("carry") is not None
    finally:
        view_server.stop()


def test_price_cache_is_bytes_only_with_hard_caps() -> None:
    """runnerd's only per-request retention: flat bytes, capped per entry and
    in entry count, TTL'd."""
    cache = _PriceSeriesByteCache(ttl_seconds=0.05, max_entry_bytes=8, max_entries=2)

    cache.put("big", b"123456789")  # over the entry cap -> silently dropped
    assert cache.get("big") is None

    cache.put("a", b"aa")
    cache.put("b", b"bb")
    cache.put("c", b"cc")  # entry-count cap evicts the oldest
    assert cache.get("a") is None
    assert cache.get("b") == b"bb"
    assert cache.get("c") == b"cc"

    time.sleep(0.06)
    assert cache.get("b") is None  # TTL expired


def test_third_concurrent_view_request_gets_a_503_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At most 2 view children run at once; a third concurrent request gets a
    quick 503 {"ok": false} so the backend can fall back to the cold CLI."""
    _seed_backtest_job(tmp_path)
    view_server = RunnerViewServer(repo_root=tmp_path, port=0, busy_wait_seconds=0.2)
    release = threading.Event()
    started: list[str] = []

    def _blocking_child(kwargs: dict) -> int:
        Path(kwargs["out_path"]).write_bytes(
            json.dumps({"ok": True, "result": None}).encode("utf-8")
        )
        started.append(kwargs["route"])
        release.wait(timeout=30)
        return 0

    monkeypatch.setattr(view_server, "_run_child", _blocking_child)
    view_server.start()
    results: list[tuple[int, dict]] = []
    threads = [
        threading.Thread(
            target=lambda: results.append(
                _get(view_server.port, "/backtest-view?job_id=carry")
            )
        )
        for _ in range(2)
    ]
    try:
        for thread in threads:
            thread.start()
        deadline = time.time() + 10
        while len(started) < 2 and time.time() < deadline:
            time.sleep(0.02)
        assert len(started) == 2  # both semaphore slots held

        status, body = _get(view_server.port, "/backtest-view?job_id=carry")
        assert status == 503
        assert body["ok"] is False
        assert "busy" in body["error"]
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=10)
        view_server.stop()
    assert sorted(status for status, _ in results) == [200, 200]


def test_child_crash_is_a_500_envelope_not_a_hang(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child that dies without writing its output file (signal death, OOM)
    must surface as {"ok": false} immediately."""
    _seed_backtest_job(tmp_path)
    view_server = RunnerViewServer(repo_root=tmp_path, port=0)
    monkeypatch.setattr(view_server, "_run_child", lambda kwargs: 1)
    view_server.start()
    try:
        status, body = _get(view_server.port, "/backtest-view?job_id=carry")
        assert status == 500
        assert body["ok"] is False
        assert "exit=1" in body["error"]
    finally:
        view_server.stop()


def test_child_timeout_is_a_500_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_backtest_job(tmp_path)
    view_server = RunnerViewServer(repo_root=tmp_path, port=0)
    monkeypatch.setattr(view_server, "_run_child", lambda kwargs: None)
    view_server.start()
    try:
        status, body = _get(view_server.port, "/backtest-view?job_id=carry")
        assert status == 500
        assert body["ok"] is False
        assert "timed out" in body["error"]
    finally:
        view_server.stop()


def test_forked_child_loader_error_is_a_500_envelope(
    tmp_path: Path, server: RunnerViewServer
) -> None:
    """Real fork path: a loader exception inside the child (corrupt artifact)
    comes back as the same 500 envelope the in-process server produced."""
    store = _seed_forward_job(tmp_path)
    ticks = store.job_dir("carry") / "results" / "forward" / "ticks.jsonl"
    ticks.write_text('{"kind": "tick", not json\n', encoding="utf-8")

    status, body = _get(server.port, "/forward-view?job_id=carry&no_prices=1")
    assert status == 500
    assert body["ok"] is False
    assert body["error"]


def test_missing_job_id_is_a_400_envelope(server: RunnerViewServer) -> None:
    status, body = _get(server.port, "/backtest-view?view=all")
    assert status == 400
    assert body["ok"] is False
    assert "job_id" in body["error"]


def test_unknown_route_is_a_404_envelope(server: RunnerViewServer) -> None:
    status, body = _get(server.port, "/nope")
    assert status == 404
    assert body["ok"] is False


def test_invalid_max_points_is_a_400_envelope(
    tmp_path: Path, server: RunnerViewServer
) -> None:
    _seed_backtest_job(tmp_path)
    status, body = _get(server.port, "/backtest-view?job_id=carry&max_points=lots")
    assert status == 400
    assert body["ok"] is False
    assert "max_points" in body["error"]


def test_bind_failure_warns_and_continues(tmp_path: Path) -> None:
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    taken_port = blocker.getsockname()[1]
    try:
        view_server = RunnerViewServer(repo_root=tmp_path, port=taken_port)
        view_server.start()  # must not raise
        assert view_server.port is None
        view_server.stop()  # idempotent no-op
    finally:
        blocker.close()


def test_daemon_starts_and_stops_the_view_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Started like RunnerControlServer: alive while the daemon runs, gone
    after shutdown. WAYFINDER_VIEW_PORT=0 -> ephemeral port for the test."""
    import shutil

    from wayfinder_paths.runner.client import RunnerControlClient
    from wayfinder_paths.runner.daemon import RunnerDaemon
    from wayfinder_paths.runner.paths import RunnerPaths

    monkeypatch.setenv("WAYFINDER_VIEW_PORT", "0")
    # Short /tmp dir: pytest tmp_path is too long for AF_UNIX socket paths
    # (same pattern as test_runner_e2e).
    runner_dir = Path("/tmp") / f"wf-viewsrv-{time.time_ns()}"
    runner_dir.mkdir(parents=True, exist_ok=True)
    paths = RunnerPaths(
        repo_root=tmp_path,
        runner_dir=runner_dir,
        db_path=runner_dir / "state.db",
        logs_dir=runner_dir / "logs",
        sock_path=runner_dir / "runner.sock",
    )
    daemon = RunnerDaemon(paths=paths, tick_seconds=0.05)
    thread = threading.Thread(target=daemon.start, name="runner-view-daemon")
    thread.start()
    client = RunnerControlClient(sock_path=paths.sock_path)
    try:
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if client.call("status").get("ok"):
                break
            time.sleep(0.1)
        assert daemon._view_server is not None
        port = daemon._view_server.port
        assert port is not None
        # The view server shares the daemon's warm spawner: one forkserver
        # image serves both tick forks and view forks.
        assert daemon._view_server._warm_spawner is daemon._warm_spawner

        status, body = _get(port, "/health")
        assert status == 200
        assert body["ok"] is True
    finally:
        try:
            client.call("shutdown")
        except Exception:  # noqa: BLE001
            daemon.stop()
        thread.join(timeout=5)
        shutil.rmtree(runner_dir, ignore_errors=True)
    assert not thread.is_alive()
    assert daemon._view_server is None


def _daemon_paths(tmp_path: Path):
    from wayfinder_paths.runner.paths import RunnerPaths

    runner_dir = tmp_path / ".wayfinder" / "runner"
    return RunnerPaths(
        repo_root=tmp_path,
        runner_dir=runner_dir,
        db_path=runner_dir / "state.db",
        logs_dir=runner_dir / "logs",
        sock_path=runner_dir / "runner.sock",
    )


def test_memory_watchdog_exits_cleanly_when_rss_exceeds_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RSS over WAYFINDER_RUNNERD_MAX_RSS_MB -> os._exit(1) at the tick
    boundary, where no run bookkeeping is mid-flight, so the supervisor
    restarts runnerd cleanly instead of the kernel OOM-killing it."""
    import os as os_module

    from wayfinder_paths.runner import daemon as daemon_module

    monkeypatch.setenv("WAYFINDER_RUNNERD_MAX_RSS_MB", "900")
    daemon = daemon_module.RunnerDaemon(paths=_daemon_paths(tmp_path))
    exits: list[int] = []
    monkeypatch.setattr(daemon_module, "_rss_mb", lambda: 1200.0)
    monkeypatch.setattr(os_module, "_exit", lambda code: exits.append(code))

    daemon.tick()
    assert exits == [1]


def test_memory_watchdog_stays_quiet_below_limit_and_without_procfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os as os_module

    from wayfinder_paths.runner import daemon as daemon_module

    monkeypatch.delenv("WAYFINDER_RUNNERD_MAX_RSS_MB", raising=False)
    daemon = daemon_module.RunnerDaemon(paths=_daemon_paths(tmp_path))
    assert daemon._max_rss_mb == daemon_module.DEFAULT_MAX_RSS_MB
    exits: list[int] = []
    monkeypatch.setattr(os_module, "_exit", lambda code: exits.append(code))

    monkeypatch.setattr(daemon_module, "_rss_mb", lambda: 100.0)
    daemon.tick()  # well below the limit
    monkeypatch.setattr(daemon_module, "_rss_mb", lambda: None)
    daemon.tick()  # no /proc (macOS) -> watchdog disabled
    assert exits == []


def test_memory_watchdog_disabled_by_non_positive_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os as os_module

    from wayfinder_paths.runner import daemon as daemon_module

    monkeypatch.setenv("WAYFINDER_RUNNERD_MAX_RSS_MB", "0")
    daemon = daemon_module.RunnerDaemon(paths=_daemon_paths(tmp_path))
    exits: list[int] = []
    monkeypatch.setattr(daemon_module, "_rss_mb", lambda: 4096.0)
    monkeypatch.setattr(os_module, "_exit", lambda code: exits.append(code))

    daemon.tick()
    assert exits == []


def _write_applying_proposal(
    repo_root: Path, job_id: str, proposal_id: str, *, status: str = "applying"
) -> None:
    proposals = repo_root / ".wayfinder" / "jobs" / job_id / "proposals"
    proposals.mkdir(parents=True, exist_ok=True)
    (proposals / f"{proposal_id}.json").write_text(
        json.dumps({"proposal_id": proposal_id, "application": {"status": status}}),
        encoding="utf-8",
    )


def test_memory_watchdog_defers_exit_while_apply_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RSS over cap while a proposal application is applying → the restart
    exit is deferred (an os._exit would orphan the apply mid-flight and leave
    the job's loops paused; observed live 2026-08-24). Re-checked next tick:
    once the apply is terminal, the deferred exit fires."""
    import os as os_module

    from wayfinder_paths.runner import daemon as daemon_module

    monkeypatch.setenv("WAYFINDER_RUNNERD_MAX_RSS_MB", "900")
    _write_applying_proposal(tmp_path, "majors-5m-lab", "prop-params-update")
    daemon = daemon_module.RunnerDaemon(paths=_daemon_paths(tmp_path))
    exits: list[int] = []
    monkeypatch.setattr(daemon_module, "_rss_mb", lambda: 1200.0)
    monkeypatch.setattr(os_module, "_exit", lambda code: exits.append(code))

    daemon.tick()
    assert exits == [], "exit deferred while the apply is in flight"

    _write_applying_proposal(
        tmp_path, "majors-5m-lab", "prop-params-update", status="applied"
    )
    daemon.tick()
    assert exits == [1], "deferred exit fires once no apply is in flight"


def test_memory_watchdog_hard_override_exits_and_journals_the_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past cap × 1.5 the daemon exits even mid-apply — an apply must not
    hold a ballooning daemon hostage — and journals which apply was in
    flight so recovery knows why it was orphaned."""
    import os as os_module

    from wayfinder_paths.runner import daemon as daemon_module

    monkeypatch.setenv("WAYFINDER_RUNNERD_MAX_RSS_MB", "900")
    _write_applying_proposal(tmp_path, "majors-5m-lab", "prop-params-update")
    daemon = daemon_module.RunnerDaemon(paths=_daemon_paths(tmp_path))
    exits: list[int] = []
    monkeypatch.setattr(daemon_module, "_rss_mb", lambda: 1400.0)
    monkeypatch.setattr(os_module, "_exit", lambda code: exits.append(code))

    daemon.tick()

    assert exits == [1]
    journal = (
        tmp_path / ".wayfinder" / "jobs" / "majors-5m-lab" / "journal.jsonl"
    ).read_text(encoding="utf-8")
    rows = [json.loads(line) for line in journal.splitlines() if line.strip()]
    breadcrumbs = [r for r in rows if r["type"] == "runnerd_rss_exit_during_apply"]
    assert len(breadcrumbs) == 1
    assert breadcrumbs[0]["proposal_id"] == "prop-params-update"
    assert breadcrumbs[0]["rss_mb"] == 1400.0
    assert breadcrumbs[0]["max_rss_mb"] == 900.0
