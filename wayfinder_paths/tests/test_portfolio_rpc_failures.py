from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from web3 import Web3
from web3.exceptions import ContractLogicError, Web3RPCError

import wayfinder_paths.adapters.moonwell_adapter.adapter as moonwell_module
import wayfinder_paths.adapters.pendle_adapter.adapter as pendle_module
from wayfinder_paths.adapters.moonwell_adapter.adapter import MoonwellAdapter
from wayfinder_paths.adapters.multicall_adapter.adapter import (
    MulticallAdapter,
    MulticallResult,
)
from wayfinder_paths.adapters.pendle_adapter.adapter import PendleAdapter

ACCOUNT = "0x" + "a" * 40
MARKETS = {"markets": [{"address": "0x" + "1" * 40, "pt": "0x" + "2" * 40}]}
type Adapter = PendleAdapter | MoonwellAdapter


@pytest.fixture(params=[PendleAdapter, MoonwellAdapter], ids=["pendle", "moonwell"])
def adapter(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Adapter:
    instance = request.param(config={})
    if isinstance(instance, PendleAdapter):
        monkeypatch.setattr(instance, "fetch_markets", AsyncMock(return_value=MARKETS))
    return instance


@pytest.fixture
def web3_context(monkeypatch: pytest.MonkeyPatch) -> Mock:
    closed = Mock()

    @asynccontextmanager
    async def context(chain_id: int) -> AsyncIterator[Web3]:
        try:
            yield Web3()
        finally:
            closed(chain_id)

    monkeypatch.setattr(pendle_module, "web3_from_chain_id", context)
    monkeypatch.setattr(moonwell_module, "web3_from_chain_id", context)
    return closed


async def read_positions(
    adapter: Adapter, *, timeout_seconds: float = 60.0
) -> tuple[bool, dict[str, Any] | str]:
    if isinstance(adapter, PendleAdapter):
        return await adapter.get_full_user_state_per_chain(
            chain=42161, account=ACCOUNT, timeout_seconds=timeout_seconds
        )
    return await adapter.get_full_user_state(
        chain_id=8453,
        account=ACCOUNT,
        include_rewards=False,
        timeout_seconds=timeout_seconds,
    )


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("RPC disconnected"),
        ConnectionError(),
        httpx.HTTPStatusError(
            "RPC unavailable",
            request=httpx.Request("POST", "https://example.invalid/rpc"),
            response=httpx.Response(503),
        ),
        Web3RPCError("RPC rejected request"),
        TimeoutError("RPC timed out"),
    ],
    ids=["disconnect", "empty-message", "http-503", "json-rpc", "timeout"],
)
@pytest.mark.parametrize("during_fallback", [False, True], ids=["batch", "fallback"])
async def test_rpc_failure_is_not_an_empty_portfolio(
    adapter: Adapter,
    web3_context: Mock,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    during_fallback: bool,
) -> None:
    failures = (
        [ContractLogicError("batch reverted"), error] if during_fallback else [error]
    )
    aggregate = AsyncMock(side_effect=failures)
    monkeypatch.setattr(MulticallAdapter, "aggregate", aggregate)

    ok, result = await read_positions(adapter)

    assert ok is False
    assert isinstance(result, str) and result
    assert aggregate.await_count == len(failures)
    web3_context.assert_called_once()


