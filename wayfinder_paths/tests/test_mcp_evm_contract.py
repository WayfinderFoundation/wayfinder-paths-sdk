from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock, patch

import pytest
from web3 import AsyncWeb3

from wayfinder_paths.core.utils.proxy import EIP1967_IMPLEMENTATION_SLOT
from wayfinder_paths.mcp.tools.evm_contract import (
    contract_call,
    contract_execute,
    contract_get_abi,
)


@pytest.mark.asyncio
async def test_contract_call_happy_path():
    contract_addr = "0x" + "12" * 20
    user_addr = "0x" + "34" * 20

    abi = [
        {
            "type": "function",
            "name": "balanceOf",
            "stateMutability": "view",
            "inputs": [{"name": "account", "type": "address"}],
            "outputs": [{"name": "", "type": "uint256"}],
        }
    ]

    expected = 123

    class _Call:
        def __init__(self, args: tuple[object, ...]):
            self._args = args

        async def call(self, _tx: dict | None = None) -> int:
            assert self._args == (AsyncWeb3.to_checksum_address(user_addr),)
            return expected

    class _FnFactory:
        def __call__(self, *args: object) -> _Call:
            return _Call(args)

    class _Contract:
        def get_function_by_signature(self, signature: str) -> _FnFactory:
            assert signature == "balanceOf(address)"
            return _FnFactory()

    class _Eth:
        def contract(self, *, address: str, abi: list[dict]) -> _Contract:  # noqa: A002
            assert address == AsyncWeb3.to_checksum_address(contract_addr)
            assert isinstance(abi, list) and abi
            return _Contract()

    class _W3:
        eth = _Eth()

    @asynccontextmanager
    async def _fake_web3_from_chain_id(chain_id: int):  # noqa: ANN001
        assert chain_id == 1
        yield _W3()

    with patch(
        "wayfinder_paths.mcp.tools.evm_contract.web3_utils.web3_from_chain_id",
        _fake_web3_from_chain_id,
    ):
        out = await contract_call(
            chain_id=1,
            contract_address=contract_addr,
            function_signature="balanceOf(address)",
            args=[user_addr],
            abi=abi,
        )

    assert out["ok"] is True, out
    result = out["result"]
    assert result["contract_address"] == AsyncWeb3.to_checksum_address(contract_addr)
    assert result["function_signature"] == "balanceOf(address)"
    assert result["args"] == [AsyncWeb3.to_checksum_address(user_addr)]
    assert result["value"] == expected


