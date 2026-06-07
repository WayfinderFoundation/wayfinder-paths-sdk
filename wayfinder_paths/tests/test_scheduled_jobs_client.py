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


def test_bulk_sync_posts_jobs(cloud_env) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.read()
        return httpx.Response(200, json={"synced": 1, "deleted": 0})

    c = _make_client(handler)
    c.bulk_sync(
        [{"job_name": "a", "status": "active", "interval_seconds": 60, "payload": {}}]
    )

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.test/opencode/instances/inst-xyz/jobs/sync/"
    assert b"job_name" in captured["body"]


def test_bulk_sync_swallows_5xx(cloud_env) -> None:
    c = _make_client(lambda _req: httpx.Response(500))
    c.bulk_sync([{"job_name": "a"}])


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