async def test_position_timeout_cancels_work_and_closes_provider(
    adapter: Adapter, web3_context: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancelled = asyncio.Event()

    async def stalled_read(*args: Any, **kwargs: Any) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    aggregate = AsyncMock(side_effect=stalled_read)
    monkeypatch.setattr(MulticallAdapter, "aggregate", aggregate)

    ok, result = await read_positions(adapter, timeout_seconds=0.01)

    assert ok is False
    assert isinstance(result, str) and "position scan timed out on chain" in result
    assert cancelled.is_set()
    aggregate.assert_awaited_once()
    web3_context.assert_called_once()


async def test_caller_cancellation_is_not_swallowed(
    adapter: Adapter, web3_context: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    aggregate = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(MulticallAdapter, "aggregate", aggregate)

    with pytest.raises(asyncio.CancelledError):
        await read_positions(adapter)

    aggregate.assert_awaited_once()
    web3_context.assert_called_once()


async def test_successful_zero_balances_remain_successful(
    adapter: Adapter, web3_context: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    if isinstance(adapter, PendleAdapter):
        data = [value.to_bytes(32) for value in (0, 18, 0, 18)]
    else:
        codec = Web3().codec
        data = [
            codec.encode(["address[]"], [[]]),
            codec.encode(["address[]"], [[]]),
            codec.encode(["uint256", "uint256", "uint256"], [0, 0, 0]),
        ]
    monkeypatch.setattr(
        MulticallAdapter,
        "aggregate",
        AsyncMock(return_value=MulticallResult(block_number=1, return_data=data)),
    )

    ok, result = await read_positions(adapter)

    assert ok is True
    assert isinstance(result, dict) and result["positions"] == []
    web3_context.assert_called_once()


async def test_contract_revert_still_salvages_individual_calls(
    adapter: Adapter,
) -> None:
    encoded = (123).to_bytes(32)
    multicall = Mock(spec=MulticallAdapter)
    multicall.aggregate = AsyncMock(
        side_effect=[
            ContractLogicError("batch reverted"),
            MulticallResult(block_number=1, return_data=[encoded]),
            ContractLogicError("deprecated contract"),
        ]
    )
    multicall.decode_uint256 = MulticallAdapter.decode_uint256
    if isinstance(adapter, PendleAdapter):
        values = await adapter._multicall_uint256_chunked(
            multicall=multicall, calls=["first", "second"], chunk_size=400
        )
        assert values == [123, None]
    else:
        raw = await adapter._multicall_chunked(
            multicall=multicall, calls=["first", "second"], chunk_size=240
        )
        assert raw == [encoded, b""]
    assert multicall.aggregate.await_count == 3


async def test_pendle_rpc_failure_keeps_other_chains_positions(
    monkeypatch: pytest.MonkeyPatch, web3_context: Mock
) -> None:
    adapter = PendleAdapter(config={})
    monkeypatch.setattr(adapter, "fetch_markets", AsyncMock(return_value=MARKETS))
    monkeypatch.setattr(
        pendle_module, "PENDLE_CHAIN_IDS", {"ethereum": 1, "arbitrum": 42161}
    )
    aggregate = AsyncMock(
        side_effect=[
            ConnectionError("RPC disconnected"),
            MulticallResult(
                block_number=1,
                return_data=[value.to_bytes(32) for value in (100, 18, 0, 18)],
            ),
        ]
    )
    monkeypatch.setattr(MulticallAdapter, "aggregate", aggregate)

    ok, result = await adapter.get_full_user_state(account=ACCOUNT)

    assert ok is True
    assert isinstance(result, dict)
    assert result["chains"] == [42161]
    assert result["errors"] == ["chain 1: RPC disconnected"]
    assert result["positions"][0]["balances"]["pt"]["raw"] == 100
    assert aggregate.await_count == 2
    assert web3_context.call_count == 2


@pytest.mark.parametrize("all_stalled", [False, True], ids=["partial", "all-chains"])
async def test_pendle_market_discovery_timeout_continues_to_next_chain(
    monkeypatch: pytest.MonkeyPatch, web3_context: Mock, all_stalled: bool
) -> None:
    adapter = PendleAdapter(config={})
    cancelled: list[int] = []

    async def fetch_markets(*, chain_id: int, **kwargs: Any) -> dict[str, Any]:
        if chain_id == 1 or all_stalled:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(chain_id)
        return MARKETS

    monkeypatch.setattr(adapter, "fetch_markets", fetch_markets)
    monkeypatch.setattr(
        pendle_module, "PENDLE_CHAIN_IDS", {"ethereum": 1, "arbitrum": 42161}
    )
    monkeypatch.setattr(
        MulticallAdapter,
        "aggregate",
        AsyncMock(
            return_value=MulticallResult(
                block_number=1,
                return_data=[value.to_bytes(32) for value in (100, 18, 0, 18)],
            )
        ),
    )

    ok, result = await adapter.get_full_user_state(
        account=ACCOUNT, chain_timeout_seconds=0.02
    )

    if all_stalled:
        assert ok is False
        assert (
            isinstance(result, str) and "chain 42161" in result and "chain 1 " in result
        )
        assert cancelled == [1, 42161]
        web3_context.assert_not_called()
    else:
        assert ok is True
        assert isinstance(result, dict)
        assert result["chains"] == [42161]
        assert (
            len(result["errors"]) == 1 and "timed out on chain 1" in result["errors"][0]
        )
        assert result["positions"][0]["balances"]["pt"]["raw"] == 100
        assert cancelled == [1]
        web3_context.assert_called_once_with(42161)
