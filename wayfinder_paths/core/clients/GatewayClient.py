from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import httpx

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url


class GatewayAPIError(RuntimeError):
    """Structured error raised when a backend gateway returns a non-2xx body."""

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


class GatewayClient(WayfinderClient):
    gateway_path: str
    gateway_name = "Gateway"
    gateway_error_class: type[GatewayAPIError] = GatewayAPIError
    session_env_keys: tuple[str, ...] = ()
    default_session_id = "mcp"
    truncate_explicit_session_id = False
    include_response_text_in_error = False

    def _gateway_url(self, path: str) -> str:
        base = get_api_base_url().rstrip("/")
        suffix = path.strip("/")
        return f"{base}/{self.gateway_path}/{suffix}/"

    async def _post_gateway(self, path: str, payload: Mapping[str, Any]) -> Any:
        try:
            response = await self._authed_request(
                "POST", self._gateway_url(path), json=payload
            )
        except httpx.HTTPStatusError as exc:
            raise self._gateway_error_from_response(exc.response) from exc
        except httpx.RequestError as exc:
            raise self.gateway_error_class(
                status_code=0,
                error_type="provider_failure",
                code="gateway_unavailable",
                message=f"{self.gateway_name} gateway request failed",
            ) from exc
        return response.json()

    @classmethod
    def resolve_session_id(cls, session_id: str | None = None) -> str:
        explicit = str(session_id or "").strip()
        if explicit and explicit != "_":
            if cls.truncate_explicit_session_id:
                return explicit[:200]
            if len(explicit) > 200:
                raise ValueError("session_id must be 200 characters or fewer")
            return explicit

        for key in cls.session_env_keys:
            value = os.environ.get(key, "").strip()
            if value:
                return value[:200]
        return cls.default_session_id

    def _gateway_error_from_response(self, response: httpx.Response) -> GatewayAPIError:
        return gateway_error_from_response(
            response,
            error_class=self.gateway_error_class,
            gateway_name=self.gateway_name,
            include_response_text=self.include_response_text_in_error,
        )

    def _extract_gateway_error(self, response: httpx.Response) -> dict[str, Any]:
        return extract_gateway_error(
            response,
            gateway_name=self.gateway_name,
            include_response_text=self.include_response_text_in_error,
        )


def gateway_error_from_response(
    response: httpx.Response,
    *,
    error_class: type[GatewayAPIError],
    gateway_name: str,
    include_response_text: bool = False,
) -> GatewayAPIError:
    error = extract_gateway_error(
        response,
        gateway_name=gateway_name,
        include_response_text=include_response_text,
    )
    return error_class(
        status_code=response.status_code,
        error_type=str(error.get("type") or "http_error"),
        code=str(error.get("code") or "http_error"),
        message=str(
            error.get("message")
            or response.reason_phrase
            or f"{gateway_name} gateway error"
        ),
        details=error.get("details"),
    )


def extract_gateway_error(
    response: httpx.Response,
    *,
    gateway_name: str,
    include_response_text: bool = False,
) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        message = response.reason_phrase or f"{gateway_name} gateway error"
        if include_response_text:
            message = response.text[:200] or message
        return {"type": "http_error", "code": "http_error", "message": message}

    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return {
                "type": str(error.get("type") or "http_error"),
                "code": str(error.get("code") or "http_error"),
                "message": str(
                    error.get("message")
                    or response.reason_phrase
                    or f"{gateway_name} gateway error"
                ),
                "details": error.get("details"),
            }
    return {
        "type": "http_error",
        "code": "http_error",
        "message": response.reason_phrase or f"{gateway_name} gateway error",
    }
