from __future__ import annotations

import json
from typing import Any

import httpx
from loguru import logger

from wayfinder_paths.runner.constants import ADD_JOB_CLI_VERB, ADD_JOB_MCP_ACTION

OPENCODE_DEFAULT_URL = "http://localhost:3096"


class OpenCodeClient:
    def __init__(self, base_url: str = OPENCODE_DEFAULT_URL):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            timeout=httpx.Timeout(10),
            headers={"Content-Type": "application/json"},
        )

    def healthy(self) -> bool:
        try:
            return (
                self.client.get(f"{self.base_url}/global/health")
                .json()
                .get("healthy", False)
            )
        except Exception:
            return False

    def list_sessions(self) -> list[dict[str, Any]]:
        try:
            return self.client.get(f"{self.base_url}/session").json()
        except Exception:
            return []

    def create_session(
        self,
        *,
        parent_id: str | None = None,
        title: str | None = None,
        agent: str | None = None,
    ) -> str | None:
        payload: dict[str, Any] = {}
        if parent_id:
            payload["parentID"] = parent_id
        if title:
            payload["title"] = title
        if agent:
            payload["agent"] = agent
        try:
            response = self.client.post(f"{self.base_url}/session", json=payload)
            if not response.is_success:
                return None
            data = response.json()
            if isinstance(data, dict):
                return data.get("id")
        except Exception as error:
            logger.debug(f"Failed to create OpenCode session: {error}")
        return None

    def find_child_session(
        self, *, parent_id: str | None, title: str | None
    ) -> str | None:
        if not parent_id:
            return None
        try:
            children = self.client.get(
                f"{self.base_url}/session/{parent_id}/children"
            ).json()
        except Exception:
            return None
        if not isinstance(children, list):
            return None
        for session in children:
            if not isinstance(session, dict):
                continue
            if title and session.get("title") != title:
                continue
            return session.get("id")
        return None

    def find_runner_session(self) -> str | None:
        """Find the session that invoked add-job, via either surface.

        The breadcrumb in the chat message blob is surface-specific:
        Bash + CLI leaves the literal `add-job` (hyphen); the wayfinder_runner
        MCP tool leaves `add_job` (underscore). Match either.
        """
        for session in self.list_sessions():
            session_id = session["id"]
            try:
                raw = json.dumps(
                    self.client.get(
                        f"{self.base_url}/session/{session_id}/message",
                        params={"limit": 50},
                    ).json()
                )
                if "runner" in raw and (
                    ADD_JOB_CLI_VERB in raw or ADD_JOB_MCP_ACTION in raw
                ):
                    return session_id
            except Exception:
                continue
        return None

    def send_message(self, session_id: str, text: str) -> bool:
        try:
            return self.client.post(
                f"{self.base_url}/session/{session_id}/message",
                json={"parts": [{"type": "text", "text": text}]},
            ).is_success
        except Exception as error:
            logger.debug(f"Failed to send message to session {session_id}: {error}")
            return False

    def prompt_async(
        self, session_id: str, text: str, *, agent: str | None = None
    ) -> bool:
        payload: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        if agent:
            payload["agent"] = agent
        try:
            return self.client.post(
                f"{self.base_url}/session/{session_id}/prompt_async",
                json=payload,
            ).is_success
        except Exception as error:
            logger.debug(f"Failed to queue async prompt for session {session_id}: {error}")
            return False


OPENCODE_CLIENT = OpenCodeClient()
