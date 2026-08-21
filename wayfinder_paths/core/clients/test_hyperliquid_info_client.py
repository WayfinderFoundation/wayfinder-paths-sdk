from __future__ import annotations

import importlib
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transient_error",
    [
        ServerError(500, "null"),
        ClientError(429, None, "rate limited", {}),
        RequestsConnectionError("connection reset"),
        RequestsTimeout("request timed out"),
    ],
)
async def test_post_retries_transient_failures(
    monkeypatch: pytest.MonkeyPatch,
    no_retry_sleep: list[float],
    transient_error: Exception,
) -> None:
    body = {"type": "candleSnapshot", "req": {"coin": "SOL"}}
    expected = [{"t": 1, "c": "100"}]
    info = Mock()
    info.post.side_effect = [transient_error, expected]
    monkeypatch.setattr(client_module, "_public_info", lambda: info)

    result = await HyperliquidInfoClient().post(body)

    assert result == expected
    assert info.post.call_count == 2
    info.post.assert_called_with("/info", body)
    assert no_retry_sleep == [0.25]


@pytest.mark.asyncio
async def test_post_does_not_retry_non_transient_client_error(
    monkeypatch: pytest.MonkeyPatch,
    no_retry_sleep: list[float],
) -> None:
    error = ClientError(400, None, "bad request", {})
    info = Mock()
    info.post.side_effect = error
    monkeypatch.setattr(client_module, "_public_info", lambda: info)

    with pytest.raises(ClientError) as raised:
        await HyperliquidInfoClient().post({"type": "invalid"})

    assert raised.value is error
    assert info.post.call_count == 1
    assert no_retry_sleep == []


@pytest.mark.asyncio
async def test_post_reraises_after_bounded_attempts(
    monkeypatch: pytest.MonkeyPatch,
    no_retry_sleep: list[float],
) -> None:
    error = ServerError(503, "unavailable")
    info = Mock()
    info.post.side_effect = error
    monkeypatch.setattr(client_module, "_public_info", lambda: info)

    with pytest.raises(ServerError) as raised:
        await HyperliquidInfoClient().post({"type": "allMids"})

    assert raised.value is error
    assert info.post.call_count == 3
    assert no_retry_sleep == [0.25, 0.5]
