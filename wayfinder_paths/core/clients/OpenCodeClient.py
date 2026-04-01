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

    def active_session_id(self) -> str | None:
        """Find the session that invoked runner add-job."""
        for s in self.list_sessions():
            sid = s.get("id")
            if sid and self._session_has_runner_job(sid):
                return sid
        return None

    def _session_has_runner_job(self, session_id: str) -> bool:
        resp = self._request(
            "GET", f"/session/{session_id}/message", params={"limit": 50}
        )
        if resp is None or resp.status_code != 200:
            return False
        raw = json.dumps(resp.json())
        return "runner" in raw and ("add-job" in raw or "add_job" in raw)

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
