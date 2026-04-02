from __future__ import annotations

import json
from typing import Any

import httpx
from loguru import logger

from wayfinder_paths.runner.constants import ADD_JOB_CLI_VERB, ADD_JOB_MCP_ACTION

OPENCODE_DEFAULT_URL = "http://localhost:4096"


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

    def find_runner_session(self) -> str | None:
        """Find the session that invoked runner add-job."""
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


OPENCODE_CLIENT = OpenCodeClient()
