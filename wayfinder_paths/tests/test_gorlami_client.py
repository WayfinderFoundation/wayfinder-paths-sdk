from unittest.mock import AsyncMock

import httpx
import pytest

from wayfinder_paths.core.clients.GorlamiTestnetClient import GorlamiTestnetClient
from wayfinder_paths.core.utils import retry as retry_utils


@pytest.mark.asyncio
async def test_gorlami_retry_after_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    client = GorlamiTestnetClient()
    request = httpx.Request(
        "POST", "https://strategies.wayfinder.ai/api/v1/blockchain/gorlami/fork"
    )
    rate_limited = httpx.Response(
        429,
        request=request,
        headers={"Retry-After": "1830780"},
        json={"detail": "rate limited"},
    )
    success = httpx.Response(200, request=request, json={"fork_id": "fork-123"})

    sleep_calls: list[float] = []

    async def fake_sleep(delay_s: float) -> None:
        sleep_calls.append(delay_s)

    monkeypatch.setattr(retry_utils.asyncio, "sleep", fake_sleep)
    client.client.request = AsyncMock(side_effect=[rate_limited, success])

    try:
        response = await client._request("POST", str(request.url))
    finally:
        await client.close()

    assert response.status_code == 200
    assert sleep_calls == [client.MAX_RETRY_DELAY_S]
