from __future__ import annotations

import json
from typing import Any

import pytest

from wayfinder_paths.core.clients.OpenCodeClient import OpenCodeClient


class _StubClient:
    def __init__(
        self, *, sessions: list[dict[str, Any]], messages_by_id: dict[str, Any]
    ):
        self._sessions = sessions
        self._messages_by_id = messages_by_id

        self.message_fetches: list[str] = []

    def get(self, url: str, params: dict[str, Any] | None = None):  # noqa: ARG002
        class _Resp:
            def __init__(self, payload: Any, status: int = 200):
                self._payload = payload
                self.is_success = status < 400

            def json(self) -> Any:
                return self._payload

        if url.endswith("/session"):
            return _Resp(self._sessions)
        for session in self._sessions:
            session_id = session["id"]
            if url.endswith(f"/session/{session_id}/message"):
                self.message_fetches.append(session_id)
                return _Resp(self._messages_by_id.get(session_id, []))
            if url.endswith(f"/session/{session_id}"):
                return _Resp(session)
        return _Resp({"name": "NotFoundError"}, status=404)


def _client_with(
    sessions: list[dict[str, Any]], messages: dict[str, Any]
) -> OpenCodeClient:
    c = OpenCodeClient()
    c.client = _StubClient(sessions=sessions, messages_by_id=messages)  # type: ignore[assignment]
    return c


def _session(session_id: str, updated: int, **extra: Any) -> dict[str, Any]:
    return {"id": session_id, "time": {"created": 0, "updated": updated}, **extra}


def _msg(created: int, text: str) -> dict[str, Any]:
    return {
        "info": {"role": "assistant", "time": {"created": created}},
        "parts": [{"type": "text", "text": text}],
    }


def _job_result(created: int) -> dict[str, Any]:
    text = json.dumps({"type": "job_result", "name": "funding-watch"})
    return {
        "info": {"role": "user", "time": {"created": created}},
        "parts": [{"type": "text", "text": text}],
    }


_CLI_REF = "ran `wayfinder runner add-job --name funding-watch`"
_MCP_REF = json.dumps(
    {
        "tool": "wayfinder_runner",
        "input": {"action": "add_job", "name": "funding-watch"},
    }
)


@pytest.mark.parametrize("text", [_CLI_REF, _MCP_REF], ids=["cli", "mcp"])
def test_find_session_referencing_job_matches_either_surface(text: str) -> None:
    c = _client_with(
        sessions=[_session("ses_a", 10)], messages={"ses_a": [_msg(5, text)]}
    )
    assert c.find_session_referencing_job("funding-watch") == "ses_a"


def test_find_session_referencing_job_prefers_latest_mention_and_stops_early() -> None:
    c = _client_with(
        sessions=[
            _session("ses_old", 100),
            _session("ses_new", 300),
            _session("ses_mid", 200),
        ],
        messages={
            "ses_old": [_msg(90, _CLI_REF)],
            "ses_new": [_msg(290, _CLI_REF)],
            "ses_mid": [_msg(190, _CLI_REF)],
        },
    )
    assert c.find_session_referencing_job("funding-watch") == "ses_new"
    # ses_mid was updated before the mention in ses_new, so it can't beat it.
    assert c.client.message_fetches == ["ses_new"]  # type: ignore[attr-defined]


def test_find_session_referencing_job_ignores_runner_result_posts() -> None:
    # Job results keep bumping the old chat, but the user last mentioned the
    # job in the new chat.
    c = _client_with(
        sessions=[_session("ses_bound", 500), _session("ses_new", 300)],
        messages={
            "ses_bound": [_msg(100, _CLI_REF), _job_result(400), _job_result(500)],
            "ses_new": [_msg(290, "runner run-once funding-watch")],
        },
    )
    assert c.find_session_referencing_job("funding-watch") == "ses_new"


def test_find_session_referencing_job_skips_archived_and_child_sessions() -> None:
    c = _client_with(
        sessions=[
            _session("ses_archived", 300, time={"updated": 300, "archived": 1}),
            _session("ses_child", 250, parentID="ses_parent"),
            _session("ses_live", 100),
        ],
        messages={
            "ses_archived": [_msg(300, _CLI_REF)],
            "ses_child": [_msg(250, _CLI_REF)],
            "ses_live": [_msg(90, _CLI_REF)],
        },
    )
    assert c.find_session_referencing_job("funding-watch") == "ses_live"


def test_find_session_referencing_job_ignores_other_jobs() -> None:
    c = _client_with(
        sessions=[_session("ses_a", 10)],
        messages={"ses_a": [_msg(5, "runner add-job --name eth-price-check")]},
    )
    assert c.find_session_referencing_job("funding-watch") is None


def test_is_live_session() -> None:
    c = _client_with(
        sessions=[
            _session("ses_live", 1),
            _session("ses_archived", 1, time={"updated": 1, "archived": 5}),
        ],
        messages={},
    )
    assert c.is_live_session("ses_live") is True
    assert c.is_live_session("ses_archived") is False
    assert c.is_live_session("ses_deleted") is False
