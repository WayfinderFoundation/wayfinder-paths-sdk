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
from requests import ConnectionError as RequestsConnectionError

from wayfinder_paths.core.utils.retry import retry_async

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


@cache
def _public_info() -> Info:
    return Info(constants.MAINNET_API_URL, skip_ws=True)


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (ClientError, ServerError)):
        return getattr(exc, "status_code", None) in _RETRYABLE_STATUS_CODES
    return isinstance(exc, RequestsConnectionError)


class HyperliquidInfoClient:
    async def post(self, body: dict[str, Any]) -> Any:
        return await retry_async(
            lambda: asyncio.to_thread(_public_info().post, "/info", body),
            should_retry=_is_retryable,
        )


HYPERLIQUID_INFO_CLIENT = HyperliquidInfoClient()
