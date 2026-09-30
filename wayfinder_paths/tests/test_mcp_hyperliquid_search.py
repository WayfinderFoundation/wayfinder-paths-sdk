from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.adapters.hyperliquid_adapter import HyperliquidAdapter
from wayfinder_paths.core.constants.hyperliquid import HyperliquidMarketType
from wayfinder_paths.mcp.tools.hyperliquid import (
    _market_search_resources,
    hyperliquid_search_hip4,
    hyperliquid_search_market,
)

# Live HL tests use subset assertions so the suite stays green as HL adds or
# removes markets. Regression tests patch the adapter to avoid rate-limit and
# market-inventory drift.


@pytest.fixture(autouse=True)
def no_tool_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "wayfinder_paths.mcp.utils._report_tool_metric", lambda *_: None
    )


def _names(rows):
    return {row["name"] for row in rows}


def _named_side(asset_name: str, label: str) -> dict:
    return {
        "name": label,
        "asset_name": asset_name,
        "description": f"{asset_name}: {label}",
    }


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
                "sides": [
                    {
                        "name": "Yes",
                        "asset_name": "#5660",
                        "description": "BTC < 62000 at 2026-06-24T06:00:00Z",
                    },
                    {
                        "name": "No",
                        "asset_name": "#5661",
                        "description": "BTC >= 62000 at 2026-06-24T06:00:00Z",
                    },
                ],
            }
        ],
    }


async def _mock_outcome_markets(self):
    return True, [
        _world_cup_champion_market(),
        _world_cup_match_market(),
        _btc_bucket_market(),
    ]


async def _mock_meta_and_asset_ctxs(self):
    return True, [
        {
            "universe": [
                {"name": "BTC"},
                {"name": "ETH"},
                {"name": "GAS"},
                {"name": "xyz:BTC"},
                {"name": "flx:BTC"},
                {"name": "hyna:BTC"},
                {"name": "cash:BTC"},
                {"name": "xyz:NATGAS"},
                {"name": "xyz:BRENTOIL"},
                {"name": "flx:OIL"},
                {"name": "vntl:ENERGY"},
                {"name": "km:USOIL"},
                {"name": "cash:WTI"},
            ]
        },
        [],
    ]


async def _mock_failed_meta_and_asset_ctxs(self):
    return False, "429 Too Many Requests"


async def _mock_spot_assets(self):
    return True, {
        "UBTC/USDC": 10001,
        "UBTC/USDH": 10002,
        "KNTQ/USDH": 10003,
    }


@pytest.fixture
def market_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    async def metadata(self: HyperliquidAdapter) -> tuple[bool, list]:
        success, data = await _mock_meta_and_asset_ctxs(self)
        # Put another quote-sharing market first to catch stable-sort ties.
        data[0]["universe"][:2] = [{"name": "ETH"}, {"name": "BTC"}]
        data[0]["universe"].append({"name": "xyz:NVDA"})
        return success, data

    monkeypatch.setattr(HyperliquidAdapter, "get_meta_and_asset_ctxs", metadata)
    monkeypatch.setattr(HyperliquidAdapter, "get_spot_assets", _mock_spot_assets)
    monkeypatch.setattr(
        HyperliquidAdapter, "get_outcome_markets", _mock_outcome_markets
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "bucket"),
    [("BTC-USDC", "perps"), ("flx:BTC", "perps"), ("UBTC/USDH", "spots")],
)
async def test_search_exact_canonical_identity_ranks_first(
    market_inventory: None, query: str, bucket: str
) -> None:
    response = await hyperliquid_search_market(query, limit=1)
    assert response["ok"]
    assert [row["name"] for row in response["result"][bucket]] == [query]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "names", "expected"),
    [
        ("BTC", ["kBTC", "UBTC", "BTC"], "BTC-USDC"),
        ("bitcoin", ["kBTC", "BTC"], "BTC-USDC"),
        ("NVDA", ["xyz:kNVDA", "xyz:NVDA"], "xyz:NVDA"),
        ("nvidia", ["xyz:kNVDA", "xyz:NVDA"], "xyz:NVDA"),
        ("BTC", ["kBTC/USDC", "UBTC/USDC", "BTC/USDC"], "BTC/USDC"),
        ("bitcoin", ["kBTC/USDC", "UBTC/USDC"], "UBTC/USDC"),
    ],
)
async def test_search_underlying_and_alias_identity_outrank_subsequences(
    market_inventory: None,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    names: list[str],
    expected: str,
) -> None:
    async def metadata(self: HyperliquidAdapter) -> tuple[bool, list]:
        return True, [{"universe": [{"name": name} for name in names]}, []]

    async def spots(self: HyperliquidAdapter) -> tuple[bool, dict[str, int]]:
        return True, {name: 10000 + index for index, name in enumerate(names)}

    if "/" in expected:
        monkeypatch.setattr(HyperliquidAdapter, "get_spot_assets", spots)
        response = await hyperliquid_search_market(query, market_type="spot", limit=1)
        bucket = "spots"
    else:
        monkeypatch.setattr(HyperliquidAdapter, "get_meta_and_asset_ctxs", metadata)
        response = await hyperliquid_search_market(query, limit=1)
        bucket = "perps"
    assert response["ok"]
    assert [row["name"] for row in response["result"][bucket]] == [expected]


