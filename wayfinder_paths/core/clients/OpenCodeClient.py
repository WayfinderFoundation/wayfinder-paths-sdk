from __future__ import annotations

import json
from typing import Any

import httpx
from loguru import logger

OPENCODE_DEFAULT_URL = "http://localhost:4096"


class OpenCodeClient:
    def __init__(self, base_url: str = OPENCODE_DEFAULT_URL):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            timeout=httpx.Timeout(10),
            headers={"Content-Type": "application/json"},
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response | None:
        url = f"{self.base_url}{path}"
        try:
            return self.client.request(method, url, **kwargs)
        except Exception as error:
            logger.debug(f"OpenCode {method} {url} failed: {error}")
            return None

    def healthy(self) -> bool:
        response = self._request("GET", "/global/health")
        if response is None or response.status_code != 200:
            return False
        return response.json().get("healthy", False)

    def list_sessions(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/session")
        if response is None or response.status_code != 200:
            return []
        return response.json()

    def active_session_id(self) -> str | None:
        """Find the session that invoked runner add-job."""
        for session in self.list_sessions():
            session_id = session.get("id")
            if session_id and self._session_has_runner_job(session_id):
                return session_id
        return None

    def _session_has_runner_job(self, session_id: str) -> bool:
        response = self._request(
            "GET", f"/session/{session_id}/message", params={"limit": 50}
        )
        if response is None or response.status_code != 200:
            return False
        raw_messages = json.dumps(response.json())
        return "runner" in raw_messages and (
            "add-job" in raw_messages or "add_job" in raw_messages
        )

    def send_message(self, session_id: str, text: str) -> bool:
        response = self._request(
            "POST",
            f"/session/{session_id}/message",
            json={"parts": [{"type": "text", "text": text}]},
        )
        if response is None or response.status_code != 200:
            return False
        return True


OPENCODE_CLIENT = OpenCodeClient()
