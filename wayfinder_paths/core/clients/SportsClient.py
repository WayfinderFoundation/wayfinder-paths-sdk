from __future__ import annotations

import os
from typing import Any

import httpx

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url

DEFAULT_SESSION_ID = "mcp"
SESSION_ENV_KEYS = (
    "WAYFINDER_SPORTS_SESSION_ID",
    "OPENCODE_SESSION_ID",
    "OPENCODE_SESSIONID",
    "OPENCODE_INSTANCE_ID",
)


class SportsGatewayAPIError(RuntimeError):
    """Structured error raised when the sports gateway returns a non-2xx body."""

    def __init__(
        self,
        *,
        status_code: int,
        error_type: str,
        code: str,
        message: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.status_code = status_code
        self.error_type = error_type
        self.code = code
        self.message = message
        self.details = details


class SportsClient(WayfinderClient):
    """Client for the backend-mediated, provider-agnostic Wayfinder Sports Gateway.

    The provider API key lives only in the backend; this client only ever talks to
    ``/api/v1/sports/*`` with the user's Wayfinder API key (``X-API-KEY``).
    """

    def _sports_url(self, path: str) -> str:
        base = get_api_base_url().rstrip("/")
        suffix = path.strip("/")
        return f"{base}/sports/{suffix}/"

    async def snapshot(
        self,
        *,
        action: str,
        sport: str,
        game_id: str | None = None,
        search: str | None = None,
        date: str | None = None,
        limit: int | None = None,
        session_id: str | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "action": str(action).strip(),
            "sport": str(sport).strip().lower(),
            "sessionID": self.resolve_session_id(session_id),
        }
        if game_id:
            payload["game_id"] = str(game_id).strip()
        if search:
            payload["search"] = str(search).strip()
        if date:
            payload["date"] = str(date).strip()
        if limit is not None:
            payload["limit"] = int(limit)
        return await self._post("snapshot", payload)

    async def backtest_state(
        self,
        *,
        action: str,
        run_id: str | None = None,
        limit: int | None = None,
        session_id: str | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "action": str(action).strip(),
            "sessionID": self.resolve_session_id(session_id),
        }
        if run_id:
            payload["run_id"] = str(run_id).strip()
        if limit is not None:
            payload["limit"] = int(limit)
        return await self._post("backtests/state", payload)

    async def provider_catalog(self, *, session_id: str | None = None) -> Any:
        return await self._post(
            "provider",
            {"action": "catalog", "sessionID": self.resolve_session_id(session_id)},
        )

    async def provider_call(
        self,
        *,
        endpoint_id: str,
        sport: str | None = None,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        body: Any = None,
        run_id: str | None = None,
        title: str | None = None,
        session_id: str | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "action": "call",
            "endpoint_id": str(endpoint_id).strip(),
            "sessionID": self.resolve_session_id(session_id),
        }
        if sport:
            payload["sport"] = str(sport).strip().lower()
        if path_params:
            payload["path_params"] = path_params
        if query:
            payload["query"] = query
        if body is not None:
            payload["body"] = body
        if run_id:
            payload["run_id"] = str(run_id).strip()
        if title:
            payload["title"] = str(title).strip()
        return await self._post("provider", payload)

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        try:
            response = await self._authed_request(
                "POST", self._sports_url(path), json=payload
            )
        except httpx.HTTPStatusError as exc:
            raise _gateway_error_from_response(exc.response) from exc
        except httpx.RequestError as exc:
            raise SportsGatewayAPIError(
                status_code=0,
                error_type="provider_failure",
                code="gateway_unavailable",
                message="Sports gateway request failed",
            ) from exc
        return response.json()

    @staticmethod
    def resolve_session_id(session_id: str | None = None) -> str:
        explicit = str(session_id or "").strip()
        if explicit and explicit != "_":
            return explicit[:200]
        for key in SESSION_ENV_KEYS:
            value = os.environ.get(key, "").strip()
            if value:
                return value[:200]
        return DEFAULT_SESSION_ID


def _gateway_error_from_response(response: httpx.Response) -> SportsGatewayAPIError:
    error: dict[str, Any] = {}
    try:
        body = response.json()
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            error = body["error"]
    except ValueError:
        error = {}
    return SportsGatewayAPIError(
        status_code=response.status_code,
        error_type=str(error.get("type") or "http_error"),
        code=str(error.get("code") or "http_error"),
        message=str(
            error.get("message") or response.reason_phrase or "Sports gateway error"
        ),
        details=error.get("details"),
    )


SPORTS_CLIENT = SportsClient()