@pytest.mark.asyncio
async def test_search_pair_does_not_match_other_underlyings_through_quote(
    market_inventory: None,
) -> None:
    response = await hyperliquid_search_market("BTC-USDC", market_type="perp")
    assert response["ok"]
    assert _names(response["result"]["perps"]) == {"BTC-USDC"}


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["usdc", "usdh", "xyz"])
async def test_search_ignores_quote_and_dex_tokens(
    market_inventory: None, query: str
) -> None:
    response = await hyperliquid_search_market(query)
    assert response["ok"]
    assert response["result"]["perps"] == []
    assert response["result"]["spots"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["bitcoin", ""])
async def test_search_hip3_filter_precedes_limit(
    market_inventory: None, query: str
) -> None:
    response = await hyperliquid_search_market(query, market_type="hip3", limit=1)
    assert response["ok"]
    assert _names(response["result"]["perps"]) == {"xyz:BTC"}
    assert response["result"]["spots"] == []
    assert response["result"]["outcomes"] == []


@pytest.mark.asyncio
async def test_search_perp_filter_precedes_exact_hip3_hit(
    market_inventory: None,
) -> None:
    response = await hyperliquid_search_market("xyz:BTC", market_type="perp", limit=1)
    assert response["ok"]
    assert _names(response["result"]["perps"]) == {"BTC-USDC"}


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["bitcoin", ""])
async def test_search_spot_filter_keeps_only_spot_bucket(
    market_inventory: None, query: str
) -> None:
    response = await hyperliquid_search_market(query, market_type="spot", limit=1)
    assert response["ok"]
    assert _names(response["result"]["spots"]) == {"UBTC/USDC"}
    assert response["result"]["perps"] == []
    assert response["result"]["outcomes"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "bucket", "expected"),
    [
        ("bitcoin", "spots", {"UBTC/USDC", "UBTC/USDH"}),
        ("kinetiq", "spots", {"KNTQ/USDH"}),
        ("nvidia", "perps", {"xyz:NVDA"}),
        ("oil futures", "perps", {"xyz:BRENTOIL", "flx:OIL", "cash:WTI"}),
    ],
)
async def test_search_preserves_alias_and_subsequence_matching(
    market_inventory: None, query: str, bucket: str, expected: set[str]
) -> None:
    response = await hyperliquid_search_market(query, limit=20)
    assert response["ok"]
    assert expected <= _names(response["result"][bucket])


@pytest.mark.asyncio
async def test_search_hip4_text_still_matches_comparison_aliases(
    market_inventory: None,
) -> None:
    response = await hyperliquid_search_market("above", market_type="hip4", limit=1)
    assert response["ok"]
    assert response["result"]["outcomes"] == [_btc_bucket_market()]
    assert response["result"]["perps"] == []
    assert response["result"]["spots"] == []


