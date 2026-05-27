"""Live network tests for HyperliquidInfoClient (public HL path).
Hits api.hyperliquid.xyz directly — no auth needed.

Set RUN_HYPERLIQUID_LIVE_TESTS=1 to enable.
"""

from __future__ import annotations

import os
import time

import pytest

from wayfinder_paths.core.clients.HyperliquidInfoClient import HyperliquidInfoClient

if os.getenv("RUN_HYPERLIQUID_LIVE_TESTS", "").lower() not in ("1", "true", "yes"):
    pytest.skip(
        "Hyperliquid live tests are disabled (set RUN_HYPERLIQUID_LIVE_TESTS=1 to enable).",
        allow_module_level=True,
    )

TEST_USER = "0xf9a9a403b039996082049394935f815523157330"


@pytest.fixture
def client() -> HyperliquidInfoClient:
    return HyperliquidInfoClient()


@pytest.mark.asyncio
async def test_public_all_mids(client: HyperliquidInfoClient) -> None:
    r = await client.post({"type": "allMids"})
    assert isinstance(r, dict)
    assert "BTC" in r
    assert float(r["BTC"]) > 0


@pytest.mark.asyncio
async def test_public_l2_book(client: HyperliquidInfoClient) -> None:
    r = await client.post({"type": "l2Book", "coin": "BTC"})
    assert "levels" in r and len(r["levels"]) == 2


@pytest.mark.asyncio
async def test_public_meta_and_asset_ctxs(
    client: HyperliquidInfoClient,
) -> None:
    r = await client.post({"type": "metaAndAssetCtxs"})
    assert isinstance(r, list) and len(r) == 2
    meta, ctxs = r
    assert "universe" in meta and isinstance(ctxs, list)


@pytest.mark.asyncio
async def test_public_spot_meta_and_asset_ctxs(
    client: HyperliquidInfoClient,
) -> None:
    r = await client.post({"type": "spotMetaAndAssetCtxs"})
    assert isinstance(r, list) and len(r) == 2


@pytest.mark.asyncio
async def test_public_user_fills(client: HyperliquidInfoClient) -> None:
    r = await client.post({"type": "userFills", "user": TEST_USER})
    assert isinstance(r, list)
    if r:
        assert "coin" in r[0] and "px" in r[0]


@pytest.mark.asyncio
async def test_public_candle_snapshot(client: HyperliquidInfoClient) -> None:
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - 3 * 60 * 60 * 1000
    r = await client.post(
        {
            "type": "candleSnapshot",
            "req": {
                "coin": "BTC",
                "interval": "1h",
                "startTime": start_ms,
                "endTime": end_ms,
            },
        }
    )
    assert isinstance(r, list)
    assert len(r) >= 2


@pytest.mark.asyncio
async def test_public_funding_history(
    client: HyperliquidInfoClient,
) -> None:
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - 24 * 60 * 60 * 1000
    r = await client.post(
        {
            "type": "fundingHistory",
            "coin": "BTC",
            "startTime": start_ms,
            "endTime": end_ms,
        }
    )
    assert isinstance(r, list)


@pytest.mark.asyncio
async def test_public_historical_orders(
    client: HyperliquidInfoClient,
) -> None:
    r = await client.post({"type": "historicalOrders", "user": TEST_USER})
    assert isinstance(r, list)


@pytest.mark.asyncio
async def test_public_user_fills_by_time(
    client: HyperliquidInfoClient,
) -> None:
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - 24 * 60 * 60 * 1000
    r = await client.post(
        {
            "type": "userFillsByTime",
            "user": TEST_USER,
            "startTime": start_ms,
            "endTime": end_ms,
            "aggregateByTime": False,
        }
    )
    assert isinstance(r, list)


@pytest.mark.asyncio
async def test_public_all_perp_metas(
    client: HyperliquidInfoClient,
) -> None:
    r = await client.post({"type": "allPerpMetas"})
    assert isinstance(r, list)
    assert len(r) >= 1
    assert "universe" in r[0]


@pytest.mark.asyncio
async def test_public_active_asset_data(
    client: HyperliquidInfoClient,
) -> None:
    r = await client.post({"type": "activeAssetData", "user": TEST_USER, "coin": "BTC"})
    assert isinstance(r, dict)
    assert "leverage" in r or "markPx" in r


@pytest.mark.asyncio
async def test_public_user_abstraction(
    client: HyperliquidInfoClient,
) -> None:
    r = await client.post({"type": "userAbstraction", "user": TEST_USER})
    assert r in {"default", "unifiedAccount", "portfolioMargin", "dexAbstraction"}


@pytest.mark.asyncio
async def test_public_margin_table(client: HyperliquidInfoClient) -> None:
    r = await client.post({"type": "marginTable", "id": 1})
    assert isinstance(r, dict)
    assert "marginTiers" in r or "description" in r


@pytest.mark.asyncio
async def test_public_vault_details(client: HyperliquidInfoClient) -> None:
    HLP_VAULT = "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"
    r = await client.post({"type": "vaultDetails", "vaultAddress": HLP_VAULT})
    assert isinstance(r, dict)
    assert r["vaultAddress"].lower() == HLP_VAULT
    assert "portfolio" in r
