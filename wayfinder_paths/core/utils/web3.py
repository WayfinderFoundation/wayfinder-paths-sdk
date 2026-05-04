import logging
from contextlib import asynccontextmanager

import httpx
from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.middleware import ExtraDataToPOAMiddleware
from web3.module import Module

from wayfinder_paths.core.config import (
    get_api_base_url,
    get_api_key,
    get_rpc_urls,
)
from wayfinder_paths.core.constants.chains import (
    CHAIN_ID_HYPEREVM,
    POA_MIDDLEWARE_CHAIN_IDS,
)
from wayfinder_paths.core.utils.retry import retry_async

logger = logging.getLogger(__name__)


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
        # It can also intermittently return 429/502/503/504, so retry a bit.
        req = self.form_request(method, params)
        request_data = self.encode_rpc_dict(req)

        async def _attempt():
            raw_response = await self._make_request(method, request_data)
            resp = self.decode_rpc_response(raw_response)
            if isinstance(resp, dict) and "id" not in resp:
                resp["id"] = req.get("id")
            return resp

        def _should_retry(exc: Exception) -> bool:
            return getattr(exc, "status", None) in (429, 502, 503, 504)

        return await retry_async(
            _attempt,
            max_retries=3,
            base_delay_s=0.25,
            should_retry=_should_retry,
        )


def _is_wayfinder_rpc(rpc: str) -> bool:
    return rpc.startswith(get_api_base_url())


_GORLAMI_FORK_PREFIX = "/blockchain/gorlami/fork/"


def _is_gorlami_fork_rpc(rpc: str) -> bool:
    base = get_api_base_url().rstrip("/")
    return rpc.startswith(f"{base}{_GORLAMI_FORK_PREFIX}")


def _wayfinder_auth_headers() -> dict[str, str]:
    headers = AsyncHTTPProvider.get_request_headers()
    api_key = get_api_key()
    if api_key:
        headers = {**headers, "X-API-KEY": api_key}
    return headers


_pool_size_cache: dict[int, int] = {}


def _fetch_pool_size(chain_id: int) -> int:
    """Probes the proxy for its fan-out width; positive results cached per process.

    Synchronous, so the first call per chain blocks the asyncio loop for one
    HTTPS round-trip (~50-200ms). Failures are NOT cached so a transient
    network blip on the very first call doesn't pin the SDK to single-URL
    fallback for the rest of the process lifetime.
    """
    if (cached := _pool_size_cache.get(chain_id)) is not None:
        return cached
    url = f"{get_api_base_url()}/blockchain/rpc/{chain_id}/count/"
    try:
        resp = httpx.get(url, headers=_wayfinder_auth_headers(), timeout=5)
        size = int(resp.json()["size"])
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.warning("RPC pool-size probe failed for chain %s: %s", chain_id, exc)
        return 0
    _pool_size_cache[chain_id] = size
    return size


def _get_rpcs_for_chain_id(chain_id: int) -> list:
    mapping = get_rpc_urls()
    rpcs = mapping.get(str(chain_id))
    if rpcs is None:
        # User overrides
        rpcs = mapping.get(chain_id)
    if rpcs is None:
        base = get_api_base_url()
        n = _fetch_pool_size(chain_id)
        if n:
            rpcs = [f"{base}/blockchain/rpc/{chain_id}/{i}/" for i in range(n)]
        else:
            rpcs = [f"{base}/blockchain/rpc/{chain_id}/"]

    if isinstance(rpcs, str):
        return [rpcs]
    return rpcs


def _get_web3(rpc: str, chain_id: int) -> AsyncWeb3:
    if _is_gorlami_fork_rpc(rpc):
        provider = _GorlamiProvider(
            rpc,
            request_kwargs={"headers": _wayfinder_auth_headers()},
        )
        web3 = AsyncWeb3(provider)
    elif _is_wayfinder_rpc(rpc):
        web3 = AsyncWeb3(
            AsyncHTTPProvider(
                rpc, request_kwargs={"headers": _wayfinder_auth_headers()}
            )
        )
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
