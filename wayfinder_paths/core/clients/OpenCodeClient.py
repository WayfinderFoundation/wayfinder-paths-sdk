from __future__ import annotations

import json
from typing import Any

import httpx
from loguru import logger

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

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        try:
            resp = self.client.get(f"{self.base_url}/session/{session_id}")
        except Exception:
            return None
        if not resp.is_success:
            return None
        return resp.json()

    def is_live_session(self, session_id: str) -> bool:
        session = self.get_session(session_id)
        return session is not None and not (session.get("time") or {}).get("archived")

    def find_session_referencing_job(self, job_name: str) -> str | None:
        """Live session with the most recent message that mentions the job.

        Ranked by the mentioning message, not the session's `updated` time: the
        runner's own job_result posts bump the bound session, which would
        otherwise keep winning after the user moves the job to another chat.
        """
        sessions = [
            s
            for s in self.list_sessions()
            if not (s.get("time") or {}).get("archived") and not s.get("parentID")
        ]
        sessions.sort(
            key=lambda s: (s.get("time") or {}).get("updated", 0), reverse=True
        )
        best_id: str | None = None
        best_at = -1
        for session in sessions:
            # A session can't hold a message newer than its own update time.
            if (session.get("time") or {}).get("updated", 0) <= best_at:
                break
            try:
                messages = self.client.get(
                    f"{self.base_url}/session/{session['id']}/message",
                    params={"limit": 50},
                ).json()
            except Exception:
                continue
            for message in messages:
                raw = json.dumps(message)
                if "job_result" in raw or "runner" not in raw or job_name not in raw:
                    continue
                created = ((message.get("info") or {}).get("time") or {}).get(
                    "created", 0
                )
                if created > best_at:
                    best_id, best_at = session["id"], created
        return best_id

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
