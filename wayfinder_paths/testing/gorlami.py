from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from loguru import logger

from wayfinder_paths.core.clients.GorlamiTestnetClient import GorlamiTestnetClient
from wayfinder_paths.core.utils.web3 import _get_web3


@pytest.fixture
async def gorlami():
    forks = {}
    client = GorlamiTestnetClient()
    client.forks = forks  # Expose forks dict for tests

    @asynccontextmanager
    async def patched_web3_from_chain_id(chain_id: int):
        key = str(chain_id)
        if key not in forks:
            fork = await client.create_fork(chain_id)
            forks[key] = fork
            logger.info(f"[gorlami] Created fork {fork['fork_id']} for chain {chain_id}")
        web3 = _get_web3(forks[key]["rpc_url"], chain_id)
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
