"""The resident view server must serve the EXACT payloads the cold CLI serves
(`wayfinder job backtest-view` / `forward-view` / catalog `starters`), in the
exact CLI envelope {"ok": true, "result": ...} — backend callers swap exec'ing
the CLI for a loopback curl byte-for-byte. Bind failure is warn-and-continue:
the daemon scheduler must keep running without it."""

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
from wayfinder_paths.runner.view_server import RunnerViewServer


def _get(port: int, path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


@pytest.fixture
def server(tmp_path: Path):
    view_server = RunnerViewServer(repo_root=tmp_path, port=0)
    view_server.start()
    assert view_server.port is not None
    yield view_server
    view_server.stop()


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


def test_forward_view_price_fetch_is_ttl_cached_and_injected(
    tmp_path: Path, server: RunnerViewServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two polls inside the TTL hit the venue exactly once — the server passes
    its cached single-flight fetcher through load_forward_view's price_fetcher
    seam."""
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

    for _ in range(2):
        status, body = _get(server.port, "/forward-view?job_id=carry")
        assert status == 200
        assert body["ok"] is True
        kinds = {s["kind"] for s in body["result"]["visualization"]["series"]}
        assert "market_price" in kinds

    assert calls == ["carry"]  # second request served from the 90s TTL cache


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
