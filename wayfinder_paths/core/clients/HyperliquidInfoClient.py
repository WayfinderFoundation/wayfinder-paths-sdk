from __future__ import annotations

import asyncio
from functools import cache
from typing import Any

from hyperliquid.api import API
from hyperliquid.utils import constants
from hyperliquid.utils.error import (  # type: ignore[import-untyped]
    ClientError,
    ServerError,
)
from requests import ConnectionError as RequestsConnectionError
from requests import Timeout as RequestsTimeout

from wayfinder_paths.core.constants.base import DEFAULT_HTTP_TIMEOUT
from wayfinder_paths.core.utils.retry import retry_async

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


@cache
def _public_info() -> API:
    # Raw /info posts do not need Info's eager market-metadata requests.
    return API(constants.MAINNET_API_URL, timeout=DEFAULT_HTTP_TIMEOUT)


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (ClientError, ServerError)):
        return getattr(exc, "status_code", None) in _RETRYABLE_STATUS_CODES
    return isinstance(exc, (RequestsConnectionError, RequestsTimeout))


class HyperliquidInfoClient:
    async def post(self, body: dict[str, Any]) -> Any:
        client = _public_info()
        return await retry_async(
            lambda: asyncio.to_thread(client.post, "/info", body),
            should_retry=_is_retryable,
        )


HYPERLIQUID_INFO_CLIENT = HyperliquidInfoClient()
