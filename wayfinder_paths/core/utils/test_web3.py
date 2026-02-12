from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.core.utils.web3 import (
    _clear_rate_limit_cooldowns,
    _FailoverRpcProvider,
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
