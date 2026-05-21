from __future__ import annotations

import copy
import os
from collections.abc import Iterator

import pytest

from wayfinder_paths.adapters.projectx_adapter.adapter import ProjectXLiquidityAdapter
from wayfinder_paths.core.config import CONFIG, set_config
from wayfinder_paths.core.constants.contracts import ZERO_ADDRESS
from wayfinder_paths.core.constants.projectx import (
    PROJECTX_CHAIN_ID,
    THBILL_TOKEN,
    THBILL_USDC_POOL,
    THBILL_USDT0_POOL,
    USDC_TOKEN,
    USDT0_TOKEN,
    WHYPE_TOKEN,
)

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.skipif(
        os.getenv("PROJECTX_HYPEREVM_SMOKE") != "1",
        reason="set PROJECTX_HYPEREVM_SMOKE=1 to run live HyperEVM ProjectX smoke",
    ),
]

PUBLIC_HYPEREVM_RPC = "https://rpc.hyperliquid.xyz/evm"


@pytest.fixture(autouse=True)
def public_hyperevm_rpc() -> Iterator[None]:
    before = copy.deepcopy(CONFIG)
    next_config = copy.deepcopy(CONFIG)
    strategy = dict(next_config.get("strategy") or {})
    rpc_urls = dict(strategy.get("rpc_urls") or {})
    rpc_urls[str(PROJECTX_CHAIN_ID)] = [PUBLIC_HYPEREVM_RPC]
    strategy["rpc_urls"] = rpc_urls
    next_config["strategy"] = strategy
    set_config(next_config)
    try:
        yield
    finally:
        set_config(before)


@pytest.mark.asyncio
async def test_projectx_hyperevm_read_only_smoke():
    adapter = ProjectXLiquidityAdapter(
        {"pool_address": THBILL_USDC_POOL},
        wallet_address=ZERO_ADDRESS,
    )

    ok_overview, overview = await adapter.pool_overview()
    assert ok_overview, overview
    assert overview["fee"] == 100
    assert overview["tick_spacing"] == 1
    assert overview["token0"]["address"] == USDC_TOKEN
    assert overview["token1"]["address"] == THBILL_TOKEN
    assert int(overview["liquidity"]) > 0

    ok_whype, whype_pool = await adapter.find_pool_for_pair(
        WHYPE_TOKEN,
        USDC_TOKEN,
        prefer_fees=[500],
    )
    assert ok_whype, whype_pool
    assert whype_pool["fee"] == 500

    ok_usdt0, usdt0_pool = await adapter.find_pool_for_pair(
        USDT0_TOKEN,
        THBILL_TOKEN,
        prefer_fees=[100],
    )
    assert ok_usdt0, usdt0_pool
    assert usdt0_pool["pool"] == THBILL_USDT0_POOL

    ok_points, points = await ProjectXLiquidityAdapter.fetch_prjx_points(ZERO_ADDRESS)
    assert ok_points, points
    assert isinstance(points, dict)
    assert "pointsTotal" in points or points.get("points") == 0
