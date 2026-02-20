from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from web3 import AsyncWeb3

from wayfinder_paths.core.utils.proxy import (
    EIP1967_IMPLEMENTATION_SLOT,
    resolve_proxy_implementation,
)


@pytest.mark.asyncio
async def test_resolve_proxy_implementation_eip1967_slot():
    proxy_addr = "0x" + "12" * 20
    impl_addr = "0x" + "56" * 20

    impl_bytes = bytes.fromhex("56" * 20)
    storage_value = b"\x00" * 12 + impl_bytes

    class _Eth:
        async def get_storage_at(self, address: str, slot: str):  # noqa: ANN001
            assert address == AsyncWeb3.to_checksum_address(proxy_addr)
            if slot == EIP1967_IMPLEMENTATION_SLOT:
                return storage_value
            return b"\x00" * 32

        def contract(self, *, address: str, abi: list[dict]):  # noqa: A002, ANN001
            raise AssertionError(f"unexpected contract() call for {address}")

    class _W3:
        eth = _Eth()

    @asynccontextmanager
    async def _fake_web3_from_chain_id(chain_id: int):  # noqa: ANN001
        assert chain_id == 1
        yield _W3()

    with patch(
        "wayfinder_paths.core.utils.proxy.web3_utils.web3_from_chain_id",
        _fake_web3_from_chain_id,
    ):
        got_impl, flavour = await resolve_proxy_implementation(1, proxy_addr)

    assert got_impl == AsyncWeb3.to_checksum_address(impl_addr)
    assert flavour == "EIP1967"

