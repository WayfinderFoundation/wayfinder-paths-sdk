from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from wayfinder_paths.core.utils import wallets
from wayfinder_paths.core.utils.wallets import (
    SESSION_EXPIRED_MESSAGE,
    SessionExpiredError,
    get_remote_sign_callback,
)
from wayfinder_paths.mcp.utils import catch_errors


def _http_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://wayfinder.ai/api/v1/wallets/0x/sign")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


@pytest.mark.asyncio
async def test_sign_callback_maps_404_to_session_expired():
    callback = get_remote_sign_callback("0xabc")
    with patch.object(
        wallets.WALLET_CLIENT,
        "sign_transaction",
        AsyncMock(side_effect=_http_error(404)),
    ):
        with pytest.raises(SessionExpiredError):
            await callback({"to": "0xdef"})


@pytest.mark.asyncio
async def test_sign_callback_reraises_other_http_errors():
    callback = get_remote_sign_callback("0xabc")
    with patch.object(
        wallets.WALLET_CLIENT,
        "sign_transaction",
        AsyncMock(side_effect=_http_error(500)),
    ):
        with pytest.raises(httpx.HTTPStatusError):
            await callback({"to": "0xdef"})


@pytest.mark.asyncio
async def test_catch_errors_surfaces_session_expired_code():
    @catch_errors
    async def tool() -> dict:
        raise SessionExpiredError(SESSION_EXPIRED_MESSAGE)

    result = await tool()
    assert result["ok"] is False
    assert result["error"]["code"] == "session_expired"
    assert "wayfinder.ai/app/shells" in result["error"]["message"]