@pytest.mark.asyncio
async def test_contract_call_overload_requires_signature():
    abi = [
        {
            "type": "function",
            "name": "foo",
            "stateMutability": "view",
            "inputs": [{"name": "a", "type": "uint256"}],
            "outputs": [{"name": "", "type": "uint256"}],
        },
        {
            "type": "function",
            "name": "foo",
            "stateMutability": "view",
            "inputs": [{"name": "a", "type": "address"}],
            "outputs": [{"name": "", "type": "uint256"}],
        },
    ]

    out = await contract_call(
        chain_id=1,
        contract_address="0x" + "12" * 20,
        function_name="foo",
        abi=abi,
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "ambiguous_function"


@pytest.mark.asyncio
async def test_contract_call_falls_back_to_etherscan_abi_when_missing():
    contract_addr = "0x" + "12" * 20
    user_addr = "0x" + "34" * 20

    fetched_abi = [
        {
            "type": "function",
            "name": "balanceOf",
            "stateMutability": "view",
            "inputs": [{"name": "account", "type": "address"}],
            "outputs": [{"name": "", "type": "uint256"}],
        }
    ]

    expected = 123

    class _Call:
        def __init__(self, args: tuple[object, ...]):
            self._args = args

        async def call(self, _tx: dict | None = None) -> int:
            assert self._args == (AsyncWeb3.to_checksum_address(user_addr),)
            return expected

    class _FnFactory:
        def __call__(self, *args: object) -> _Call:
            return _Call(args)

    class _Contract:
        def get_function_by_signature(self, signature: str) -> _FnFactory:
            assert signature == "balanceOf(address)"
            return _FnFactory()

    class _Eth:
        def contract(self, *, address: str, abi: list[dict]) -> _Contract:  # noqa: A002
            assert address == AsyncWeb3.to_checksum_address(contract_addr)
            assert abi == fetched_abi
            return _Contract()

    class _W3:
        eth = _Eth()

    @asynccontextmanager
    async def _fake_web3_from_chain_id(chain_id: int):  # noqa: ANN001
        assert chain_id == 1
        yield _W3()

    with (
        patch(
            "wayfinder_paths.mcp.tools.evm_contract.fetch_contract_abi",
            new=AsyncMock(return_value=fetched_abi),
        ),
        patch(
            "wayfinder_paths.mcp.tools.evm_contract.web3_utils.web3_from_chain_id",
            _fake_web3_from_chain_id,
        ),
    ):
        out = await contract_call(
            chain_id=1,
            contract_address=contract_addr,
            function_signature="balanceOf(address)",
            args=[user_addr],
        )

    assert out["ok"] is True, out
    assert out["result"]["abi_source"] == "etherscan_v2"


@pytest.mark.asyncio
async def test_contract_call_uses_proxy_implementation_abi_when_missing_function():
    proxy_addr = "0x" + "12" * 20
    impl_addr = "0x" + "56" * 20
    user_addr = "0x" + "34" * 20

    proxy_abi = [
        {
            "type": "function",
            "name": "upgradeTo",
            "stateMutability": "nonpayable",
            "inputs": [{"name": "implementation", "type": "address"}],
            "outputs": [],
        }
    ]
    impl_abi = [
        {
            "type": "function",
            "name": "balanceOf",
            "stateMutability": "view",
            "inputs": [{"name": "account", "type": "address"}],
            "outputs": [{"name": "", "type": "uint256"}],
        }
    ]

    expected = 123
    impl_storage = b"\x00" * 12 + bytes.fromhex("56" * 20)

    class _Call:
        def __init__(self, args: tuple[object, ...]):
            self._args = args

        async def call(self, _tx: dict | None = None) -> int:
            assert self._args == (AsyncWeb3.to_checksum_address(user_addr),)
            return expected

    class _FnFactory:
        def __call__(self, *args: object) -> _Call:
            return _Call(args)

    class _Contract:
        def get_function_by_signature(self, signature: str) -> _FnFactory:
            assert signature == "balanceOf(address)"
            return _FnFactory()

    class _Eth:
        async def get_storage_at(self, address: str, slot: str):  # noqa: ANN001
            assert address == AsyncWeb3.to_checksum_address(proxy_addr)
            if slot == EIP1967_IMPLEMENTATION_SLOT:
                return impl_storage
            return b"\x00" * 32

        def contract(self, *, address: str, abi: list[dict]) -> _Contract:  # noqa: A002
            assert address == AsyncWeb3.to_checksum_address(proxy_addr)
            assert abi == impl_abi
            return _Contract()

    class _W3:
        eth = _Eth()

    @asynccontextmanager
    async def _fake_web3_from_chain_id(chain_id: int):  # noqa: ANN001
        assert chain_id == 1
        yield _W3()

    async def _fake_fetch_contract_abi(chain_id: int, address: str, **_kwargs):  # noqa: ANN001
        assert chain_id == 1
        if address.lower() == proxy_addr.lower():
            return proxy_abi
        if address.lower() == impl_addr.lower():
            return impl_abi
        raise AssertionError(f"unexpected ABI fetch address: {address}")

    with (
        patch(
            "wayfinder_paths.mcp.tools.evm_contract.fetch_contract_abi",
            new=AsyncMock(side_effect=_fake_fetch_contract_abi),
        ),
        patch(
            "wayfinder_paths.mcp.tools.evm_contract.web3_utils.web3_from_chain_id",
            _fake_web3_from_chain_id,
        ),
    ):
        out = await contract_call(
            chain_id=1,
            contract_address=proxy_addr,
            function_signature="balanceOf(address)",
            args=[user_addr],
        )

    assert out["ok"] is True, out
    result = out["result"]
    assert result["abi_source"] == "etherscan_v2_proxy"
    assert result["implementation_address"] == AsyncWeb3.to_checksum_address(impl_addr)
    assert result["value"] == expected


@pytest.mark.asyncio
async def test_contract_execute_rejects_view_function():
    wallet = {
        "address": "0x" + "aa" * 20,
        "private_key_hex": "0x" + "11" * 32,
    }
    abi = [
        {
            "type": "function",
            "name": "getValue",
            "stateMutability": "view",
            "inputs": [],
            "outputs": [{"name": "", "type": "uint256"}],
        }
    ]

    with patch(
        "wayfinder_paths.mcp.tools.evm_contract.find_wallet_by_label",
        return_value=wallet,
    ):
        out = await contract_execute(
            wallet_label="main",
            chain_id=1,
            contract_address="0x" + "12" * 20,
            function_name="getValue",
            abi=abi,
        )

    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_function"


@pytest.mark.asyncio
async def test_contract_execute_encodes_sends_and_annotates():
    wallet = {
        "address": "0x" + "aa" * 20,
        "private_key_hex": "0x" + "11" * 32,
    }
    contract_addr = "0x" + "12" * 20
    abi = [
        {
            "type": "function",
            "name": "deposit",
            "stateMutability": "nonpayable",
            "inputs": [{"name": "amount", "type": "uint256"}],
            "outputs": [],
        }
    ]

    fake_store = Mock()
    fake_store.annotate_safe = Mock()

    fake_encode = AsyncMock(return_value={"to": contract_addr, "data": "0xdeadbeef"})
    fake_send = AsyncMock(return_value="0x" + "99" * 32)

    with (
        patch(
            "wayfinder_paths.mcp.tools.evm_contract.find_wallet_by_label",
            return_value=wallet,
        ),
        patch(
            "wayfinder_paths.mcp.tools.evm_contract.WalletProfileStore.default",
            return_value=fake_store,
        ),
        patch(
            "wayfinder_paths.mcp.tools.evm_contract.encode_call",
            fake_encode,
        ),
        patch(
            "wayfinder_paths.mcp.tools.evm_contract.send_transaction",
            fake_send,
        ),
        patch(
            "wayfinder_paths.mcp.tools.evm_contract.get_etherscan_transaction_link",
            return_value="https://example.invalid/tx/0x" + "99" * 32,
        ),
    ):
        out = await contract_execute(
            wallet_label="main",
            chain_id=1,
            contract_address=contract_addr,
            function_name="deposit",
            args='["5"]',
            abi=abi,
            wait_for_receipt=False,
        )

    assert out["ok"] is True, out
    result = out["result"]
    assert result["tx_hash"].startswith("0x")
    assert result["contract_address"] == AsyncWeb3.to_checksum_address(contract_addr)
    assert result["function_signature"] == "deposit(uint256)"
    assert result["args"] == [5]

    fake_encode.assert_awaited_once()
    _kwargs = fake_encode.await_args.kwargs
    assert _kwargs["target"] == contract_addr
    assert _kwargs["fn_name"] == "deposit(uint256)"
    assert _kwargs["args"] == [5]

    fake_send.assert_awaited_once()
    assert fake_send.await_args.kwargs.get("wait_for_receipt") is False
    fake_store.annotate_safe.assert_called()
    assert fake_store.annotate_safe.call_args.kwargs.get("status") == "broadcast"


@pytest.mark.asyncio
async def test_contract_get_abi_fetches_from_etherscan():
    addr = "0x" + "12" * 20
    fetched_abi = [
        {
            "type": "function",
            "name": "balanceOf",
            "stateMutability": "view",
            "inputs": [{"name": "account", "type": "address"}],
            "outputs": [{"name": "", "type": "uint256"}],
        }
    ]

    with patch(
        "wayfinder_paths.mcp.tools.evm_contract.fetch_contract_abi",
        new=AsyncMock(return_value=fetched_abi),
    ):
        out = await contract_get_abi(
            chain_id=1, contract_address=addr, resolve_proxy=False
        )

    assert out["ok"] is True, out
    result = out["result"]
    assert result["abi"] == fetched_abi
    assert result["abi_source"] == "etherscan_v2"


@pytest.mark.asyncio
async def test_contract_get_abi_prefers_proxy_implementation():
    proxy_addr = "0x" + "12" * 20
    impl_addr = "0x" + "56" * 20
    impl_abi = [
        {
            "type": "function",
            "name": "balanceOf",
            "stateMutability": "view",
            "inputs": [{"name": "account", "type": "address"}],
            "outputs": [{"name": "", "type": "uint256"}],
        }
    ]

    async def _fake_fetch(chain_id: int, contract_address: str, **_kwargs):  # noqa: ANN001
        assert chain_id == 1
        assert contract_address.lower() == impl_addr.lower()
        return impl_abi

    with (
        patch(
            "wayfinder_paths.mcp.tools.evm_contract.resolve_proxy_implementation",
            new=AsyncMock(
                return_value=(AsyncWeb3.to_checksum_address(impl_addr), "EIP1967")
            ),
        ),
        patch(
            "wayfinder_paths.mcp.tools.evm_contract.fetch_contract_abi",
            new=AsyncMock(side_effect=_fake_fetch),
        ),
    ):
        out = await contract_get_abi(
            chain_id=1, contract_address=proxy_addr, resolve_proxy=True
        )

    assert out["ok"] is True, out
    result = out["result"]
    assert result["abi_source"] == "etherscan_v2_proxy"
    assert result["implementation_address"] == AsyncWeb3.to_checksum_address(impl_addr)
    assert result["abi"] == impl_abi


@pytest.mark.asyncio
async def test_contract_get_abi_falls_back_to_proxy_abi_when_impl_fetch_fails():
    proxy_addr = "0x" + "12" * 20
    impl_addr = "0x" + "56" * 20
    proxy_abi = [
        {
            "type": "function",
            "name": "upgradeTo",
            "stateMutability": "nonpayable",
            "inputs": [{"name": "implementation", "type": "address"}],
            "outputs": [],
        }
    ]

    async def _fake_fetch(chain_id: int, contract_address: str, **_kwargs):  # noqa: ANN001
        assert chain_id == 1
        if contract_address.lower() == impl_addr.lower():
            raise ValueError("Contract source code not verified")
        if contract_address.lower() == proxy_addr.lower():
            return proxy_abi
        raise AssertionError(f"unexpected ABI fetch address: {contract_address}")

    with (
        patch(
            "wayfinder_paths.mcp.tools.evm_contract.resolve_proxy_implementation",
            new=AsyncMock(
                return_value=(AsyncWeb3.to_checksum_address(impl_addr), "EIP1967")
            ),
        ),
        patch(
            "wayfinder_paths.mcp.tools.evm_contract.fetch_contract_abi",
            new=AsyncMock(side_effect=_fake_fetch),
        ),
    ):
        out = await contract_get_abi(
            chain_id=1, contract_address=proxy_addr, resolve_proxy=True
        )

    assert out["ok"] is True, out
    result = out["result"]
    assert result["abi_source"] == "etherscan_v2"
    assert result["implementation_address"] == AsyncWeb3.to_checksum_address(impl_addr)
    assert "implementation_abi_error" in result
    assert result["abi"] == proxy_abi
