from __future__ import annotations

import asyncio
from functools import cache
from typing import Any

from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.error import (  # type: ignore[import-untyped]
    ClientError,
    ServerError,
)
from loguru import logger
from requests import ConnectionError as RequestsConnectionError
from requests import Timeout as RequestsTimeout

from wayfinder_paths.core.utils.retry import retry_async

_MAX_ATTEMPTS = 3
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


@cache
def _public_info() -> Info:
    return Info(constants.MAINNET_API_URL, skip_ws=True)


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (ClientError, ServerError)):
        return getattr(exc, "status_code", None) in _RETRYABLE_STATUS_CODES
    return isinstance(exc, (RequestsConnectionError, RequestsTimeout))


class HyperliquidInfoClient:
    async def post(self, body: dict[str, Any]) -> Any:
        async def _attempt() -> Any:
            # Resolve the cached SDK client in the worker as its constructor performs
            # synchronous metadata requests on first use.
            return await asyncio.to_thread(lambda: _public_info().post("/info", body))

        def _on_retry(attempt: int, exc: Exception, delay_s: float) -> None:
            logger.warning(
                "Hyperliquid info request '{}' failed with {}; retrying in {:.2f}s "
                "(attempt {}/{})",
                body.get("type", "unknown"),
                type(exc).__name__,
                delay_s,
                attempt + 1,
                _MAX_ATTEMPTS,
            )

        return await retry_async(
            _attempt,
            max_retries=_MAX_ATTEMPTS,
            base_delay_s=0.25,
            should_retry=_is_retryable,
            on_retry=_on_retry,
        )


HYPERLIQUID_INFO_CLIENT = HyperliquidInfoClient()
