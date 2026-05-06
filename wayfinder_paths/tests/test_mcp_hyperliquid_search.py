from __future__ import annotations

import pytest

from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_search_market

# Snapshot tests against live HL. The HIP-4 outcome's expiry date + targetPrice
# rotate daily, so we only pin the stable prefix (up to `expiry:`) and suffix.
_BTC_OUTCOME_PREFIX = "class:priceBinary|underlying:BTC|expiry:"
_BTC_OUTCOME_SUFFIX = "|period:1d"


@pytest.mark.asyncio
async def test_search_bitcoin():
    res = await hyperliquid_search_market("bitcoin", limit=5)
    assert res["ok"]
    result = res["result"]

    assert result["perps"] == [
        {"name": "BTC-USDC"},
        {"name": "flx:BTC"},
        {"name": "hyna:BTC"},
        {"name": "cash:BTC"},
    ]
    assert result["spots"] == [
        {"name": "UBTC/USDC"},
        {"name": "UBTC/USDH"},
    ]
    assert [r["name"] for r in result["outcomes"]] == ["#40", "#41"]
    assert all(
        r["description"].startswith(_BTC_OUTCOME_PREFIX)
        and r["description"].endswith(_BTC_OUTCOME_SUFFIX)
        for r in result["outcomes"]
    )


@pytest.mark.asyncio
async def test_search_nvidia():
    res = await hyperliquid_search_market("nvidia", limit=5)
    assert res["ok"]
    result = res["result"]

    assert result["perps"] == [
        {"name": "xyz:NVDA"},
        {"name": "flx:NVDA"},
        {"name": "km:NVDA"},
        {"name": "cash:NVDA"},
    ]
    assert result["spots"] == []
    assert result["outcomes"] == []


@pytest.mark.asyncio
async def test_search_oil_futures():
    res = await hyperliquid_search_market("oil futures", limit=10)
    assert res["ok"]
    result = res["result"]

    assert result["perps"] == [
        {"name": "GAS-USDC"},
        {"name": "xyz:NATGAS"},
        {"name": "xyz:BRENTOIL"},
        {"name": "flx:OIL"},
        {"name": "flx:GAS"},
        {"name": "vntl:ENERGY"},
        {"name": "km:USOIL"},
        {"name": "cash:WTI"},
    ]
    assert result["spots"] == []
    assert result["outcomes"] == []
