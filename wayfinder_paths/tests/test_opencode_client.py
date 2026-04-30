from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.OpenCodeClient import OpenCodeClient


class _StubClient:
    def __init__(
        self, *, sessions: list[dict[str, Any]], messages_by_id: dict[str, Any]
    ):
        self._sessions = sessions
        self._messages_by_id = messages_by_id

    def get(self, url: str, params: dict[str, Any] | None = None):  # noqa: ARG002
        class _Resp:
            def __init__(self, payload: Any):
                self._payload = payload

            def json(self) -> Any:
                return self._payload

        if url.endswith("/session"):
            return _Resp(self._sessions)
        for session_id, payload in self._messages_by_id.items():
            if url.endswith(f"/session/{session_id}/message"):
                return _Resp(payload)
        return _Resp([])


def _client_with(
    sessions: list[dict[str, Any]], messages: dict[str, Any]
) -> OpenCodeClient:
    c = OpenCodeClient()
    c.client = _StubClient(sessions=sessions, messages_by_id=messages)  # type: ignore[assignment]
    return c


def test_find_runner_session_matches_cli_verb() -> None:
    c = _client_with(
        sessions=[{"id": "ses_cli"}],
        messages={"ses_cli": [{"text": "I ran `runner add-job foo --interval 60`"}]},
    )
    assert c.find_runner_session() == "ses_cli"


def test_find_runner_session_matches_mcp_action() -> None:
    """MCP tool calls serialize as `wayfinder_runner` + action `add_job`
    (underscore). The CLI verb is `add-job` (hyphen). Both must bind."""
    c = _client_with(
        sessions=[{"id": "ses_mcp"}],
        messages={
            "ses_mcp": [
                {
                    "tool": "wayfinder_runner",
                    "input": {"action": "add_job", "name": "eth-price-check"},
                }
            ]
        },
    )
    assert c.find_runner_session() == "ses_mcp"


def test_find_runner_session_returns_none_when_no_match() -> None:
    c = _client_with(
        sessions=[{"id": "ses_other"}],
        messages={"ses_other": [{"text": "hello world"}]},
    )
    assert c.find_runner_session() is None
