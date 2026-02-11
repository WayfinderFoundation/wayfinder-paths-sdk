from __future__ import annotations

import pytest
from web3 import AsyncHTTPProvider, AsyncWeb3

from wayfinder_paths.adapters.multicall_adapter.adapter import (
    MulticallAdapter,
    MulticallCall,
)


class _DummyAggregate:
    def __init__(self, expected_value: int, result):
        self._expected_value = expected_value
        self._result = result

    async def call(self, tx_params, block_identifier=None):
        assert tx_params == {"value": self._expected_value}
        assert block_identifier is None
        return self._result


class _DummyFunctions:
    def __init__(self, expected_calls, expected_value: int, result):
        self._expected_calls = expected_calls
        self._expected_value = expected_value
        self._result = result

    def aggregate(self, calls):
        assert calls == self._expected_calls
        return _DummyAggregate(self._expected_value, self._result)


class _DummyContract:
    def __init__(self, expected_calls, expected_value: int, result):
        self.functions = _DummyFunctions(expected_calls, expected_value, result)


class TestMulticallAdapter:
    def test_normalize_call_data_hex_str(self):
        raw = MulticallAdapter._normalize_call_data("0x1234")
        assert raw == b"\x12\x34"

    def test_decode_uint256(self):
        value = 123
        encoded = value.to_bytes(32, "big")
        assert MulticallAdapter.decode_uint256(encoded) == value

    def test_encode_balance_calls_produce_bytes(self):
        w3 = AsyncWeb3(AsyncHTTPProvider("http://localhost:8545"))
        adapter = MulticallAdapter(web3=w3)

        eth_call = adapter.encode_eth_balance(
            "0x0000000000000000000000000000000000000002"
        )
        assert isinstance(eth_call, MulticallCall)
        assert isinstance(eth_call.call_data, (bytes, bytearray))
        assert len(eth_call.call_data) > 0

        erc20_call = adapter.encode_erc20_balance(
            "0x0000000000000000000000000000000000000001",
            "0x0000000000000000000000000000000000000002",
        )
        assert isinstance(erc20_call, MulticallCall)
        assert isinstance(erc20_call.call_data, (bytes, bytearray))
        assert len(erc20_call.call_data) > 0

    @pytest.mark.asyncio
    async def test_aggregate_coerces_calls_and_returns_bytes(self):
        w3 = AsyncWeb3(AsyncHTTPProvider("http://localhost:8545"))
        adapter = MulticallAdapter(web3=w3)

        call = adapter.build_call(
            "0x0000000000000000000000000000000000000001", "0x1234"
        )
        expected_calls = [
            (w3.to_checksum_address(call.target), b"\x12\x34"),
        ]

        dummy_contract = _DummyContract(
            expected_calls=expected_calls,
            expected_value=7,
            result=(123, ["0x" + ("00" * 32)]),
        )
        adapter.contract = dummy_contract

        result = await adapter.aggregate([call], value=7)
        assert result.block_number == 123
        assert result.return_data == (b"\x00" * 32,)

    @pytest.mark.asyncio
    async def test_aggregate_empty_returns_empty_result(self):
        w3 = AsyncWeb3(AsyncHTTPProvider("http://localhost:8545"))
        adapter = MulticallAdapter(web3=w3)

        result = await adapter.aggregate([])
        assert result.block_number == 0
        assert list(result.return_data) == []
