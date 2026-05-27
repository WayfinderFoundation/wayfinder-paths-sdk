from __future__ import annotations

import asyncio
from functools import cache
from typing import Any

from hyperliquid.info import Info
from hyperliquid.utils import constants


@cache
def _public_info() -> Info:
    return Info(constants.MAINNET_API_URL, skip_ws=True)


class HyperliquidInfoClient:
    async def post(self, body: dict[str, Any]) -> Any:
        return await asyncio.to_thread(_public_info().post, "/info", body)


HYPERLIQUID_INFO_CLIENT = HyperliquidInfoClient()
