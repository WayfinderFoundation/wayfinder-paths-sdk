from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from web3 import AsyncHTTPProvider, AsyncWeb3

from wayfinder_paths.adapters.multicall_adapter.adapter import MulticallAdapter
from wayfinder_paths.core.constants.contracts import (
    BASE_USDC,
    BASE_WETH,
    KHYPE_ADDRESS,
)
from wayfinder_paths.core.constants.erc20_abi import ERC20_ABI
from wayfinder_paths.core.utils import multicall as multicall_mod
from wayfinder_paths.core.utils.multicall import (
    ReadOnlyCall,
    _multicall3_supported,
    read_only_calls_multicall_or_gather,
)
from wayfinder_paths.core.utils.web3 import web3_from_chain_id


@dataclass
class _FnDef:
    outputs: list[dict[str, str]]


class _DummyCall:
    def __init__(self, value: Any):
        self._value = value

    async def call(self, _tx_params=None, block_identifier=None):  # matches web3 shape
        return self._value


class _DummyContractFunctions:
    def __init__(self, results: dict[tuple[str, tuple[Any, ...]], Any]):
        self._results = results

    def __getattr__(self, name: str):
        def _fn(*args: Any):
            key = (name, tuple(args))
            if key not in self._results:
                raise AttributeError(f"Missing dummy result for {key!r}")
            return _DummyCall(self._results[key])

        return _fn


class _DummyContract:
    def __init__(
        self,
        *,
        address: str,
        fn_defs: dict[str, _FnDef],
        call_results: dict[tuple[str, tuple[Any, ...]], Any],
    ) -> None:
        self.address = address
        self._fn_defs = fn_defs
        self.functions = _DummyContractFunctions(call_results)

    def encode_abi(self, _fn_name: str, *, args: list[Any] | None = None) -> str:
        # The util only needs "some" calldata; the multicall adapter normalizes it.
        # Tests patch MulticallAdapter.aggregate so it never inspects calldata.
        _ = args
        return "0x1234"

    def get_function_by_name(self, fn_name: str):
        if fn_name not in self._fn_defs:
            raise ValueError(f"Unknown dummy function: {fn_name}")
        outputs = self._fn_defs[fn_name].outputs
        return type("_Fn", (), {"abi": {"outputs": outputs}})()


@pytest.mark.asyncio
async def test_read_only_calls_multicall_or_gather_decodes_outputs(monkeypatch):
    web3 = AsyncWeb3(AsyncHTTPProvider("http://localhost:8545"))

    async def _code_ok(_addr):
        return b"\x01"

    monkeypatch.setattr(web3.eth, "get_code", _code_ok)

    dummy = _DummyContract(
        address="0x0000000000000000000000000000000000000001",
        fn_defs={
            "foo": _FnDef(outputs=[{"name": "", "type": "uint256"}]),
            "bar": _FnDef(
                outputs=[
                    {"name": "a", "type": "uint256"},
                    {"name": "b", "type": "bool"},
                ]
            ),
        },
        call_results={},
    )

    real_aggregate = MulticallAdapter.aggregate

    @dataclass
    class _Res:
        return_data: tuple[bytes, ...]

    async def _fake_aggregate(self, calls, *, value=0, block_identifier=None):
        _ = (self, calls, value, block_identifier)
        encoded_foo = web3.codec.encode(["uint256"], [123])
        encoded_bar = web3.codec.encode(["uint256", "bool"], [456, True])
        return _Res(return_data=(encoded_foo, encoded_bar))

    monkeypatch.setattr(MulticallAdapter, "aggregate", _fake_aggregate)

    foo, bar = await read_only_calls_multicall_or_gather(
        web3=web3,
        chain_id=1,
        calls=[
            ReadOnlyCall(dummy, "foo"),
            ReadOnlyCall(dummy, "bar"),
        ],
        block_identifier="latest",
    )

    assert foo == 123
    assert bar == (456, True)

    monkeypatch.setattr(MulticallAdapter, "aggregate", real_aggregate)