@pytest.mark.asyncio
async def test_search_includes_public_market_context_without_wallet_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def metadata(self: HyperliquidAdapter) -> tuple[bool, list]:
        return True, [
            {"universe": [{"name": "BTC", "maxLeverage": 40, "szDecimals": 5}]},
            [
                {
                    "funding": "0.00001",
                    "dayNtlVlm": "2000000000",
                    "midPx": "80000",
                    "impactPxs": ["79999", "80001"],
                    "openInterest": "12000",
                }
            ],
        ]

    monkeypatch.setattr(HyperliquidAdapter, "get_meta_and_asset_ctxs", metadata)
    monkeypatch.setattr(HyperliquidAdapter, "get_spot_assets", _mock_spot_assets)
    monkeypatch.setattr(
        HyperliquidAdapter, "get_outcome_markets", _mock_outcome_markets
    )
    response = await hyperliquid_search_market("bitcoin", market_type="perp")
    market = response["result"]["perps"][0]["market"]
    assert market["day_notional_volume_usd"] == 2_000_000_000
    assert market["funding_rate_hourly"] == 0.00001
    assert market["min_order_notional_usd"] == 10
    assert market["compatible_margin_modes"] == ["cross", "isolated"]
    assert market["impact_px_ask"] == 80001
    assert market["open_interest_base"] == 12000
    assert market["open_interest_usd_at_mid"] == 960_000_000
    assert "open_interest" not in market
    assert "raw_context" not in market
    assert response["result"]["warnings"] == []


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
async def test_search_market_type_filter(monkeypatch):
    monkeypatch.setattr(
        HyperliquidAdapter, "get_meta_and_asset_ctxs", _mock_meta_and_asset_ctxs
    )
    monkeypatch.setattr(HyperliquidAdapter, "get_spot_assets", _mock_spot_assets)
    monkeypatch.setattr(
        HyperliquidAdapter, "get_outcome_markets", _mock_outcome_markets
    )

    res_perp = await hyperliquid_search_market("bitcoin", limit=10, market_type="perp")
    res_hip3 = await hyperliquid_search_market("bitcoin", limit=10, market_type="hip3")
    res_hip4 = await hyperliquid_search_market("bitcoin", limit=10, market_type="hip4")

    assert res_perp["ok"]
    assert res_hip3["ok"]
    assert res_hip4["ok"]
    assert {"BTC-USDC"} <= _names(res_perp["result"]["perps"])
    assert not any(":" in r["name"] for r in res_perp["result"]["perps"])
    assert res_perp["result"]["spots"] == [] and res_perp["result"]["outcomes"] == []

    assert {"flx:BTC"} <= _names(res_hip3["result"]["perps"])
    assert all(":" in r["name"] for r in res_hip3["result"]["perps"])

    assert res_hip4["result"]["perps"] == [] and res_hip4["result"]["spots"] == []
    assert res_hip4["result"]["outcomes"]


