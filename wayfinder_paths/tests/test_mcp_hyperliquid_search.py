from __future__ import annotations

import pytest

from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_search_market

# Live HL tests — assertions check the expected set is a SUBSET of returned
# names so the suite stays green as HL adds/removes markets.


def _names(rows):
    return {row["name"] for row in rows}


@pytest.mark.asyncio
async def test_search_bitcoin():
    res = await hyperliquid_search_market("bitcoin", limit=10)
    assert res["ok"]
    result = res["result"]

    assert {"BTC-USDC", "flx:BTC", "hyna:BTC", "cash:BTC"} <= _names(result["perps"])
    assert {"UBTC/USDC", "UBTC/USDH"} <= _names(result["spots"])
    # HIP-4 outcome IDs rotate daily and span priceBinary/priceBucket
    # classes; presence + BTC-underlying marker is enough.
    assert result["outcomes"]
    assert all("underlying:BTC" in r["description"] for r in result["outcomes"])
    assert all(r["side"] in {"Yes", "No"} for r in result["outcomes"])


@pytest.mark.asyncio
async def test_search_nvidia():
    res = await hyperliquid_search_market("nvidia", limit=10)
    assert res["ok"]
    result = res["result"]

    assert {"xyz:NVDA", "flx:NVDA", "km:NVDA", "cash:NVDA"} <= _names(result["perps"])


@pytest.mark.asyncio
async def test_search_empty_query_returns_first_n_per_bucket():
    res = await hyperliquid_search_market("", limit=3)
    assert res["ok"]
    result = res["result"]

    for bucket in ("perps", "spots", "outcomes"):
        assert 0 < len(result[bucket]) <= 3, bucket


@pytest.mark.asyncio
async def test_search_kinetiq_resolves_to_kntq_spot():
    # No alias for kinetiq → kntq; the matches/min_len metric handles
    # vowel-stripped HL token symbols natively.
    res = await hyperliquid_search_market("kinetiq", limit=10)
    assert res["ok"]
    result = res["result"]

    assert {"KNTQ/USDH"} <= _names(result["spots"])


@pytest.mark.asyncio
async def test_search_oil_futures():
    res = await hyperliquid_search_market("oil futures", limit=20)
    assert res["ok"]
    result = res["result"]

    assert {
        "GAS-USDC",
        "xyz:NATGAS",
        "xyz:BRENTOIL",
        "flx:OIL",
        "vntl:ENERGY",
        "km:USOIL",
        "cash:WTI",
    } <= _names(result["perps"])