@pytest.mark.asyncio
async def test_read_only_calls_multicall_or_gather_falls_back_on_multicall_error(
    monkeypatch,
):
    web3 = AsyncWeb3(AsyncHTTPProvider("http://localhost:8545"))

    async def _code_ok(_addr):
        return b"\x01"

    monkeypatch.setattr(web3.eth, "get_code", _code_ok)

    dummy = _DummyContract(
        address="0x0000000000000000000000000000000000000001",
        fn_defs={"foo": _FnDef(outputs=[{"name": "", "type": "uint256"}])},
        call_results={
            ("foo", ()): 999,
        },
    )

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("forced")

    monkeypatch.setattr(MulticallAdapter, "aggregate", _boom)

    (foo,) = await read_only_calls_multicall_or_gather(
        web3=web3,
        chain_id=1,
        calls=[ReadOnlyCall(dummy, "foo")],
        block_identifier="latest",
    )
    assert foo == 999


@pytest.mark.asyncio
async def test_read_only_calls_multicall_or_gather_falls_back_when_no_multicall_code(
    monkeypatch,
):
    web3 = AsyncWeb3(AsyncHTTPProvider("http://localhost:8545"))

    async def _no_code(_addr):
        return b""

    monkeypatch.setattr(web3.eth, "get_code", _no_code)

    dummy = _DummyContract(
        address="0x0000000000000000000000000000000000000001",
        fn_defs={"foo": _FnDef(outputs=[{"name": "", "type": "uint256"}])},
        call_results={
            ("foo", ()): 111,
        },
    )

    async def _should_not_be_called(*_args, **_kwargs):
        raise AssertionError("MulticallAdapter.aggregate should not be called")

    monkeypatch.setattr(MulticallAdapter, "aggregate", _should_not_be_called)

    (foo,) = await read_only_calls_multicall_or_gather(
        web3=web3,
        chain_id=1,
        calls=[ReadOnlyCall(dummy, "foo")],
        block_identifier="latest",
    )
    assert foo == 111


# ---------------------------------------------------------------------------
# Live network tests (require configured RPCs in config.json)
# ---------------------------------------------------------------------------


async def _web3_or_skip(chain_id: int):
    """Return (ctx, web3) for *chain_id*, or skip if RPC is unreachable."""
    ctx = web3_from_chain_id(chain_id)
    try:
        web3 = await ctx.__aenter__()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"RPC unreachable for chain {chain_id}: {exc}")
    try:
        await web3.eth.get_block_number()
    except Exception as exc:  # noqa: BLE001
        await ctx.__aexit__(None, None, None)
        pytest.skip(f"RPC unreachable for chain {chain_id}: {exc}")
    return ctx, web3


@pytest.mark.asyncio
@pytest.mark.local
async def test_live_multicall3_on_base(monkeypatch):
    """Multicall3 is deployed on Base — verify aggregation is used and results decode."""
    ctx, web3 = await _web3_or_skip(8453)
    try:
        assert await _multicall3_supported(web3), "Multicall3 not found on Base"

        called = {"n": 0}
        real_aggregate = MulticallAdapter.aggregate

        async def _spy(self, calls, *, value=0, block_identifier=None):
            called["n"] += 1
            return await real_aggregate(
                self, calls, value=value, block_identifier=block_identifier
            )

        monkeypatch.setattr(MulticallAdapter, "aggregate", _spy)

        usdc = web3.eth.contract(address=BASE_USDC, abi=ERC20_ABI)
        weth = web3.eth.contract(address=BASE_WETH, abi=ERC20_ABI)

        (
            usdc_dec,
            usdc_sym,
            weth_dec,
            weth_sym,
        ) = await read_only_calls_multicall_or_gather(
            web3=web3,
            chain_id=8453,
            calls=[
                ReadOnlyCall(usdc, "decimals", postprocess=int),
                ReadOnlyCall(usdc, "symbol", postprocess=str),
                ReadOnlyCall(weth, "decimals", postprocess=int),
                ReadOnlyCall(weth, "symbol", postprocess=str),
            ],
            block_identifier="latest",
        )

        assert called["n"] >= 1, "Expected multicall aggregation on Base"
        assert usdc_dec == 6
        assert usdc_sym == "USDC"
        assert weth_dec == 18
        assert weth_sym == "WETH"
    finally:
        await ctx.__aexit__(None, None, None)


