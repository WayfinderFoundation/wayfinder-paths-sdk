"""Read-only contract/API regression checks run by the check-in integration job.

No signer, approvals or sends. Provider/schema/deployment failures fail loudly.
"""

import asyncio
from copy import deepcopy

import pytest

from wayfinder_paths.adapters.aave_v4_adapter.adapter import AaveV4Adapter
from wayfinder_paths.adapters.uniswap_adapter.adapter import UniswapAdapter
from wayfinder_paths.core.clients.MorphoClient import MorphoClient
from wayfinder_paths.core.config import get_rpc_urls, set_rpc_urls
from wayfinder_paths.core.constants.aave_v4_contracts import (
    ARC_AAVE_V4_HUB,
    ARC_AAVE_V4_SPOKES,
    ARC_AAVE_V4_VAULTS,
)
from wayfinder_paths.core.constants.contracts import (
    MULTICALL3_ADDRESS,
    UNISWAP_PERMIT2,
    UNISWAP_V4_POOL_MANAGER,
    UNISWAP_V4_POSITION_MANAGER,
    UNISWAP_V4_QUOTER,
    UNISWAP_V4_STATE_VIEW,
    UNISWAP_V4_UNIVERSAL_ROUTER,
)
from wayfinder_paths.core.constants.morpho_contracts import MORPHO_BY_CHAIN
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

pytestmark = [pytest.mark.local, pytest.mark.asyncio, pytest.mark.timeout(90)]
CHAIN = 5042
USDC = "0x3600000000000000000000000000000000000000"
EURC = "0xbEf5f6d51CB62b58e6A8f77868681825C6fe21c1"
READER = "0x1111111111111111111111111111111111111111"


@pytest.fixture(autouse=True)
def public_arc_reads():
    previous = deepcopy(get_rpc_urls())
    set_rpc_urls({**previous, str(CHAIN): "https://rpc.mainnet.arc.io"})
    try:
        yield
    finally:
        set_rpc_urls(previous)


async def test_arc_deployments_exist():
    contracts = [
        MULTICALL3_ADDRESS,
        UNISWAP_PERMIT2,
        UNISWAP_V4_POOL_MANAGER[CHAIN],
        UNISWAP_V4_POSITION_MANAGER[CHAIN],
        UNISWAP_V4_QUOTER[CHAIN],
        UNISWAP_V4_STATE_VIEW[CHAIN],
        UNISWAP_V4_UNIVERSAL_ROUTER[CHAIN],
        MORPHO_BY_CHAIN[CHAIN]["morpho"],
        ARC_AAVE_V4_HUB,
        *ARC_AAVE_V4_SPOKES.values(),
        *ARC_AAVE_V4_VAULTS.values(),
    ]
    async with web3_from_chain_id(CHAIN) as web3:
        assert await web3.eth.chain_id == CHAIN
        for address in contracts:
            assert await web3.eth.get_code(web3.to_checksum_address(address)), address


async def test_arc_aave_v4_spoke_abi_and_reserves():
    adapter = AaveV4Adapter()
    async with asyncio.timeout(60):
        for spoke, count in (("main", 4), ("forex", 2)):
            ok, markets = await adapter.get_markets(spoke=spoke)
            assert ok, markets
            assert len(markets) >= count
            usdc = next(r for r in markets if r["underlying"].lower() == USDC.lower())
            assert usdc["decimals"] == 6
            assert usdc["hub"].lower() == ARC_AAVE_V4_HUB.lower()
            ok, state = await adapter.get_user_state(spoke=spoke, account=READER)
            assert ok, state
            assert len(state["positions"]) == len(markets)


async def test_arc_uniswap_v4_live_quote():
    adapter = UniswapAdapter({"chain_id": CHAIN}, wallet_address=READER)
    async with asyncio.timeout(60):
        ok, quote = await adapter.v4_quote(USDC, EURC, 10 * 10**6)
        assert ok, quote
        assert quote["amount_out"] > 0


async def test_arc_morpho_live_discovery_without_allocator():
    async with MorphoClient() as client, asyncio.timeout(60):
        deployments = await client.get_morpho_by_chain()
        assert (
            deployments[CHAIN]["morpho"].lower()
            == MORPHO_BY_CHAIN[CHAIN]["morpho"].lower()
        )
        markets = await client.get_all_markets(chain_id=CHAIN, max_pages=2)
        assert any(m["loanAsset"]["address"].lower() == USDC.lower() for m in markets)
