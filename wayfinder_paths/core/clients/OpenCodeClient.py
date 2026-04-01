import httpx
from loguru import logger

OPENCODE_DEFAULT_URL = "http://localhost:4096"


class OpenCodeClient:
    def __init__(self, base_url: str = OPENCODE_DEFAULT_URL):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=10)

    def healthy(self) -> bool:
        try:
            r = self._client.get(f"{self.base_url}/global/health")
            return r.status_code == 200 and r.json().get("healthy", False)
        except Exception:
            return False

    def latest_session_id(self) -> str | None:
        try:
            r = self._client.get(f"{self.base_url}/session")
            if r.status_code != 200:
                return None
            sessions = r.json()
            if not sessions:
                return None
            return sessions[0].get("id")
        except Exception:
            return None

    def send_message(self, session_id: str, text: str) -> bool:
        try:
            r = self._client.post(
                f"{self.base_url}/session/{session_id}/message",
                json={"parts": [{"type": "text", "text": text}]},
            )
            return r.status_code == 200
        except Exception as exc:
            logger.debug(f"Failed to send message to session {session_id}: {exc}")
            return False
