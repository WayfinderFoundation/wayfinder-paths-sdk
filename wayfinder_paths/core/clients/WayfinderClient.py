import asyncio
import time
from typing import Any

import httpx
from loguru import logger

from wayfinder_paths.core.config import get_api_key
from wayfinder_paths.core.constants.base import DEFAULT_HTTP_TIMEOUT


class WayfinderClient:
    # Cap for the single polite in-client 429 retry below. Subclasses with
    # their own retry stack (Gorlami) override this tighter and the base
    # retry honors it — a hardcoded base cap silently overrode theirs.
    MAX_RETRY_DELAY_S = 30.0

    def __init__(self):
        self.headers = {
            "Content-Type": "application/json",
        }
        self._ensure_api_key_header()
        self.client = self._build_client()
        self._client_loop: asyncio.AbstractEventLoop | None = None

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(DEFAULT_HTTP_TIMEOUT),
            follow_redirects=True,
            headers=self.headers,
        )

    def _ensure_client_for_running_loop(self) -> None:
        # Client singletons outlive event loops: every `asyncio.run()` seam
        # (counterfactual replay, derived features, scripts) creates a fresh
        # loop, while the httpx connection pool stays bound to the first loop
        # that used it. Reusing it then dies with "Event loop is closed"
        # (observed live: majors-5m-lab post-apply shadow dark for 5 days).
        # Rebuild the client whenever the running loop changed; the old
        # pool's sockets belong to a dead loop and are abandoned to GC.
        loop = asyncio.get_running_loop()
        if self._client_loop is None:
            self._client_loop = loop
        elif self._client_loop is not loop:
            self.client = self._build_client()
            self._client_loop = loop

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
        self._ensure_client_for_running_loop()

        # Pass API key to all endpoints (including public ones) for rate limiting
        self._ensure_api_key_header()
        if "X-API-KEY" in self.headers:
            self.client.headers["X-API-KEY"] = self.headers["X-API-KEY"]

        merged_headers = dict(self.headers)
        if headers:
            merged_headers.update(headers)
        resp = await self.client.request(method, url, headers=merged_headers, **kwargs)

        if resp.status_code == 429:
            # One polite retry honoring Retry-After (capped), then surface the
            # error. Instant hammer-retries are what turned a credit blip into
            # an hours-long 429 storm across every job on the box.
            cap = float(self.MAX_RETRY_DELAY_S)
            try:
                delay = min(float(resp.headers.get("Retry-After") or 5.0), cap)
            except ValueError:
                delay = min(5.0, cap)
            logger.warning(
                f"HTTP 429 for {method} {url} — retrying once in {delay:.0f}s"
            )
            await asyncio.sleep(delay)
            resp = await self.client.request(
                method, url, headers=merged_headers, **kwargs
            )

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
