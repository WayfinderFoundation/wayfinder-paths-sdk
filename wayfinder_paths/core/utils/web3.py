from contextlib import asynccontextmanager
from urllib.parse import urlparse

from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.middleware import ExtraDataToPOAMiddleware
from web3.module import Module

from wayfinder_paths.core.config import get_api_key, get_rpc_urls
from wayfinder_paths.core.constants.chains import (
    CHAIN_ID_HYPEREVM,
    POA_MIDDLEWARE_CHAIN_IDS,
)


def is_wayfinder_rpc(rpc: str) -> bool:
    parsed = urlparse(rpc)
    return parsed.netloc == "strategies.wayfinder.ai"


class HyperModule(Module):
    def __init__(self, w3):
        super().__init__(w3)

    async def big_block_gas_price(self):
        big_block_gas_price = await self.w3.manager.coro_request(
            "eth_bigBlockGasPrice", []
        )
        return int(big_block_gas_price, 16)


def _get_rpcs_for_chain_id(chain_id: int) -> list:
    mapping = get_rpc_urls()
    rpcs = mapping.get(str(chain_id))
    if rpcs is None:
        rpcs = mapping.get(chain_id)  # allow int keys
    if rpcs is None:
        raise ValueError(f"No RPCs configured for chain ID {chain_id}")
    if isinstance(rpcs, str):
        return [rpcs]
    return rpcs


def _get_web3(rpc: str, chain_id: int) -> AsyncWeb3:
    request_kwargs = {}

    if is_wayfinder_rpc(rpc):
        api_key = get_api_key()
        if api_key:
            request_kwargs.update(
                {"X-API-Key": api_key, "Content-Type": "application/json"}
            )

    web3 = AsyncWeb3(AsyncHTTPProvider(rpc, request_kwargs=request_kwargs))
    if chain_id in POA_MIDDLEWARE_CHAIN_IDS:
        web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if chain_id == CHAIN_ID_HYPEREVM:
        web3.attach_modules({"hype": (HyperModule)})
    return web3


def get_transaction_chain_id(transaction: dict) -> int:
    if "chainId" not in transaction:
        raise ValueError("Transaction does not contain chainId")
    return int(transaction["chainId"])


def get_web3s_from_chain_id(chain_id: int) -> list[AsyncWeb3]:
    rpcs = _get_rpcs_for_chain_id(chain_id)
    return [_get_web3(rpc, chain_id) for rpc in rpcs]


@asynccontextmanager
async def web3s_from_chain_id(chain_id: int):
    web3s = get_web3s_from_chain_id(chain_id)
    try:
        yield web3s
    finally:
        for web3 in web3s:
            await web3.provider.disconnect()


@asynccontextmanager
async def web3_from_chain_id(chain_id: int):
    web3s = get_web3s_from_chain_id(chain_id)
    try:
        yield web3s[0]
    finally:
        await web3s[0].provider.disconnect()