@pytest.mark.asyncio
async def test_search_market_handles_perp_meta_failure_without_error(monkeypatch):
    monkeypatch.setattr(
        HyperliquidAdapter,
        "get_meta_and_asset_ctxs",
        _mock_failed_meta_and_asset_ctxs,
    )
    monkeypatch.setattr(HyperliquidAdapter, "get_spot_assets", _mock_spot_assets)
    monkeypatch.setattr(
        HyperliquidAdapter, "get_outcome_markets", _mock_outcome_markets
    )

    res = await hyperliquid_search_market("bitcoin", limit=10, market_type="perp")

    assert res["ok"]
    assert res["result"] == {
        "perps": [],
        "spots": [],
        "outcomes": [],
        "warnings": ["perp metadata unavailable; discovery is incomplete"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("market_type", "requested"),
    [
        (None, {"perp", "spot", "outcome"}),
        ("perp", {"perp"}),
        ("hip3", {"perp"}),
        ("spot", {"spot"}),
        ("hip4", {"outcome"}),
    ],
)
async def test_search_only_calls_requested_providers(
    monkeypatch: pytest.MonkeyPatch,
    market_type: HyperliquidMarketType | None,
    requested: set[str],
) -> None:
    calls = {}
    for name, method in {
        "perp": "get_meta_and_asset_ctxs",
        "spot": "get_spot_assets",
        "outcome": "get_outcome_markets",
    }.items():
        calls[name] = AsyncMock(side_effect=RuntimeError("Provider unavailable"))
        monkeypatch.setattr(HyperliquidAdapter, method, calls[name])
    result = await hyperliquid_search_market("BTC", market_type=market_type)
    assert result["ok"]
    assert {name for name, call in calls.items() if call.await_count} == requested
    assert set(result["result"]["warnings"]) == {
        f"{name} metadata unavailable; discovery is incomplete" for name in requested
    }


@pytest.mark.asyncio
async def test_unrelated_provider_exception_preserves_available_markets(
    market_inventory: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        HyperliquidAdapter,
        "get_outcome_markets",
        AsyncMock(side_effect=RuntimeError("429")),
    )
    result = await hyperliquid_search_market("BTC", limit=1)
    assert result["ok"]
    assert result["result"]["perps"][0]["name"] == "BTC-USDC"
    assert result["result"]["spots"][0]["name"] == "UBTC/USDC"
    assert result["result"]["outcomes"] == []
    assert result["result"]["warnings"] == [
        "outcome metadata unavailable; discovery is incomplete"
    ]


@pytest.mark.asyncio
async def test_search_cancellation_is_not_reported_as_missing_markets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        HyperliquidAdapter,
        "get_spot_assets",
        AsyncMock(side_effect=asyncio.CancelledError()),
    )
    with pytest.raises(asyncio.CancelledError):
        await hyperliquid_search_market("BTC", market_type="spot")


@pytest.mark.asyncio
async def test_parallel_searches_reuse_metadata_until_existing_cache_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch = AsyncMock(
        return_value=[{"universe": [{"name": "BTC"}, {"name": "ETH"}]}, []]
    )
    monkeypatch.setattr(HyperliquidAdapter, "_post_across_dexes", fetch)
    responses = await asyncio.gather(
        *(
            hyperliquid_search_market(query, market_type="perp")
            for query in ("BTC", "ETH", "BTC-USDC")
        )
    )
    assert all(response["ok"] for response in responses)
    assert [response["result"]["perps"][0]["name"] for response in responses] == [
        "BTC-USDC",
        "ETH-USDC",
        "BTC-USDC",
    ]
    fetch.assert_awaited_once()
    adapter, _ = _market_search_resources(asyncio.get_running_loop())
    await adapter._cache.delete("hl_meta_and_asset_ctxs")
    await hyperliquid_search_market("BTC", market_type="perp")
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_search_does_not_cache_a_failed_provider_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch = AsyncMock(
        side_effect=[RuntimeError("429"), [{"universe": [{"name": "BTC"}]}, []]]
    )
    monkeypatch.setattr(HyperliquidAdapter, "_post_across_dexes", fetch)
    failed = await hyperliquid_search_market("BTC", market_type="perp")
    recovered = await hyperliquid_search_market("BTC", market_type="perp")
    assert failed["result"]["warnings"]
    assert recovered["result"]["perps"][0]["name"] == "BTC-USDC"
    assert recovered["result"]["warnings"] == []
    assert fetch.await_count == 2


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
    monkeypatch.setattr(
        HyperliquidAdapter, "get_outcome_markets", _mock_outcome_markets
    )

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
    monkeypatch.setattr(
        HyperliquidAdapter, "get_outcome_markets", _mock_outcome_markets
    )

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
async def test_search_oil_futures(monkeypatch):
    monkeypatch.setattr(
        HyperliquidAdapter, "get_meta_and_asset_ctxs", _mock_meta_and_asset_ctxs
    )
    monkeypatch.setattr(HyperliquidAdapter, "get_spot_assets", _mock_spot_assets)
    monkeypatch.setattr(
        HyperliquidAdapter, "get_outcome_markets", _mock_outcome_markets
    )

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
