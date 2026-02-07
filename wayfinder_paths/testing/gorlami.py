from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from loguru import logger
from web3 import AsyncHTTPProvider, AsyncWeb3

from wayfinder_paths.core.clients.GorlamiTestnetClient import GorlamiTestnetClient
from wayfinder_paths.core.config import get_gorlami_api_key


@pytest.fixture
async def gorlami():
    forks = {}
    client = GorlamiTestnetClient()
    client.forks = forks  # Expose forks dict for tests

    @asynccontextmanager
    async def patched_web3_from_chain_id(chain_id: int):
        api_key = (get_gorlami_api_key() or "").strip()
        key = str(chain_id)
        if key not in forks:
            fork = await client.create_fork(chain_id)
            forks[key] = fork
            logger.info(f"[gorlami] Created fork {fork['fork_id']} for chain {chain_id}")

        class _GorlamiProvider(AsyncHTTPProvider):
            async def make_request(self, method, params):  # type: ignore[override]
                req = self.form_request(method, params)
                request_data = self.encode_rpc_dict(req)
                raw_response = await self._make_request(method, request_data)
                resp = self.decode_rpc_response(raw_response)
                if isinstance(resp, dict) and "id" not in resp:
                    resp["id"] = req.get("id")
                return resp

        headers = AsyncHTTPProvider.get_request_headers()
        if api_key:
            headers["Authorization"] = api_key
        provider = _GorlamiProvider(
            forks[key]["rpc_url"], request_kwargs={"headers": headers}
        )
        web3 = AsyncWeb3(provider)
        try:
            yield web3
        finally:
            await web3.provider.disconnect()

    @asynccontextmanager
    async def patched_web3s_from_chain_id(chain_id: int):
        async with patched_web3_from_chain_id(chain_id) as web3:
            yield [web3]

    with (
        patch("wayfinder_paths.core.utils.web3.web3_from_chain_id", patched_web3_from_chain_id),
        patch("wayfinder_paths.core.utils.web3.web3s_from_chain_id", patched_web3s_from_chain_id),
    ):
        yield client

    for fork in forks.values():
        try:
            await client.delete_fork(fork["fork_id"])
            logger.info(f"[gorlami] Deleted fork {fork['fork_id']}")
        except Exception as e:
            logger.warning(f"[gorlami] Failed to delete fork: {e}")
    await client.close()
