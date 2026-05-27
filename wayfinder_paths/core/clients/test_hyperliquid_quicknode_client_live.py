"""Live network tests for HyperliquidQuicknodeInfoClient. Hits
vault-backend QuickNode proxy — no mocks.

Set RUN_HYPERLIQUID_LIVE_TESTS=1 to enable. Requires WAYFINDER_API_KEY
in env (or config.json).
"""

from __future__ import annotations

import os

import pytest

from wayfinder_paths.core.clients.HyperliquidQuicknodeInfoClient import (
    QUICKNODE_PROXIED_TYPES,
    HyperliquidQuicknodeInfoClient,
)
from wayfinder_paths.core.config import get_api_key

if os.getenv("RUN_HYPERLIQUID_LIVE_TESTS", "").lower() not in ("1", "true", "yes"):
    pytest.skip(
        "Hyperliquid live tests are disabled (set RUN_HYPERLIQUID_LIVE_TESTS=1 to enable).",
        allow_module_level=True,
    )

TEST_USER = "0xf9a9a403b039996082049394935f815523157330"
TEST_BUILDER = "0xaa1d89f333857ed78f8434cc4f896a9293efe65c"

needs_api_key = pytest.mark.skipif(
    not get_api_key(),
    reason="WAYFINDER_API_KEY not configured — QuickNode-proxied path requires backend auth",
)


@pytest.fixture
def client() -> HyperliquidQuicknodeInfoClient:
    return HyperliquidQuicknodeInfoClient()


def test_whitelist_covers_critical_methods() -> None:
    for method in (
        "activeAssetData",
        "clearinghouseState",
        "frontendOpenOrders",
        "maxBuilderFee",
        "meta",
        "openOrders",
        "outcomeMeta",
        "perpDexs",
        "spotClearinghouseState",
        "spotMeta",
    ):
        assert method in QUICKNODE_PROXIED_TYPES


@needs_api_key
@pytest.mark.asyncio
async def test_quicknode_clearinghouse_state(
    client: HyperliquidQuicknodeInfoClient,
) -> None:
    r = await client.post({"type": "clearinghouseState", "user": TEST_USER})
    assert "marginSummary" in r
    assert "assetPositions" in r


@needs_api_key
@pytest.mark.asyncio
async def test_quicknode_spot_clearinghouse_state(
    client: HyperliquidQuicknodeInfoClient,
) -> None:
    r = await client.post({"type": "spotClearinghouseState", "user": TEST_USER})
    assert "balances" in r
    assert isinstance(r["balances"], list)


@needs_api_key
@pytest.mark.asyncio
async def test_quicknode_meta(client: HyperliquidQuicknodeInfoClient) -> None:
    r = await client.post({"type": "meta"})
    assert "universe" in r
    assert len(r["universe"]) > 50


@needs_api_key
@pytest.mark.asyncio
async def test_quicknode_spot_meta(client: HyperliquidQuicknodeInfoClient) -> None:
    r = await client.post({"type": "spotMeta"})
    assert "tokens" in r and "universe" in r


@needs_api_key
@pytest.mark.asyncio
async def test_quicknode_open_orders(client: HyperliquidQuicknodeInfoClient) -> None:
    r = await client.post({"type": "openOrders", "user": TEST_USER})
    assert isinstance(r, list)


@needs_api_key
@pytest.mark.asyncio
async def test_quicknode_perp_dexes(client: HyperliquidQuicknodeInfoClient) -> None:
    r = await client.post({"type": "perpDexs"})
    assert isinstance(r, list)
    assert any(d and d["name"] == "xyz" for d in r if d is not None)


@needs_api_key
@pytest.mark.asyncio
async def test_quicknode_max_builder_fee(
    client: HyperliquidQuicknodeInfoClient,
) -> None:
    r = await client.post(
        {"type": "maxBuilderFee", "user": TEST_USER, "builder": TEST_BUILDER}
    )
    assert isinstance(r, int)
    assert r >= 0


@needs_api_key
@pytest.mark.asyncio
async def test_quicknode_frontend_open_orders(
    client: HyperliquidQuicknodeInfoClient,
) -> None:
    r = await client.post({"type": "frontendOpenOrders", "user": TEST_USER})
    assert isinstance(r, list)


@needs_api_key
@pytest.mark.asyncio
async def test_quicknode_portfolio_state(
    client: HyperliquidQuicknodeInfoClient,
) -> None:
    r = await client.portfolio_state(TEST_USER)
    assert "clearinghouseState" in r
    assert "spotClearinghouseState" in r
    assert "userAbstraction" in r
    assert "assetPositions" in r["clearinghouseState"]
    assert "balances" in r["spotClearinghouseState"]
