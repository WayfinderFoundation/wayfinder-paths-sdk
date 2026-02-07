import asyncio
from contextlib import asynccontextmanager

from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.middleware import ExtraDataToPOAMiddleware
from web3.module import Module

from wayfinder_paths.core.config import (
    get_gorlami_api_key,
    get_gorlami_base_url,
    get_rpc_urls,
)
from wayfinder_paths.core.constants.chains import (
    CHAIN_ID_HYPEREVM,
    POA_MIDDLEWARE_CHAIN_IDS,
)


class HyperModule(Module):
    def __init__(self, w3):
        super().__init__(w3)

    async def big_block_gas_price(self):
        big_block_gas_price = await self.w3.manager.coro_request(
            "eth_bigBlockGasPrice", []
        )
        return int(big_block_gas_price, 16)


class _GorlamiProvider(AsyncHTTPProvider):
    async def make_request(self, method, params):  # type: ignore[override]
        # Gorlami's JSON-RPC responses omit `id`, which breaks web3.py.
        # It can also intermittently return 502/503/504, so retry a bit.
        req = self.form_request(method, params)
        request_data = self.encode_rpc_dict(req)

        max_retries = 3
        delay_s = 0.25
        for attempt in range(max_retries):
            try:
                raw_response = await self._make_request(method, request_data)
                resp = self.decode_rpc_response(raw_response)
                if isinstance(resp, dict) and "id" not in resp:
                    resp["id"] = req.get("id")
                return resp
            except Exception as exc:
                status = getattr(exc, "status", None)
                if status in (502, 503, 504) and attempt < (max_retries - 1):
                    await asyncio.sleep(delay_s * (2**attempt))
                    continue
                raise


def _get_gorlami_base_url_safe() -> str | None:
    try:
        return get_gorlami_base_url().rstrip("/")
    except Exception:
        return None


def _is_gorlami_fork_rpc(rpc: str) -> bool:
    base = _get_gorlami_base_url_safe()
    if not base:
        return False
    return rpc.startswith(f"{base}/fork/")


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
    if _is_gorlami_fork_rpc(rpc):
        headers = AsyncHTTPProvider.get_request_headers()
        api_key = (get_gorlami_api_key() or "").strip()
        if api_key:
            headers["Authorization"] = api_key
        provider = _GorlamiProvider(rpc, request_kwargs={"headers": headers})
        web3 = AsyncWeb3(provider)
    else:
        web3 = AsyncWeb3(AsyncHTTPProvider(rpc))
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
