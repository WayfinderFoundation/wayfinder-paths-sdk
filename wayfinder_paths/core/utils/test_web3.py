import copy
from unittest.mock import AsyncMock

import pytest

import wayfinder_paths.core.config as config
from wayfinder_paths.core.utils.web3 import (
    _clear_rate_limit_cooldowns,
    _FailoverRpcProvider,
    _get_web3,
)


class _RateLimitedError(Exception):
    def __init__(self):
        super().__init__("Too Many Requests")
        self.status = 429


@pytest.fixture(autouse=True)
def clear_cooldowns():
    _clear_rate_limit_cooldowns()
    yield
    _clear_rate_limit_cooldowns()


@pytest.fixture
def restore_global_config() -> None:
    original = copy.deepcopy(config.CONFIG)
    yield
    config.set_config(original)


@pytest.mark.asyncio
async def test_failover_provider_uses_backend_on_http_429():
    provider = _FailoverRpcProvider("https://primary-rpc.invalid", chain_id=8453)
    provider._make_request = AsyncMock(side_effect=_RateLimitedError())
    provider.failover_provider._make_request = AsyncMock(
        return_value=b'{"jsonrpc":"2.0","id":1,"result":"0x1"}'
    )

    resp = await provider.make_request("eth_blockNumber", [])

    assert resp["result"] == "0x1"
    assert provider.failover_provider._make_request.await_count == 1


@pytest.mark.asyncio
async def test_failover_provider_uses_backend_on_rpc_rate_limit_error_code():
    provider = _FailoverRpcProvider("https://primary-rpc.invalid", chain_id=8453)
    provider._make_request = AsyncMock(
        return_value=(
            b'{"jsonrpc":"2.0","id":1,"error":{"code":-32005,"message":"Limit exceeded","data":{"backoff_seconds":120}}}'
        )
    )
    provider.failover_provider._make_request = AsyncMock(
        return_value=b'{"jsonrpc":"2.0","id":1,"result":"0x2"}'
    )

    resp = await provider.make_request("eth_blockNumber", [])

    assert resp["result"] == "0x2"
    assert provider.failover_provider._make_request.await_count == 1


@pytest.mark.asyncio
async def test_failover_provider_does_not_failover_on_non_rate_limited_error():
    provider = _FailoverRpcProvider("https://primary-rpc.invalid", chain_id=8453)
    provider._make_request = AsyncMock(
        return_value=(
            b'{"jsonrpc":"2.0","id":1,"error":{"code":3,"message":"execution reverted"}}'
        )
    )
    provider.failover_provider._make_request = AsyncMock(
        return_value=b'{"jsonrpc":"2.0","id":1,"result":"0x1"}'
    )

    resp = await provider.make_request("eth_call", [])

    assert resp["error"]["code"] == 3
    assert provider.failover_provider._make_request.await_count == 0


@pytest.mark.asyncio
async def test_failover_provider_uses_cooldown_after_rate_limit():
    provider = _FailoverRpcProvider("https://primary-rpc.invalid", chain_id=8453)
    provider._make_request = AsyncMock(
        side_effect=[
            _RateLimitedError(),
            b'{"jsonrpc":"2.0","id":1,"result":"0xSHOULD_NOT_USE_PRIMARY"}',
        ]
    )
    provider.failover_provider._make_request = AsyncMock(
        return_value=b'{"jsonrpc":"2.0","id":1,"result":"0x3"}'
    )

    first = await provider.make_request("eth_blockNumber", [])
    second = await provider.make_request("eth_blockNumber", [])

    assert first["result"] == "0x3"
    assert second["result"] == "0x3"
    # First call hit primary then failover; second call should skip primary due to cooldown.
    assert provider._make_request.await_count == 1
    assert provider.failover_provider._make_request.await_count == 2


@pytest.mark.asyncio
async def test_failover_provider_disconnect_closes_failover_provider():
    provider = _FailoverRpcProvider("https://primary-rpc.invalid", chain_id=8453)
    provider.failover_provider.disconnect = AsyncMock()

    await provider.disconnect()

    assert provider.failover_provider.disconnect.await_count == 1


def test_primary_rpc_headers_do_not_include_wayfinder_api_key(
    restore_global_config: None,
):
    config.set_config(
        {
            "system": {"api_key": "wk_should_not_leak"},
            "strategy": {"rpc_urls": {"8453": ["https://primary-rpc.invalid"]}},
        }
    )
    w3 = _get_web3("https://primary-rpc.invalid", 8453)
    provider = w3.provider

    assert "X-API-KEY" not in provider._request_kwargs["headers"]
    assert (
        provider.failover_provider._request_kwargs["headers"]["X-API-KEY"]
        == "wk_should_not_leak"
    )
