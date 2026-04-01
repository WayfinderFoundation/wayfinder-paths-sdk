from __future__ import annotations

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
            resp = self.client.request(method, url, **kwargs)
            return resp
        except Exception as exc:
            logger.debug(f"OpenCode {method} {url} failed: {exc}")
            return None

    def healthy(self) -> bool:
        resp = self._request("GET", "/global/health")
        if resp is None or resp.status_code != 200:
            return False
        return resp.json().get("healthy", False)

    def list_sessions(self) -> list[dict[str, Any]]:
        resp = self._request("GET", "/session")
        if resp is None or resp.status_code != 200:
            return []
        return resp.json()

    def latest_session_id(self) -> str | None:
        sessions = self.list_sessions()
        if not sessions:
            return None
        return sessions[0].get("id")

    def send_message(self, session_id: str, text: str) -> bool:
        resp = self._request(
            "POST",
            f"/session/{session_id}/message",
            json={"parts": [{"type": "text", "text": text}]},
        )
        if resp is None or resp.status_code != 200:
            return False
        return True


OPENCODE_CLIENT = OpenCodeClient()
