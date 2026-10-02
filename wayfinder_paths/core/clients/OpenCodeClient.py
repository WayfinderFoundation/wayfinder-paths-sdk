from __future__ import annotations

import shlex
import time
from pathlib import PurePosixPath
from typing import Any

import httpx
from loguru import logger

from wayfinder_paths.runner.constants import ADD_JOB_CLI_VERB, RUNNER_SESSION_ACTIONS

OPENCODE_DEFAULT_URL = "http://localhost:3096"


def _references_runner_job(part: dict[str, Any], job_name: str) -> bool:
    if part.get("type") != "tool":
        return False
    inputs = (part.get("state") or {}).get("input") or {}
    if part.get("tool") in {"core_runner", "wayfinder_core_runner", "wayfinder_runner"}:
        return (
            inputs.get("action") in RUNNER_SESSION_ACTIONS
            and inputs.get("name") == job_name
        )
    if part.get("tool") != "bash":
        return False

    # Only inspect command inputs, never tool output or quoted chat prose.
    lexer = shlex.shlex(
        str(inputs.get("command") or ""), posix=True, punctuation_chars=";&|<>\n"
    )
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return False
    command: list[str] = []
    heredocs: list[str] = []
    for token in [*tokens, ";"]:
        if not token or not all(char in ";&|\n" for char in token):
            command.append(token)
            continue
        args, command = command, []
        if heredocs:
            if args == [heredocs[0]]:
                heredocs.pop(0)
            continue
        # Script bodies are data, even if they contain literal runner commands.
        heredocs = [
            args[index + 1].removeprefix("-")
            for index, arg in enumerate(args[:-1])
            if arg == "<<"
        ]
        if args[:2] == ["poetry", "run"]:
            args = args[2:]
        if args and PurePosixPath(args[0]).name == "wayfinder":
            args = args[1:]
        elif (
            len(args) >= 3
            and PurePosixPath(args[0]).name.startswith("python")
            and args[1:3] == ["-m", "wayfinder_paths.mcp.cli"]
        ):
            args = args[3:]
        else:
            continue
        if len(args) < 3 or args[0] != "runner":
            continue
        if args[1] in {"resume", "run-once"} and args[2:] == [job_name]:
            return True
        if args[1] in {ADD_JOB_CLI_VERB, "update-job"}:
            for index, arg in enumerate(args[2:], start=2):
                if arg == f"--name={job_name}" or (
                    arg == "--name" and args[index + 1 : index + 2] == [job_name]
                ):
                    return True
    return False


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

    def list_sessions(self, *, timeout: float = 10.0) -> list[dict[str, Any]]:
        try:
            response = self.client.get(f"{self.base_url}/session", timeout=timeout)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            return []

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        try:
            resp = self.client.get(f"{self.base_url}/session/{session_id}")
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError):
            return None

    def is_live_session(self, session_id: str) -> bool:
        session = self.get_session(session_id)
        return session is not None and not (session.get("time") or {}).get("archived")

    def find_session_referencing_job(self, job_name: str) -> str | None:
        """Find the latest exact job action in a human chat, within two seconds."""
        deadline = time.monotonic() + 2.0
        best_id: str | None = None
        best_at = -1
        try:
            for session in self.list_sessions(timeout=2.0):
                if (
                    (session.get("time") or {}).get("archived")
                    or session.get("parentID")
                    or str(session.get("title") or "").strip().startswith("job/")
                ):
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                response = self.client.get(
                    f"{self.base_url}/session/{session['id']}/message",
                    params={"limit": 50},
                    timeout=remaining,
                )
                response.raise_for_status()
                for message in response.json():
                    info = message.get("info") or {}
                    if info.get("role") != "assistant":
                        continue
                    for part in message.get("parts") or []:
                        if not _references_runner_job(part, job_name):
                            continue
                        created = ((part.get("state") or {}).get("time") or {}).get(
                            "start", (info.get("time") or {}).get("created", 0)
                        )
                        if created > best_at:
                            best_id, best_at = session["id"], created
        except (httpx.HTTPError, ValueError):
            return None
        if time.monotonic() > deadline:
            return None
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
