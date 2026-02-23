from __future__ import annotations

from typing import Any

from web3 import AsyncWeb3

from wayfinder_paths.core.utils import web3 as web3_utils

EIP1967_IMPLEMENTATION_SLOT = (
    "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
)
ZEPPELINOS_IMPLEMENTATION_SLOT = (
    "0x7050c9e0f4ca769c69bd3a8ef740bc37934f8e2c036e5a723fd8ee048ed3f8c3"
)

EIP897_ABI: list[dict[str, Any]] = [
    {
        "constant": True,
        "inputs": [],
        "name": "proxyType",
        "outputs": [{"name": "proxyTypeId", "type": "uint256"}],
        "payable": False,
        "stateMutability": "pure",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "implementation",
        "outputs": [{"name": "codeAddr", "type": "address"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    },
]


async def _impl_from_storage_slot(
    w3: AsyncWeb3, *, address: str, slot: str
) -> str | None:
    try:
        storage = await w3.eth.get_storage_at(address, slot)
    except Exception:
        return None

    if not storage or int.from_bytes(storage, "big") == 0:
        return None

    if len(storage) < 20:
        return None

    impl_bytes = storage[-20:]
    if int.from_bytes(impl_bytes, "big") == 0:
        return None

    return AsyncWeb3.to_checksum_address("0x" + impl_bytes.hex())


async def resolve_proxy_implementation_with_web3(
    w3: AsyncWeb3, address: str
) -> tuple[str | None, str | None]:
    """Return (implementation_address, proxy_flavour) for common proxy patterns."""
    try:
        proxy_addr = AsyncWeb3.to_checksum_address(address)
    except Exception:
        return None, None

    impl = await _impl_from_storage_slot(
        w3, address=proxy_addr, slot=EIP1967_IMPLEMENTATION_SLOT
    )
    if impl:
        return impl, "EIP1967"

    impl = await _impl_from_storage_slot(
        w3, address=proxy_addr, slot=ZEPPELINOS_IMPLEMENTATION_SLOT
    )
    if impl:
        return impl, "ZeppelinOS"

    try:
        contract = w3.eth.contract(address=proxy_addr, abi=EIP897_ABI)
        proxy_type = await contract.functions.proxyType().call()
        if int(proxy_type) not in (1, 2):
            return None, None
        implementation_address = await contract.functions.implementation().call()
        impl_addr = AsyncWeb3.to_checksum_address(implementation_address)
        if int(impl_addr, 16) == 0:
            return None, None
        return impl_addr, "EIP897"
    except Exception:
        return None, None


async def resolve_proxy_implementation(
    chain_id: int, address: str
) -> tuple[str | None, str | None]:
    async with web3_utils.web3_from_chain_id(int(chain_id)) as w3:
        return await resolve_proxy_implementation_with_web3(w3, address)