@pytest.mark.asyncio
@pytest.mark.local
async def test_live_multicall3_chunking_on_base(monkeypatch):
    """Verify chunk_size splits calls into multiple multicall batches."""
    ctx, web3 = await _web3_or_skip(8453)
    try:
        assert await _multicall3_supported(web3), "Multicall3 not found on Base"

        batch_count = {"n": 0}
        real_aggregate = MulticallAdapter.aggregate

        async def _spy(self, calls, *, value=0, block_identifier=None):
            batch_count["n"] += 1
            return await real_aggregate(
                self, calls, value=value, block_identifier=block_identifier
            )

        monkeypatch.setattr(MulticallAdapter, "aggregate", _spy)

        usdc = web3.eth.contract(address=BASE_USDC, abi=ERC20_ABI)

        results = await read_only_calls_multicall_or_gather(
            web3=web3,
            chain_id=8453,
            calls=[
                ReadOnlyCall(usdc, "name", postprocess=str),
                ReadOnlyCall(usdc, "symbol", postprocess=str),
                ReadOnlyCall(usdc, "decimals", postprocess=int),
                ReadOnlyCall(usdc, "totalSupply", postprocess=int),
            ],
            block_identifier="latest",
            chunk_size=2,
        )

        assert batch_count["n"] == 2, f"Expected 2 batches, got {batch_count['n']}"
        name, symbol, decimals, total_supply = results
        assert name == "USD Coin"
        assert symbol == "USDC"
        assert decimals == 6
        assert total_supply > 0
    finally:
        await ctx.__aexit__(None, None, None)


@pytest.mark.asyncio
@pytest.mark.local
async def test_live_fallback_when_multicall_unsupported(monkeypatch):
    """Force _multicall3_supported=False on HyperEVM and verify fallback to individual calls."""
    ctx, web3 = await _web3_or_skip(999)
    try:

        async def _unsupported(*_a, **_kw):
            return False

        monkeypatch.setattr(multicall_mod, "_multicall3_supported", _unsupported)

        called = {"n": 0}

        async def _spy_aggregate(*_args, **_kwargs):
            called["n"] += 1
            raise AssertionError("aggregate should not be called")

        monkeypatch.setattr(MulticallAdapter, "aggregate", _spy_aggregate)

        erc20 = web3.eth.contract(address=KHYPE_ADDRESS, abi=ERC20_ABI)

        name, symbol, decimals = await read_only_calls_multicall_or_gather(
            web3=web3,
            chain_id=999,
            calls=[
                ReadOnlyCall(erc20, "name", postprocess=str),
                ReadOnlyCall(erc20, "symbol", postprocess=str),
                ReadOnlyCall(erc20, "decimals", postprocess=int),
            ],
            block_identifier="latest",
        )

        assert called["n"] == 0, "aggregate should never be called when unsupported"
        assert isinstance(name, str) and name
        assert isinstance(symbol, str) and symbol
        assert isinstance(decimals, int) and 0 <= decimals <= 36
    finally:
        await ctx.__aexit__(None, None, None)


@pytest.mark.asyncio
@pytest.mark.local
async def test_live_multicall_matches_direct_calls_on_base():
    """Multicall results must match individual eth_call results exactly."""
    ctx, web3 = await _web3_or_skip(8453)
    try:
        usdc = web3.eth.contract(address=BASE_USDC, abi=ERC20_ABI)

        # Direct calls
        direct_name = await usdc.functions.name().call()
        direct_decimals = await usdc.functions.decimals().call()
        direct_supply = await usdc.functions.totalSupply().call()

        # Multicall
        mc_name, mc_decimals, mc_supply = await read_only_calls_multicall_or_gather(
            web3=web3,
            chain_id=8453,
            calls=[
                ReadOnlyCall(usdc, "name"),
                ReadOnlyCall(usdc, "decimals"),
                ReadOnlyCall(usdc, "totalSupply"),
            ],
            block_identifier="latest",
        )

        assert mc_name == direct_name
        assert mc_decimals == direct_decimals
        assert mc_supply == direct_supply
    finally:
        await ctx.__aexit__(None, None, None)
