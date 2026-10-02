from __future__ import annotations

from typing import Any

import httpx
import pytest

from wayfinder_paths.core.clients import OpenCodeClient as client_module
from wayfinder_paths.core.clients.OpenCodeClient import OpenCodeClient


def _session(session_id: str, updated: int, **extra: Any) -> dict[str, Any]:
    return {"id": session_id, "time": {"created": 0, "updated": updated}, **extra}


def _tool(
    created: int,
    *,
    command: str | None = None,
    action: str = "add_job",
    name: str = "funding-watch",
) -> dict[str, Any]:
    return {
        "info": {"role": "assistant", "time": {"created": created}},
        "parts": [
            {
                "type": "tool",
                "tool": "bash" if command is not None else "wayfinder_core_runner",
                "state": {
                    "input": {"command": command}
                    if command is not None
                    else {"action": action, "name": name},
                    "time": {"start": created},
                },
            }
        ],
    }


def _client_with(
    sessions: list[dict[str, Any]], messages: dict[str, Any]
) -> OpenCodeClient:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session":
            return httpx.Response(200, json=sessions)
        for session in sessions:
            if request.url.path == f"/session/{session['id']}/message":
                return httpx.Response(200, json=messages.get(session["id"], []))
            if request.url.path == f"/session/{session['id']}":
                return httpx.Response(200, json=session)
        return httpx.Response(404, json={"name": "NotFoundError"})

    client = OpenCodeClient()
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(respond))
    return client


@pytest.mark.parametrize(
    "command",
    [
        "wayfinder runner add-job --name funding-watch --type script",
        "poetry run wayfinder runner update-job --name='funding-watch' --payload-json '{}'",
        "/wf/sdk/.venv/bin/wayfinder runner resume funding-watch",
        "/wf/sdk/.venv/bin/python -m wayfinder_paths.mcp.cli runner run-once funding-watch",
        "cd /wf/sdk && wayfinder runner run-once funding-watch",
        "cat > watcher.py <<'PY'\nprint({'type': 'job_result'})\nPY\nwayfinder runner add-job --name funding-watch",
    ],
)
def test_matches_exact_cli_action(command: str) -> None:
    client = _client_with(
        [_session("caller", 10)], {"caller": [_tool(5, command=command)]}
    )
    assert client.find_session_referencing_job("funding-watch") == "caller"


@pytest.mark.parametrize("action", ["add_job", "update_job", "resume_job", "run_once"])
def test_matches_exact_mcp_action(action: str) -> None:
    client = _client_with(
        [_session("caller", 10)], {"caller": [_tool(5, action=action)]}
    )
    assert client.find_session_referencing_job("funding-watch") == "caller"


@pytest.mark.parametrize(
    "message",
    [
        _tool(100, command="wayfinder runner add-job --name funding-watch-eth"),
        _tool(100, command="wayfinder runner resume funding-watch-eth"),
        _tool(100, command="echo 'wayfinder runner resume funding-watch'"),
        _tool(
            100,
            command="cat > example.sh <<'SH'\nwayfinder runner resume funding-watch\nSH",
        ),
        _tool(100, command="wayfinder runner status"),
        _tool(100, action="status"),
        _tool(100, name="funding-watch-eth"),
        {
            "info": {"role": "user", "time": {"created": 100}},
            "parts": [
                {"type": "text", "text": "wayfinder runner resume funding-watch"}
            ],
        },
        {
            "info": {"role": "assistant", "time": {"created": 100}},
            "parts": [{"type": "text", "text": "runner job_result funding-watch"}],
        },
    ],
)
def test_ignores_discussion_listings_and_other_jobs(message: dict[str, Any]) -> None:
    client = _client_with(
        [_session("owner", 10), _session("other", 200)],
        {"owner": [_tool(5)], "other": [message]},
    )
    assert client.find_session_referencing_job("funding-watch") == "owner"


def test_ignores_job_name_in_tool_output() -> None:
    listing = _tool(100, command="wayfinder runner status")
    listing["parts"][0]["state"]["output"] = "funding-watch"
    client = _client_with([_session("listing", 200)], {"listing": [listing]})
    assert client.find_session_referencing_job("funding-watch") is None


def test_ranks_tool_start_not_result_posts_or_session_update() -> None:
    later_action = _tool(90)
    later_action["parts"][0]["state"]["time"]["start"] = 290
    result_post = {
        "info": {"role": "user", "time": {"created": 500}},
        "parts": [
            {"type": "text", "text": '{"type":"job_result","name":"funding-watch"}'}
        ],
    }
    client = _client_with(
        [_session("old", 500), _session("new", 100)],
        {"old": [_tool(190), result_post], "new": [later_action]},
    )
    assert client.find_session_referencing_job("funding-watch") == "new"


@pytest.mark.parametrize(
    "extra",
    [
        {"time": {"updated": 300, "archived": 1}},
        {"parentID": "parent"},
        {"title": "job/strategy/monitor"},
    ],
)
def test_skips_archived_child_and_worker_sessions(extra: dict[str, Any]) -> None:
    client = _client_with(
        [_session("owner", 10), _session("internal", 300, **extra)],
        {"owner": [_tool(5)], "internal": [_tool(290)]},
    )
    assert client.find_session_referencing_job("funding-watch") == "owner"


def test_discovery_budget_does_not_return_a_partial_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client_with(
        [_session("first", 10), _session("second", 20)], {"first": [_tool(5)]}
    )
    ticks = iter([0.0, 0.5, 2.1])
    monkeypatch.setattr(client_module.time, "monotonic", lambda: next(ticks))
    assert client.find_session_referencing_job("funding-watch") is None


@pytest.mark.parametrize("failure", ["http", "timeout", "json"])
def test_discovery_failure_is_a_miss(failure: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("offline", request=request)
        if failure == "json":
            return httpx.Response(200, text="unavailable")
        return httpx.Response(503)

    client = OpenCodeClient()
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(respond))
    assert client.find_session_referencing_job("funding-watch") is None


def test_is_live_session() -> None:
    client = _client_with(
        [_session("live", 1), _session("archived", 1, time={"archived": 5})], {}
    )
    assert client.is_live_session("live") is True
    assert client.is_live_session("archived") is False
    assert client.is_live_session("deleted") is False
