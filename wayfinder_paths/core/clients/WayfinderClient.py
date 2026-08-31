from __future__ import annotations

import time
from collections.abc import Awaitable
from typing import Any, Self
from weakref import WeakSet

import httpx
from loguru import logger

from wayfinder_paths.core.config import get_api_key
from wayfinder_paths.core.constants.base import DEFAULT_HTTP_TIMEOUT


class AsyncClientOwner:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        _CLIENT_OWNERS.add(self)

    def _create_client(self) -> httpx.AsyncClient:
        raise NotImplementedError

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed is True:
            self._client = self._create_client()
        return self._client

    @client.setter
    def client(self, value: httpx.AsyncClient) -> None:
        self._client = value

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None and client.is_closed is not True:
            await client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


_CLIENT_OWNERS: WeakSet[AsyncClientOwner] = WeakSet()


async def close_async_clients() -> None:
    for owner in list(_CLIENT_OWNERS):
        await owner.aclose()


async def with_async_client_cleanup[ResultT](
    awaitable: Awaitable[ResultT],
) -> ResultT:
    try:
        return await awaitable
    finally:
        await close_async_clients()


class WayfinderClient(AsyncClientOwner):
    def __init__(self) -> None:
        super().__init__()
        self.headers = {
            "Content-Type": "application/json",
        }
        self._ensure_api_key_header()

    def _create_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(DEFAULT_HTTP_TIMEOUT),
            follow_redirects=True,
            headers=self.headers,
        )

    def _ensure_api_key_header(self) -> None:
        if self.headers.get("X-API-KEY"):
            return
        api_key = get_api_key()
        if api_key:
            self.headers["X-API-KEY"] = api_key

    async def _authed_request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        logger.debug(f"Making {method} request to {url}")
        start_time = time.time()

        # Pass API key to all endpoints (including public ones) for rate limiting
        self._ensure_api_key_header()
        if "X-API-KEY" in self.headers:
            self.client.headers["X-API-KEY"] = self.headers["X-API-KEY"]

        merged_headers = dict(self.headers)
        if headers:
            merged_headers.update(headers)
        resp = await self.client.request(method, url, headers=merged_headers, **kwargs)

        elapsed = time.time() - start_time
        if resp.status_code >= 400:
            logger.warning(
                f"HTTP {resp.status_code} response for {method} {url} after {elapsed:.2f}s"
            )
        else:
            logger.debug(
                f"HTTP {resp.status_code} response for {method} {url} after {elapsed:.2f}s"
            )

        resp.raise_for_status()
        return resp
