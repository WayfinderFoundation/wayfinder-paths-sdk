from __future__ import annotations

import pytest

from wayfinder_paths.adapters.hyperliquid_adapter import HyperliquidAdapter
from wayfinder_paths.mcp.tools.hyperliquid import (
    hyperliquid_search_hip4,
    hyperliquid_search_market,
)

# Live HL tests — assertions check the expected set is a SUBSET of returned
# names so the suite stays green as HL adds/removes markets.


def _names(rows):
    return {row["name"] for row in rows}


def _named_side(asset_name: str, label: str) -> dict:
    return {"name": label, "asset_name": asset_name, "description": f"{asset_name}: {label}"}


def _world_cup_match_market() -> dict:
    long_description = "resolver text " * 80
    return {
        "class": "named",
        "name": "World Cup: Switzerland vs Canada",
        "description": long_description,
        "outcomes": [
            {
                "name": "Switzerland",
                "sides": [_named_side("#5260", "Yes"), _named_side("#5261", "No")],
            },
            {
                "name": "Draw",
                "sides": [_named_side("#5270", "Yes"), _named_side("#5271", "No")],
            },
            {
                "name": "Canada",
                "sides": [_named_side("#5280", "Yes"), _named_side("#5281", "No")],
            },
        ],
    }


def _world_cup_champion_market() -> dict:
    countries = [
        ("Algeria", "#1720", "#1721"),
        ("Argentina", "#1730", "#1731"),
        ("Brazil", "#1780", "#1781"),
        ("Canada", "#1790", "#1791"),
        ("France", "#1890", "#1891"),
        ("Scotland", "#2080", "#2081"),
        ("Switzerland", "#2140", "#2141"),
        ("USA", "#2170", "#2171"),
        ("Uruguay", "#2180", "#2181"),
    ]
    return {
        "class": "named",
        "name": "2026 World Cup Champion",
        "description": "full tournament resolver " * 80,
        "outcomes": [
            {
                "name": name,
                "sides": [_named_side(yes, "Yes"), _named_side(no, "No")],
            }
            for name, yes, no in countries
        ],
    }


def _btc_bucket_market() -> dict:
    return {
        "class": "priceBucket",
        "description": "class:priceBucket|underlying:BTC|expiry:20260624-0600",
        "underlying": "BTC",
        "price_thresholds": [62000.0, 64000.0],
        "expiry": "2026-06-24T06:00:00Z",
        "period": "1d",
        "outcomes": [
            {
                "bucket_index": 0,
                "sides": [_named_side("#5660", "Yes"), _named_side("#5661", "No")],
            }
        ],
    }


async def _mock_outcome_markets(self):
    return True, [
        _world_cup_champion_market(),
        _world_cup_match_market(),
        _btc_bucket_market(),
    ]


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
    assert all(r["class"] in {"priceBinary", "priceBucket"} for r in result["outcomes"])


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
async def test_search_market_type_filter():
    res_perp = await hyperliquid_search_market("bitcoin", limit=10, market_type="perp")
    res_hip3 = await hyperliquid_search_market("bitcoin", limit=10, market_type="hip3")
    res_hip4 = await hyperliquid_search_market("bitcoin", limit=10, market_type="hip4")

    assert {"BTC-USDC"} <= _names(res_perp["result"]["perps"])
    assert not any(":" in r["name"] for r in res_perp["result"]["perps"])
    assert res_perp["result"]["spots"] == [] and res_perp["result"]["outcomes"] == []

    assert {"flx:BTC"} <= _names(res_hip3["result"]["perps"])
    assert all(":" in r["name"] for r in res_hip3["result"]["perps"])

    assert res_hip4["result"]["perps"] == [] and res_hip4["result"]["spots"] == []
    assert res_hip4["result"]["outcomes"]


@pytest.mark.asyncio
async def test_search_hip4_wrapper_only_returns_outcomes():
    res = await hyperliquid_search_hip4("bitcoin", limit=10)
    assert res["ok"]
    result = res["result"]

    assert result["market_type"] == "hip4"
    assert "perps" not in result and "spots" not in result
    assert result["compact"] is True
    assert result["outcomes"]
    assert result["asset_names"]
    assert all(name.startswith("#") for name in result["asset_names"])


@pytest.mark.asyncio
async def test_search_hip4_compacts_and_ranks_specific_world_cup_query(monkeypatch):
    monkeypatch.setattr(HyperliquidAdapter, "get_outcome_markets", _mock_outcome_markets)

    res = await hyperliquid_search_hip4("world cup switzerland canada", limit=15)
    assert res["ok"]
    result = res["result"]

    assert result["compact"] is True
    assert [row["name"] for row in result["outcomes"]] == [
        "World Cup: Switzerland vs Canada",
        "2026 World Cup Champion",
    ]
    assert "description" not in result["outcomes"][0]
    assert result["outcomes"][0]["matched_outcomes"] == [
        {
            "name": "Switzerland",
            "sides": [
                {"name": "Yes", "asset_name": "#5260"},
                {"name": "No", "asset_name": "#5261"},
            ],
        },
        {
            "name": "Canada",
            "sides": [
                {"name": "Yes", "asset_name": "#5280"},
                {"name": "No", "asset_name": "#5281"},
            ],
        },
        {
            "name": "Draw",
            "sides": [
                {"name": "Yes", "asset_name": "#5270"},
                {"name": "No", "asset_name": "#5271"},
            ],
        },
    ]
    champion = result["outcomes"][1]
    assert champion["outcome_count"] == 9
    assert champion["truncated_outcomes"] is True
    assert {row["name"] for row in champion["matched_outcomes"]} == {
        "Canada",
        "Switzerland",
    }
    assert "#5260" in result["asset_names"]
    assert "#5280" in result["asset_names"]
    assert "#5660" not in result["asset_names"]


@pytest.mark.asyncio
async def test_search_hip4_include_details_caps_descriptions(monkeypatch):
    monkeypatch.setattr(HyperliquidAdapter, "get_outcome_markets", _mock_outcome_markets)

    res = await hyperliquid_search_hip4(
        "world cup switzerland canada",
        limit=15,
        include_details=True,
    )
    assert res["ok"]
    result = res["result"]

    assert result["compact"] is False
    first = result["outcomes"][0]
    assert first["name"] == "World Cup: Switzerland vs Canada"
    assert len(first["description"]) <= 300
    assert first["description_truncated"] is True
    assert first["outcomes"][0]["sides"][0]["description"] == "#5260: Yes"


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
