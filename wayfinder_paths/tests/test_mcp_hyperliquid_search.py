from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_search_markets


class _FakeHyperliquidAdapter:
    async def get_meta_and_asset_ctxs(self):
        return True, [
            {
                "universe": [
                    {"name": "BTC", "maxLeverage": 40, "szDecimals": 5},
                    {"name": "kBONK", "maxLeverage": 5, "szDecimals": 0},
                    {"name": "uSOL", "maxLeverage": 10, "szDecimals": 2},
                    {"name": "MON", "maxLeverage": 5, "szDecimals": 0},
                    {"name": "km:AAPL", "maxLeverage": 15, "szDecimals": 3},
                    {"name": "km:NVDA", "maxLeverage": 12, "szDecimals": 3},
                    {"name": "cash:WTI", "maxLeverage": 10, "szDecimals": 2},
                    {"name": "xyz:BRENTOIL", "maxLeverage": 20, "szDecimals": 2},
                    {"name": "flx:OIL", "maxLeverage": 15, "szDecimals": 3},
                    {"name": "xyz:NATGAS", "maxLeverage": 10, "szDecimals": 1},
                    {"name": "vntl:ENERGY", "maxLeverage": 20, "szDecimals": 2},
                    {"name": "PURR", "maxLeverage": 3, "szDecimals": 0},
                ]
            },
            [],
        ]

    async def get_spot_assets(self):
        return True, {
            "HYPE/USDC": 10107,
            "UBTC/USDC": 10001,
            "UETH/USDC": 10002,
            "PURR/USDC": 10000,
        }


async def _search(query: str, **kwargs):
    with patch(
        "wayfinder_paths.mcp.tools.hyperliquid.HyperliquidAdapter",
        _FakeHyperliquidAdapter,
    ):
        out = await hyperliquid_search_markets(query, **kwargs)
    return json.loads(out)


@pytest.mark.asyncio
async def test_hyperliquid_search_markets_is_high_recall_for_oil():
    result = await _search("oil futures", market_type="perp", limit=20)

    names = {row["name"] for row in result["matches"]}
    assert {"cash:WTI", "xyz:BRENTOIL", "flx:OIL"}.issubset(names)
    assert {"xyz:NATGAS", "vntl:ENERGY"}.issubset(names)
    assert result["searched_counts"] == {"perp": 12, "spot": 0}


@pytest.mark.asyncio
async def test_hyperliquid_search_markets_handles_hl_wrappers():
    bonk = await _search("bonk", market_type="perp", limit=5)
    sol = await _search("solana", market_type="perp", limit=5)

    assert bonk["matches"][0]["name"] == "kBONK"
    assert bonk["matches"][0]["confidence"] == "high"
    assert sol["matches"][0]["name"] == "uSOL"
    assert sol["matches"][0]["confidence"] == "high"


@pytest.mark.asyncio
async def test_hyperliquid_search_markets_handles_asset_name_aliases():
    monad = await _search("monad", market_type="perp", limit=5)
    nvidia = await _search("nvidia", market_type="perp", limit=5)

    assert monad["matches"][0]["name"] == "MON"
    for row in monad["matches"]:
        assert "prefix:kmonad" not in row["match_reasons"]
    assert nvidia["matches"][0]["name"] == "km:NVDA"
    assert nvidia["matches"][0]["confidence"] == "high"


@pytest.mark.asyncio
async def test_hyperliquid_search_markets_filters_spot_and_perp():
    spot = await _search("hype", market_type="spot", limit=10)
    perp = await _search("hype", market_type="perp", limit=10)

    assert all(row["type"] == "spot" for row in spot["matches"])
    assert spot["matches"][0]["name"] == "HYPE/USDC"
    assert spot["searched_counts"] == {"perp": 0, "spot": 4}
    assert all(row["type"] == "perp" for row in perp["matches"])
    assert perp["searched_counts"] == {"perp": 12, "spot": 0}


@pytest.mark.asyncio
async def test_hyperliquid_search_markets_caps_output_but_reports_total_candidates():
    result = await _search("oil", market_type="both", limit=3)

    assert result["count"] == 3
    assert len(result["matches"]) == 3
    assert result["total_candidates"] > result["count"]
    assert result["matches"][0]["score"] >= result["matches"][1]["score"]
