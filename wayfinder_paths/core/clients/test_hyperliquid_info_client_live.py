"""Live network tests for HyperliquidInfoClient. Hits real Hyperliquid
public + vault-backend QN proxy — no mocks.

Set RUN_HYPERLIQUID_LIVE_TESTS=1 to enable. Tests against whatever
`get_api_base_url()` resolves to (default: prod). Override via config.json
or WAYFINDER_CONFIG_PATH if you want dev.

QN-proxied tests require WAYFINDER_API_KEY in env (or config.json) — they're
skipped if missing. Public-direct tests run unconditionally once the module
is enabled.
"""

from __future__ import annotations

import os
import time

import pytest

from wayfinder_paths.core.clients.HyperliquidInfoClient import (
    QN_PROXIED_TYPES,
    HyperliquidInfoClient,
)
from wayfinder_paths.core.config import get_api_key

if os.getenv("RUN_HYPERLIQUID_LIVE_TESTS", "").lower() not in ("1", "true", "yes"):
    pytest.skip(
        "Hyperliquid live tests are disabled (set RUN_HYPERLIQUID_LIVE_TESTS=1 to enable).",
        allow_module_level=True,
    )

# Active HL wallet (from QN's own docs examples) — has perp + spot state
# across multiple dexes, so positive-data assertions are stable.
TEST_USER = "0xf9a9a403b039996082049394935f815523157330"
TEST_BUILDER = "0xaa1d89f333857ed78f8434cc4f896a9293efe65c"

needs_api_key = pytest.mark.skipif(
    not get_api_key(),
    reason="WAYFINDER_API_KEY not configured — QN-proxied path requires backend auth",
)


@pytest.fixture
def client() -> HyperliquidInfoClient:
    # Function-scoped — httpx.AsyncClient must be built inside the test's
    # event loop, not at module import.
    return HyperliquidInfoClient()


# ── Whitelist sanity ──────────────────────────────────────────────────────


def test_whitelist_covers_critical_methods() -> None:
    for method in (
        "clearinghouseState",
        "spotClearinghouseState",
        "frontendOpenOrders",
        "maxBuilderFee",
        "meta",
        "openOrders",
        "perpDexs",
        "spotMeta",
    ):
        assert method in QN_PROXIED_TYPES


# ── QN-proxied path (backend → QuickNode) ─────────────────────────────────


@needs_api_key
@pytest.mark.asyncio
async def test_qn_clearinghouse_state(client: HyperliquidInfoClient) -> None:
    r = await client.post({"type": "clearinghouseState", "user": TEST_USER})
    assert "marginSummary" in r
    assert "assetPositions" in r


@needs_api_key
@pytest.mark.asyncio
async def test_qn_spot_clearinghouse_state(client: HyperliquidInfoClient) -> None:
    r = await client.post({"type": "spotClearinghouseState", "user": TEST_USER})
    assert "balances" in r
    assert isinstance(r["balances"], list)


@needs_api_key
@pytest.mark.asyncio
async def test_qn_meta(client: HyperliquidInfoClient) -> None:
    r = await client.post({"type": "meta"})
    assert "universe" in r
    assert len(r["universe"]) > 50


@needs_api_key
@pytest.mark.asyncio
async def test_qn_spot_meta(client: HyperliquidInfoClient) -> None:
    r = await client.post({"type": "spotMeta"})
    assert "tokens" in r and "universe" in r


@needs_api_key
@pytest.mark.asyncio
async def test_qn_open_orders(client: HyperliquidInfoClient) -> None:
    r = await client.post({"type": "openOrders", "user": TEST_USER})
    assert isinstance(r, list)


@needs_api_key
@pytest.mark.asyncio
async def test_qn_perp_dexes(client: HyperliquidInfoClient) -> None:
    r = await client.post({"type": "perpDexs"})
    assert isinstance(r, list)
    assert any(d and d["name"] == "xyz" for d in r if d is not None)


@needs_api_key
@pytest.mark.asyncio
async def test_qn_max_builder_fee(client: HyperliquidInfoClient) -> None:
    r = await client.post(
        {"type": "maxBuilderFee", "user": TEST_USER, "builder": TEST_BUILDER}
    )
    assert isinstance(r, int)
    assert r >= 0


@needs_api_key
@pytest.mark.asyncio
async def test_qn_frontend_open_orders(client: HyperliquidInfoClient) -> None:
    r = await client.post({"type": "frontendOpenOrders", "user": TEST_USER})
    assert isinstance(r, list)


# ── Public-direct path (SDK Info → api.hyperliquid.xyz) ──────────────────


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
async def test_public_meta_and_asset_ctxs(client: HyperliquidInfoClient) -> None:
    r = await client.post({"type": "metaAndAssetCtxs"})
    assert isinstance(r, list) and len(r) == 2
    meta, ctxs = r
    assert "universe" in meta and isinstance(ctxs, list)


@pytest.mark.asyncio
async def test_public_spot_meta_and_asset_ctxs(client: HyperliquidInfoClient) -> None:
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
async def test_public_funding_history(client: HyperliquidInfoClient) -> None:
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
async def test_public_historical_orders(client: HyperliquidInfoClient) -> None:
    r = await client.post({"type": "historicalOrders", "user": TEST_USER})
    assert isinstance(r, list)


@pytest.mark.asyncio
async def test_public_user_fills_by_time(client: HyperliquidInfoClient) -> None:
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


# Adapter-call-site coverage — public-only HL types the adapter hits today.


@pytest.mark.asyncio
async def test_public_all_perp_metas(client: HyperliquidInfoClient) -> None:
    r = await client.post({"type": "allPerpMetas"})
    assert isinstance(r, list)
    assert len(r) >= 1
    assert "universe" in r[0]


@pytest.mark.asyncio
async def test_public_active_asset_data(client: HyperliquidInfoClient) -> None:
    r = await client.post({"type": "activeAssetData", "user": TEST_USER, "coin": "BTC"})
    assert isinstance(r, dict)
    assert "leverage" in r or "markPx" in r


@pytest.mark.asyncio
async def test_public_user_abstraction(client: HyperliquidInfoClient) -> None:
    r = await client.post({"type": "userAbstraction", "user": TEST_USER})
    assert r in {"default", "unifiedAccount", "portfolioMargin", "dexAbstraction"}


@pytest.mark.asyncio
async def test_public_margin_table(client: HyperliquidInfoClient) -> None:
    # HL margin table 1 is the default cross-margin schedule — always present.
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
