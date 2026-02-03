import time
from typing import Any

import httpx
from loguru import logger

from wayfinder_paths.core.config import get_api_key
from wayfinder_paths.core.constants.base import DEFAULT_HTTP_TIMEOUT


class WayfinderClient:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(DEFAULT_HTTP_TIMEOUT))
        self.headers = {
            "Content-Type": "application/json",
        }

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
        if not self.headers.get("X-API-KEY"):
            self.headers["X-API-KEY"] = get_api_key()

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
