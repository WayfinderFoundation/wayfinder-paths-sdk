from __future__ import annotations

import pytest
from eth_utils import to_checksum_address

from wayfinder_paths.adapters.multicall_adapter.adapter import MulticallAdapter
from wayfinder_paths.core.constants.contracts import MULTICALL3_ADDRESS
from wayfinder_paths.core.constants.erc20_abi import ERC20_ABI
from wayfinder_paths.core.utils.multicall import (
    ReadOnlyCall,
    read_only_calls_multicall_or_gather,
)
from wayfinder_paths.core.utils.web3 import web3_from_chain_id
from wayfinder_paths.testing.gorlami import gorlami_configured

pytestmark = pytest.mark.skipif(
    not gorlami_configured(),
    reason="api_key not configured (needed for gorlami fork proxy)",
)


UNISWAP_V3_POOL_USDC_WETH_003 = to_checksum_address(
    "0x8ad599c3a0ff1de082011efddc58f1908eb6e6d8"
)
UNISWAP_V3_POOL_ABI_MIN = [
    {
        "inputs": [],
        "name": "slot0",
        "outputs": [
            {"internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"},
            {"internalType": "int24", "name": "tick", "type": "int24"},
            {"internalType": "uint16", "name": "observationIndex", "type": "uint16"},
            {
                "internalType": "uint16",
                "name": "observationCardinality",
                "type": "uint16",
            },
            {
                "internalType": "uint16",
                "name": "observationCardinalityNext",
                "type": "uint16",
            },
            {"internalType": "uint8", "name": "feeProtocol", "type": "uint8"},
            {"internalType": "bool", "name": "unlocked", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "liquidity",
        "outputs": [{"internalType": "uint128", "name": "", "type": "uint128"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "fee",
        "outputs": [{"internalType": "uint24", "name": "", "type": "uint24"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "token0",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "token1",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_multicall_helper_uses_multicall3_on_mainnet_fork(gorlami, monkeypatch):
    async with web3_from_chain_id(1) as web3:
        try:
            code = await web3.eth.get_code(MULTICALL3_ADDRESS)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"gorlami RPC unreachable: {exc}")
        assert code and len(bytes(code)) > 0

        pool = web3.eth.contract(
            address=UNISWAP_V3_POOL_USDC_WETH_003,
            abi=UNISWAP_V3_POOL_ABI_MIN,
        )

        called = {"n": 0}
        real_aggregate = MulticallAdapter.aggregate

        async def _wrapped_aggregate(self, calls, *, value=0, block_identifier=None):
            called["n"] += 1
            return await real_aggregate(
                self,
                calls,
                value=value,
                block_identifier=block_identifier,
            )

        monkeypatch.setattr(MulticallAdapter, "aggregate", _wrapped_aggregate)

        def _cs(a: object) -> str:
            return to_checksum_address(str(a))

        slot0, fee, liq, token0, token1 = await read_only_calls_multicall_or_gather(
            web3=web3,
            chain_id=1,
            calls=[
                ReadOnlyCall(pool, "slot0"),
                ReadOnlyCall(pool, "fee", postprocess=int),
                ReadOnlyCall(pool, "liquidity", postprocess=int),
                ReadOnlyCall(pool, "token0", postprocess=_cs),
                ReadOnlyCall(pool, "token1", postprocess=_cs),
            ],
            block_identifier="latest",
        )

        assert called["n"] >= 1
        assert isinstance(slot0, tuple) and len(slot0) == 7
        assert isinstance(fee, int) and fee == 3000
        assert isinstance(liq, int) and liq >= 0
        assert isinstance(token0, str) and token0.startswith("0x")
        assert isinstance(token1, str) and token1.startswith("0x")

        # Also sanity-check decoding of ERC20 string + int outputs via the helper.
        erc20_0 = web3.eth.contract(address=token0, abi=ERC20_ABI)
        erc20_1 = web3.eth.contract(address=token1, abi=ERC20_ABI)
        dec0, sym0, dec1, sym1 = await read_only_calls_multicall_or_gather(
            web3=web3,
            chain_id=1,
            calls=[
                ReadOnlyCall(erc20_0, "decimals", postprocess=int),
                ReadOnlyCall(erc20_0, "symbol", postprocess=str),
                ReadOnlyCall(erc20_1, "decimals", postprocess=int),
                ReadOnlyCall(erc20_1, "symbol", postprocess=str),
            ],
            block_identifier="latest",
        )
        assert isinstance(dec0, int) and 0 <= dec0 <= 36
        assert isinstance(dec1, int) and 0 <= dec1 <= 36
        assert isinstance(sym0, str) and sym0
        assert isinstance(sym1, str) and sym1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_multicall_helper_falls_back_when_multicall_reverts(gorlami, monkeypatch):
    async with web3_from_chain_id(1) as web3:
        try:
            await web3.eth.get_block_number()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"gorlami RPC unreachable: {exc}")
        pool = web3.eth.contract(
            address=UNISWAP_V3_POOL_USDC_WETH_003,
            abi=UNISWAP_V3_POOL_ABI_MIN,
        )

        async def _boom(*_args, **_kwargs):
            raise RuntimeError("forced multicall failure")

        monkeypatch.setattr(MulticallAdapter, "aggregate", _boom)

        slot0_mc, fee_mc = await read_only_calls_multicall_or_gather(
            web3=web3,
            chain_id=1,
            calls=[
                ReadOnlyCall(pool, "slot0"),
                ReadOnlyCall(pool, "fee", postprocess=int),
            ],
            block_identifier="latest",
        )

        slot0_direct = await pool.functions.slot0().call(block_identifier="latest")
        fee_direct = await pool.functions.fee().call(block_identifier="latest")

        assert tuple(slot0_mc) == tuple(slot0_direct)
        assert int(fee_mc) == int(fee_direct)
