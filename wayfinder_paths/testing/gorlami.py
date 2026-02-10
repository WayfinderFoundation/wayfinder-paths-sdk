from contextlib import asynccontextmanager
from copy import deepcopy
from unittest.mock import patch

import pytest
from loguru import logger

from wayfinder_paths.core.clients.GorlamiTestnetClient import GorlamiTestnetClient
from wayfinder_paths.core.config import get_rpc_urls, set_rpc_urls
from wayfinder_paths.core.utils import web3 as web3_utils


@pytest.fixture
async def gorlami():
    forks = {}
    client = GorlamiTestnetClient()
    client.forks = forks  # Expose forks dict for tests

    old_rpc_urls = deepcopy(get_rpc_urls())
    _real_web3_from_chain_id = web3_utils.web3_from_chain_id
    _real_web3s_from_chain_id = web3_utils.web3s_from_chain_id

    def _apply_rpc_overrides() -> None:
        new_rpc_urls = deepcopy(old_rpc_urls)
        for chain_key, fork in forks.items():
            new_rpc_urls[str(chain_key)] = fork["rpc_url"]
        set_rpc_urls(new_rpc_urls)

    async def _ensure_fork(chain_id: int) -> None:
        key = str(chain_id)
        if key in forks:
            return
        fork = await client.create_fork(chain_id)
        forks[key] = fork
        logger.info(f"[gorlami] Created fork {fork['fork_id']} for chain {chain_id}")
        _apply_rpc_overrides()

    @asynccontextmanager
    async def patched_web3_from_chain_id(chain_id: int):
        await _ensure_fork(chain_id)

        async with _real_web3_from_chain_id(chain_id) as web3:
            yield web3

    @asynccontextmanager
    async def patched_web3s_from_chain_id(chain_id: int):
        await _ensure_fork(chain_id)

        async with _real_web3s_from_chain_id(chain_id) as web3s:
            yield web3s

    try:
        with (
            patch(
                "wayfinder_paths.core.utils.web3.web3_from_chain_id",
                patched_web3_from_chain_id,
            ),
            patch(
                "wayfinder_paths.core.utils.web3.web3s_from_chain_id",
                patched_web3s_from_chain_id,
            ),
        ):
            yield client
    finally:
        try:
            set_rpc_urls(old_rpc_urls)
        except Exception:
            pass

        for fork in forks.values():
            try:
                await client.delete_fork(fork["fork_id"])
                logger.info(f"[gorlami] Deleted fork {fork['fork_id']}")
            except Exception as e:
                logger.warning(f"[gorlami] Failed to delete fork: {e}")
        await client.close()
