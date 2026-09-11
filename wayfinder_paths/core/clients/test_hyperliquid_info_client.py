from __future__ import annotations

import importlib
import threading
from unittest.mock import Mock

import pytest
from hyperliquid.utils.error import (  # type: ignore[import-untyped]
    ClientError,
    ServerError,
)
from requests import ConnectionError as RequestsConnectionError
from requests import Timeout as RequestsTimeout

from wayfinder_paths.core.clients.HyperliquidInfoClient import (
    HyperliquidInfoClient,
)
from wayfinder_paths.core.constants.base import DEFAULT_HTTP_TIMEOUT
from wayfinder_paths.core.utils import retry as retry_utils

client_module = importlib.import_module(
    "wayfinder_paths.core.clients.HyperliquidInfoClient"
)


@pytest.fixture
def no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleep_calls: list[float] = []

    async def fake_sleep(delay_s: float) -> None:
        sleep_calls.append(delay_s)

    monkeypatch.setattr(retry_utils.asyncio, "sleep", fake_sleep)
    return sleep_calls


@pytest.fixture
def mock_info(monkeypatch: pytest.MonkeyPatch) -> Mock:
    info = Mock()
    monkeypatch.setattr(client_module, "_public_info", lambda: info)
    return info


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transient_error",
    [
        ServerError(500, "null"),
        ClientError(429, None, "rate limited", {}),
        RequestsConnectionError("connection reset"),
        RequestsTimeout("read timed out"),
    ],
)
async def test_post_retries_transient_failures(
    mock_info: Mock,
    no_retry_sleep: list[float],
    transient_error: Exception,
) -> None:
    body = {"type": "candleSnapshot", "req": {"coin": "SOL"}}
    expected = [{"t": 1, "c": "100"}]
    mock_info.post.side_effect = [transient_error, expected]

    result = await HyperliquidInfoClient().post(body)

    assert result == expected
    assert mock_info.post.call_count == 2
    mock_info.post.assert_called_with("/info", body)
    assert no_retry_sleep == [0.25]


@pytest.mark.asyncio
async def test_post_does_not_retry_non_transient_client_error(
    mock_info: Mock,
    no_retry_sleep: list[float],
) -> None:
    error = ClientError(400, None, "bad request", {})
    mock_info.post.side_effect = error

    with pytest.raises(ClientError) as raised:
        await HyperliquidInfoClient().post({"type": "invalid"})

    assert raised.value is error
    assert mock_info.post.call_count == 1
    assert no_retry_sleep == []


@pytest.mark.asyncio
async def test_post_reraises_after_bounded_attempts(
    mock_info: Mock,
    no_retry_sleep: list[float],
) -> None:
    error = ServerError(503, "unavailable")
    mock_info.post.side_effect = error

    with pytest.raises(ServerError) as raised:
        await HyperliquidInfoClient().post({"type": "allMids"})

    assert raised.value is error
    assert mock_info.post.call_count == 3
    assert no_retry_sleep == [0.25, 0.5]


@pytest.mark.asyncio
async def test_client_reuses_transport_without_metadata_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_thread = threading.get_ident()
    threads: list[int] = []
    expected = {"ETH": "2000"}
    response = Mock(status_code=200)
    response.json.return_value = expected

    def post(*args: object, **kwargs: object) -> Mock:
        threads.append(threading.get_ident())
        return response

    transport = Mock()
    transport.post.side_effect = post
    factory = Mock(return_value=transport)
    monkeypatch.setattr("requests.Session", factory)
    client_module._public_info.cache_clear()
    try:
        client = HyperliquidInfoClient()
        for _ in range(2):
            assert await client.post({"type": "allMids"}) == expected
        assert len(threads) == 2
        assert all(thread != loop_thread for thread in threads)
        factory.assert_called_once_with()
        assert transport.post.call_count == 2
        transport.post.assert_called_with(
            f"{client_module.constants.MAINNET_API_URL}/info",
            json={"type": "allMids"},
            timeout=DEFAULT_HTTP_TIMEOUT,
        )
    finally:
        client_module._public_info.cache_clear()
