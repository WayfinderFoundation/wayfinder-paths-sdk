from __future__ import annotations

import httpx
import pytest

from wayfinder_paths.core.clients.ScheduledJobsClient import ScheduledJobsClient


@pytest.fixture
def cloud_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "inst-xyz")
    monkeypatch.setattr(
        "wayfinder_paths.core.clients.ScheduledJobsClient.get_api_base_url",
        lambda: "https://api.test",
    )
    monkeypatch.setattr(
        "wayfinder_paths.core.clients.ScheduledJobsClient.get_api_key",
        lambda: "wk_test",
    )


def _make_client(handler) -> ScheduledJobsClient:
    c = ScheduledJobsClient()
    c._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
    return c


def test_list_jobs_returns_rows(cloud_env) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["api_key"] = request.headers.get("X-API-KEY")
        return httpx.Response(
            200, json=[{"job_name": "a", "status": "active", "interval_seconds": 60}]
        )

    c = _make_client(handler)
    rows = c.list_jobs()

    assert rows == [{"job_name": "a", "status": "active", "interval_seconds": 60}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://api.test/opencode/instances/inst-xyz/jobs/"
    assert captured["api_key"] == "wk_test"


def test_list_jobs_http_error_returns_empty(cloud_env) -> None:
    c = _make_client(lambda _req: httpx.Response(500))
    assert c.list_jobs() == []


def test_sync_job_puts_with_payload(cloud_env) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.read()
        return httpx.Response(200, json={})

    c = _make_client(handler)
    c.sync_job("my-job", {"status": "active", "interval_seconds": 60, "payload": {}})

    assert captured["method"] == "PUT"
    assert (
        captured["url"] == "https://api.test/opencode/instances/inst-xyz/jobs/my-job/"
    )
    assert b"active" in captured["body"]


def test_sync_job_swallows_4xx(cloud_env) -> None:
    c = _make_client(lambda _req: httpx.Response(404))
    c.sync_job("my-job", {"status": "active", "interval_seconds": 60, "payload": {}})


def test_delete_job_issues_delete(cloud_env) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(204)

    c = _make_client(handler)
    c.delete_job("my-job")

    assert captured["method"] == "DELETE"
    assert (
        captured["url"] == "https://api.test/opencode/instances/inst-xyz/jobs/my-job/"
    )


def test_delete_job_swallows_5xx(cloud_env) -> None:
    c = _make_client(lambda _req: httpx.Response(500))
    c.delete_job("my-job")


def test_report_run_posts_run_data(cloud_env) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.read()
        return httpx.Response(201)

    c = _make_client(handler)
    c.report_run("my-job", {"run_id": "1", "status": "OK"})

    assert captured["method"] == "POST"
    assert (
        captured["url"]
        == "https://api.test/opencode/instances/inst-xyz/jobs/my-job/runs/"
    )
    assert b"run_id" in captured["body"]


def test_report_run_swallows_4xx(cloud_env) -> None:
    c = _make_client(lambda _req: httpx.Response(403))
    c.report_run("my-job", {"run_id": "1", "status": "OK"})
