from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.core.constants.polymarket import derive_deposit_wallet
from wayfinder_paths.mcp.preview import build_polymarket_place_market_order_preview
from wayfinder_paths.mcp.tools.polymarket import (
    polymarket_get_state,
    polymarket_place_market_order,
    polymarket_read,
)

_FIND_WALLET = "wayfinder_paths.mcp.utils.find_wallet_by_label"
_PREVIEW_FIND_WALLET = "wayfinder_paths.mcp.preview.find_wallet_by_label"
_PREVIEW_GET_TOKEN_BALANCE = "wayfinder_paths.mcp.preview.get_token_balance"
_GET_SIGN_CB = "wayfinder_paths.mcp.tools.polymarket.get_wallet_signing_callback"
_GET_HASH_CB = "wayfinder_paths.mcp.tools.polymarket.get_wallet_sign_hash_callback"
_GET_TYPED_CB = (
    "wayfinder_paths.mcp.tools.polymarket.get_wallet_sign_typed_data_callback"
)

_ADDR = "0x000000000000000000000000000000000000dEaD"
_WALLET = {"address": _ADDR}
_SIGN_CB = AsyncMock(return_value=b"\x00" * 65)
_HASH_CB = AsyncMock(return_value="0x" + "00" * 65)
_TYPED_CB = AsyncMock(return_value="0x" + "00" * 65)


@pytest.mark.asyncio
async def test_polymarket_get_state_uses_adapter_full_state():
    full_state = AsyncMock(return_value=(True, {"protocol": "polymarket_read"}))
    with (
        patch(_FIND_WALLET, AsyncMock(return_value=_WALLET)),
        patch(_GET_SIGN_CB, AsyncMock(return_value=(_SIGN_CB, _ADDR))),
        patch(_GET_HASH_CB, AsyncMock(return_value=(_HASH_CB, _ADDR))),
        patch(_GET_TYPED_CB, AsyncMock(return_value=(_TYPED_CB, _ADDR))),
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_full_user_state",
            new=full_state,
        ),
    ):
        out = await polymarket_get_state(wallet_label="main")
        assert out["ok"] is True
        assert out["result"]["ok"] is True
        assert out["result"]["state"]["protocol"] == "polymarket_read"
        assert out["result"]["account"] == derive_deposit_wallet(_ADDR)
        assert full_state.await_args.kwargs["account"] == derive_deposit_wallet(_ADDR)


@pytest.mark.asyncio
async def test_polymarket_search_uses_adapter_search():
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.search_markets",
            new=AsyncMock(
                return_value=(
                    True,
                    [
                        {
                            "slug": "m1",
                            "eventSlug": "e1",
                            "question": "Will BTC rally?",
                            "yesPrice": 0.42,
                            "noPrice": 0.58,
                            "yesTokenId": "tok_yes",
                            "noTokenId": "tok_no",
                            "conditionId": "0xabc",
                            "liquidity": 1234.0,
                            "volume24h": 5678.0,
                            "resolvesAt": "2026-06-01T00:00:00Z",
                        }
                    ],
                )
            ),
        ),
    ):
        out = await polymarket_read("search", query="bitcoin", limit=1)
        assert out["ok"] is True
        assert out["result"]["action"] == "search"
        assert out["result"]["summaryMode"] is True
        assert "markets" not in out["result"]
        candidate = out["result"]["candidates"][0]
        assert candidate["slug"] == "m1"
        assert candidate["outcomes"][0] == {
            "label": "Yes",
            "price": 0.42,
            "tokenId": "tok_yes",
        }
        assert candidate["outcomes"][1]["tokenId"] == "tok_no"
        assert out["result"]["truncation"]["rawAvailableWithSummaryFalse"] is True


@pytest.mark.asyncio
async def test_polymarket_search_suggests_event_hydration_for_grouped_results():
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.search_markets",
            new=AsyncMock(
                return_value=(
                    True,
                    [
                        {
                            "slug": f"event-market-{idx}",
                            "eventSlug": "event-ladder",
                            "question": f"Event by day {idx}?",
                            "yesPrice": 0.1 * idx,
                            "noPrice": 1 - (0.1 * idx),
                            "yesTokenId": f"tok_yes_{idx}",
                            "noTokenId": f"tok_no_{idx}",
                            "liquidity": 100 * idx,
                            "volume24h": 200 * idx,
                        }
                        for idx in range(1, 12)
                    ],
                )
            ),
        ),
    ):
        out = await polymarket_read("search", query="event ladder", limit=11)

    assert out["ok"] is True
    result = out["result"]
    assert len(result["candidates"]) == 10
    assert result["truncation"] == {
        "totalAvailable": 11,
        "returnedCandidates": 10,
        "truncated": True,
        "rawAvailableWithSummaryFalse": True,
    }
    assert result["eventGroups"][0]["eventSlug"] == "event-ladder"
    assert result["eventGroups"][0]["candidatesInSearch"] == 11
    assert result["nextSuggestedCalls"][0]["call"] == {
        "action": "get_event",
        "event_slug": "event-ladder",
        "candidate_limit": 20,
    }


@pytest.mark.asyncio
async def test_polymarket_search_uses_compact_variants_not_generated_slugs():
    calls: list[str] = []

    async def fake_search_markets(*_args, **kwargs):
        calls.append(kwargs["query"])
        if kwargs["query"] == "anthropic openai ipo first":
            return (
                True,
                [
                    {
                        "slug": "will-anthropic-or-openai-ipo-first",
                        "eventSlug": "will-anthropic-or-openai-ipo-first",
                        "question": "Will Anthropic or OpenAI IPO first?",
                        "outcomes": ["Anthropic", "OpenAI"],
                        "outcomePrices": [0.73, 0.27],
                        "clobTokenIds": ["tok_anthropic", "tok_openai"],
                        "conditionId": "0xipo",
                        "enableOrderBook": True,
                        "acceptingOrders": True,
                        "active": True,
                        "closed": False,
                        "liquidityNum": "10000",
                        "volume24hr": "5000",
                    }
                ],
            )
        return (
            True,
            [
                {
                    "slug": "will-claude-fable-5-be-restored-for-us-customers-by-june-17",
                    "eventSlug": "claude-fable",
                    "question": "Will Claude Fable 5 be restored for US customers by June 17?",
                    "yesPrice": 0.42,
                    "noPrice": 0.58,
                    "yesTokenId": "tok_bad_yes",
                    "noTokenId": "tok_bad_no",
                    "conditionId": "0xbad",
                    "liquidity": 999_999.0,
                    "volume24h": 999_999.0,
                }
            ],
        )

    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.search_markets",
            new=fake_search_markets,
        ),
    ):
        out = await polymarket_read(
            "search",
            query="do we think openai or anthropic will ipo first?",
            candidate_limit=3,
        )

    assert out["ok"] is True
    result = out["result"]
    assert result["relevance"]["mode"] == "expanded"
    assert result["relevance"]["directHydrations"] == []
    assert calls[0] == "openai anthropic ipo first"
    assert "anthropic openai ipo first" in calls
    assert result["candidates"][0]["slug"] == "will-anthropic-or-openai-ipo-first"


@pytest.mark.asyncio
async def test_polymarket_search_reranks_relevant_candidate_before_truncation():
    rows = [
        {
            "slug": f"irrelevant-ai-market-{idx}",
            "eventSlug": "ai-models",
            "question": f"Will an unrelated AI model lead benchmark {idx}?",
            "yesPrice": 0.1,
            "noPrice": 0.9,
            "yesTokenId": f"tok_bad_yes_{idx}",
            "noTokenId": f"tok_bad_no_{idx}",
            "conditionId": f"0xbad{idx}",
            "liquidity": 1_000_000.0 - idx,
            "volume24h": 1_000_000.0 - idx,
        }
        for idx in range(8)
    ]
    rows.append(
        {
            "slug": "will-france-win-the-2026-fifa-world-cup-924",
            "eventSlug": "world-cup-winner",
            "question": "Will France win the 2026 FIFA World Cup?",
            "yesPrice": 0.12,
            "noPrice": 0.88,
            "yesTokenId": "tok_france_yes",
            "noTokenId": "tok_france_no",
            "conditionId": "0xfrance",
            "liquidity": 10_000.0,
            "volume24h": 5_000.0,
        }
    )

    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.search_markets",
            new=AsyncMock(return_value=(True, rows)),
        ),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_event_by_slug",
            new=AsyncMock(return_value=(False, "Event not found")),
        ),
    ):
        out = await polymarket_read(
            "search",
            query="what are France's world cup odds",
            candidate_limit=5,
        )

    assert out["ok"] is True
    result = out["result"]
    assert (
        result["candidates"][0]["slug"] == "will-france-win-the-2026-fifa-world-cup-924"
    )
    assert result["truncation"]["totalAvailable"] == 9


@pytest.mark.asyncio
async def test_polymarket_search_hydrates_parent_event_for_child_market_recall():
    async def fake_search_markets(*_args, **_kwargs):
        return (
            True,
            [
                {
                    "slug": "will-a-uefa-team-win-the-2026-fifa-world-cup",
                    "eventSlug": "world-cup-winner",
                    "question": "Will a UEFA team win the 2026 FIFA World Cup?",
                    "yesPrice": 0.5,
                    "noPrice": 0.5,
                    "yesTokenId": "tok_uefa_yes",
                    "noTokenId": "tok_uefa_no",
                    "conditionId": "0xuefa",
                    "liquidity": 200_000.0,
                    "volume24h": 100_000.0,
                },
                {
                    "slug": "world-cup-halftime-show",
                    "eventSlug": "world-cup-halftime-show",
                    "question": "Who will perform at the World Cup halftime show?",
                    "yesPrice": 0.1,
                    "noPrice": 0.9,
                    "yesTokenId": "tok_show_yes",
                    "noTokenId": "tok_show_no",
                    "conditionId": "0xshow",
                    "liquidity": 900_000.0,
                    "volume24h": 800_000.0,
                },
            ],
        )

    async def fake_get_event_by_slug(_self, slug: str):
        if slug == "world-cup-winner":
            return (
                True,
                {
                    "slug": "world-cup-winner",
                    "title": "World Cup Winner",
                    "markets": [
                        {
                            "slug": "will-france-win-the-2026-fifa-world-cup-924",
                            "question": "Will France win the 2026 FIFA World Cup?",
                            "yesPrice": 0.12,
                            "noPrice": 0.88,
                            "yesTokenId": "tok_france_yes",
                            "noTokenId": "tok_france_no",
                            "conditionId": "0xfrance",
                            "liquidity": 10_000.0,
                            "volume24h": 5_000.0,
                        }
                    ],
                },
            )
        return False, "Event not found"

    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.search_markets",
            new=fake_search_markets,
        ),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_event_by_slug",
            new=fake_get_event_by_slug,
        ),
    ):
        out = await polymarket_read(
            "search",
            query="what are France's world cup odds",
            candidate_limit=5,
        )

    assert out["ok"] is True
    result = out["result"]
    assert "world-cup-winner" in result["relevance"]["eventHydrations"]
    assert (
        result["candidates"][0]["slug"] == "will-france-win-the-2026-fifa-world-cup-924"
    )


@pytest.mark.asyncio
async def test_polymarket_search_summary_false_preserves_raw_markets():
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.search_markets",
            new=AsyncMock(return_value=(True, [{"slug": "m1", "raw": True}])),
        ),
    ):
        out = await polymarket_read("search", query="bitcoin", limit=1, summary=False)
        assert out["ok"] is True
        assert "summaryMode" not in out["result"]
        assert out["result"]["markets"] == [{"slug": "m1", "raw": True}]


@pytest.mark.asyncio
async def test_polymarket_get_event_summary_returns_compact_candidates():
    event = {
        "slug": "world-cup-winner",
        "title": "World Cup winner",
        "description": "Pick the tournament winner.",
        "markets": [
            {
                "slug": "closed-world-cup-market",
                "question": "Closed market",
                "outcomes": ["Yes", "No"],
                "outcomePrices": [1.0, 0.0],
                "clobTokenIds": ["tok_closed_yes", "tok_closed_no"],
                "enableOrderBook": True,
                "acceptingOrders": False,
                "active": True,
                "closed": True,
                "liquidityNum": "999999.99",
                "volume24hr": "999999.99",
            },
            {
                "slug": "world-cup-winner-2026",
                "question": "Who will win the 2026 World Cup?",
                "outcomes": ["Brazil", "France", "Spain"],
                "outcomePrices": [0.25, 0.2, 0.12],
                "clobTokenIds": ["tok_brazil", "tok_france", "tok_spain"],
                "conditionId": "0xcond1",
                "enableOrderBook": True,
                "acceptingOrders": True,
                "active": True,
                "closed": False,
                "liquidityNum": "12345.67",
                "volume24hr": "987.65",
                "endDate": "2026-07-19T00:00:00Z",
                "rawLargeField": {"should": "not appear"},
            },
            {
                "slug": "other-world-cup-market",
                "question": "Another market",
                "outcomes": ["Yes", "No"],
                "outcomePrices": [0.5, 0.5],
                "clobTokenIds": ["tok_yes", "tok_no"],
            },
        ],
    }
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_event_by_slug",
            new=AsyncMock(return_value=(True, event)),
        ),
    ):
        out = await polymarket_read(
            "get_event", event_slug="world-cup-winner", candidate_limit=1
        )

    assert out["ok"] is True
    result = out["result"]
    assert result["summaryMode"] is True
    assert result["event"] == {
        "slug": "world-cup-winner",
        "title": "World Cup winner",
        "description": "Pick the tournament winner.",
        "startDate": None,
        "endDate": None,
        "active": None,
        "closed": None,
    }
    assert "markets" not in result["event"]
    assert result["truncation"] == {
        "totalAvailable": 3,
        "returnedCandidates": 1,
        "truncated": True,
        "rawAvailableWithSummaryFalse": True,
    }
    candidate = result["candidates"][0]
    assert candidate["slug"] == "world-cup-winner-2026"
    assert candidate["eventSlug"] == "world-cup-winner"
    assert candidate["outcomes"] == [
        {"label": "Brazil", "price": 0.25, "tokenId": "tok_brazil"},
        {"label": "France", "price": 0.2, "tokenId": "tok_france"},
        {"label": "Spain", "price": 0.12, "tokenId": "tok_spain"},
    ]
    assert candidate["liquidity"] == 12345.67
    assert candidate["tradable"] is True
    assert "rawLargeField" not in candidate


@pytest.mark.asyncio
async def test_polymarket_get_event_summary_hydrates_sports_child_events():
    event = {
        "id": "351763",
        "slug": "fifwc-che-can-2026-06-24",
        "title": "Switzerland vs. Canada",
        "gameId": 90086957,
        "tags": [{"slug": "sports"}],
        "markets": [
            {
                "slug": "fifwc-che-can-2026-06-24-che",
                "question": "Will Switzerland win?",
                "outcomes": ["Yes", "No"],
                "outcomePrices": [0.42, 0.58],
                "clobTokenIds": ["parent_yes", "parent_no"],
                "sportsMarketType": "moneyline",
                "bestBid": 0.41,
                "bestAsk": 0.42,
                "enableOrderBook": True,
                "acceptingOrders": True,
                "active": True,
                "closed": False,
            }
        ],
    }
    child_events = [
        {
            "id": "619747",
            "slug": "fifwc-che-can-2026-06-24-player-props",
            "title": "Switzerland vs. Canada - Player Props",
            "markets": [
                {
                    "slug": "fifwc-che-can-2026-06-24-shots-david-gte2",
                    "question": "Jonathan David: 2+ shots",
                    "groupItemTitle": "Jonathan David: 2+ shots",
                    "sportsMarketType": "soccer_player_shots",
                    "outcomes": ["Yes", "No"],
                    "outcomePrices": [0.55, 0.45],
                    "clobTokenIds": ["child_yes", "child_no"],
                    "line": 1.5,
                    "liquidity": "1000",
                    "volume24hr": "500",
                    "bestBid": 0.54,
                    "bestAsk": 0.55,
                    "enableOrderBook": True,
                    "acceptingOrders": True,
                    "active": True,
                    "closed": False,
                    "marketMetadata": {
                        "opticOddsMarketName": "Player Shots",
                        "opticOddsPlayerId": "player-1",
                        "opticOddsSelection": "Jonathan David Over 1.5",
                        "largeIgnoredField": "x" * 1000,
                    },
                }
            ],
        }
    ]

    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_event_by_slug",
            new=AsyncMock(return_value=(True, event)),
        ),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.list_events",
            new=AsyncMock(return_value=(True, child_events)),
        ),
    ):
        out = await polymarket_read(
            "get_event",
            event_slug="fifwc-che-can-2026-06-24",
            candidate_limit=10,
        )

    assert out["ok"] is True
    result = out["result"]
    assert result["sportsBoard"] == {
        "parentMarketCount": 1,
        "childEventCount": 1,
        "childMarketCount": 1,
        "totalMarketCount": 2,
    }
    assert result["childEvents"][0]["slug"] == "fifwc-che-can-2026-06-24-player-props"
    assert result["childEvents"][0]["marketCount"] == 1
    assert result["categorySummary"][0]["sportsMarketType"] == "soccer_player_shots"
    child = next(
        c for c in result["candidates"] if c["slug"].endswith("shots-david-gte2")
    )
    assert child["eventSlug"] == "fifwc-che-can-2026-06-24-player-props"
    assert child["sportsMarketType"] == "soccer_player_shots"
    assert child["groupItemTitle"] == "Jonathan David: 2+ shots"
    assert child["line"] == 1.5
    assert child["outcomes"][0]["tokenId"] == "child_yes"
    assert child["marketMetadata"] == {
        "opticOddsMarketName": "Player Shots",
        "opticOddsPlayerId": "player-1",
        "opticOddsSelection": "Jonathan David Over 1.5",
    }


@pytest.mark.asyncio
async def test_polymarket_search_exact_sports_url_hydrates_event():
    event = {
        "id": "351763",
        "slug": "fifwc-che-can-2026-06-24",
        "gameId": 90086957,
        "markets": [],
    }
    child_events = [
        {
            "id": "619747",
            "slug": "fifwc-che-can-2026-06-24-player-props",
            "markets": [
                {
                    "slug": "fifwc-che-can-2026-06-24-saves-crepeau-gte3",
                    "question": "Maxime Crepeau: 3+ saves",
                    "sportsMarketType": "soccer_player_goalkeeper_saves",
                    "outcomes": ["Yes", "No"],
                    "outcomePrices": [0.4, 0.6],
                    "clobTokenIds": ["yes", "no"],
                    "enableOrderBook": True,
                    "acceptingOrders": True,
                    "active": True,
                    "closed": False,
                }
            ],
        }
    ]

    get_event = AsyncMock(return_value=(True, event))
    search = AsyncMock()
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_event_by_slug",
            new=get_event,
        ),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.list_events",
            new=AsyncMock(return_value=(True, child_events)),
        ),
        patch("wayfinder_paths.mcp.tools.polymarket.relevance_search", new=search),
    ):
        out = await polymarket_read(
            "search",
            query="https://polymarket.com/sports/world-cup/fifwc-che-can-2026-06-24",
        )

    assert out["ok"] is True
    result = out["result"]
    assert result["action"] == "search"
    assert result["exactEventHydration"] is True
    assert result["eventSlug"] == "fifwc-che-can-2026-06-24"
    assert result["sportsBoard"]["totalMarketCount"] == 1
    assert (
        result["candidates"][0]["sportsMarketType"] == "soccer_player_goalkeeper_saves"
    )
    get_event.assert_awaited_once()
    search.assert_not_awaited()


@pytest.mark.asyncio
async def test_polymarket_get_event_default_returns_ten_candidates_and_suggests_more():
    event = {
        "slug": "date-ladder",
        "markets": [
            {
                "slug": f"date-ladder-{idx}",
                "question": f"Date ladder by day {idx}?",
                "outcomes": ["Yes", "No"],
                "outcomePrices": [0.1, 0.9],
                "clobTokenIds": [f"yes_{idx}", f"no_{idx}"],
                "enableOrderBook": True,
                "acceptingOrders": True,
                "active": True,
                "closed": False,
            }
            for idx in range(12)
        ],
    }
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_event_by_slug",
            new=AsyncMock(return_value=(True, event)),
        ),
    ):
        out = await polymarket_read("get_event", event_slug="date-ladder")

    assert out["ok"] is True
    result = out["result"]
    assert len(result["candidates"]) == 10
    assert result["truncation"]["truncated"] is True
    assert result["nextSuggestedCalls"] == [
        {
            "reason": "event candidates truncated",
            "call": {
                "action": "get_event",
                "event_slug": "date-ladder",
                "candidate_limit": 20,
            },
        }
    ]


@pytest.mark.asyncio
async def test_polymarket_get_event_summary_false_preserves_raw_event():
    event = {"slug": "event", "markets": [{"slug": "m1", "raw": True}]}
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_event_by_slug",
            new=AsyncMock(return_value=(True, event)),
        ),
    ):
        out = await polymarket_read("get_event", event_slug="event", summary=False)
    assert out["ok"] is True
    assert out["result"]["event"] == event


@pytest.mark.asyncio
async def test_polymarket_get_market_summary_and_raw_modes():
    market = {
        "slug": "market",
        "question": "Will it happen?",
        "description": "Resolution text " * 80,
        "resolutionSource": "https://example.com/rules",
        "outcomes": ["Yes", "No"],
        "outcomePrices": [0.4, 0.6],
        "clobTokenIds": ["tok_yes", "tok_no"],
        "conditionId": "0xcond",
        "raw": {"nested": True},
    }
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_market_by_slug",
            new=AsyncMock(return_value=(True, market)),
        ),
    ):
        summary = await polymarket_read("get_market", market_slug="market")
        raw = await polymarket_read("get_market", market_slug="market", summary=False)

    assert summary["ok"] is True
    assert summary["result"]["summaryMode"] is True
    assert summary["result"]["market"]["outcomes"][0]["tokenId"] == "tok_yes"
    assert len(summary["result"]["market"]["description"]) < len(market["description"])
    assert "raw" not in summary["result"]["market"]
    assert raw["ok"] is True
    assert raw["result"]["market"] == market


@pytest.mark.asyncio
async def test_polymarket_get_market_preserves_structured_gamma_error():
    gamma_error = {
        "code": "gamma_http_error",
        "message": "Gamma returned HTTP 404 for /markets/slug/event-slug",
        "statusCode": 404,
        "endpoint": "/markets/slug/event-slug",
        "slug": "event-slug",
        "hint": "No market found for this slug. Call get_event_by_slug().",
    }
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_market_by_slug",
            new=AsyncMock(return_value=(False, gamma_error)),
        ),
    ):
        out = await polymarket_read("get_market", market_slug="event-slug")

    assert out["ok"] is False
    assert out["error"]["code"] == "gamma_http_error"
    assert out["error"]["message"] == gamma_error["message"]
    assert out["error"]["details"] == gamma_error


@pytest.mark.asyncio
async def test_polymarket_order_book_summary_is_compact_by_default():
    book = {
        "market": "0xmarket",
        "asset_id": "tok_yes",
        "timestamp": "123",
        "bids": [
            {"price": "0.03", "size": "100"},
            {"price": "0.01", "size": "1000"},
            {"price": "0.02", "size": "500"},
            {"price": "0.025", "size": "50"},
        ],
        "asks": [
            {"price": "0.05", "size": "100"},
            {"price": "0.04", "size": "200"},
            {"price": "0.06", "size": "50"},
            {"price": "0.045", "size": "300"},
        ],
    }
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_order_book",
            new=AsyncMock(return_value=(True, book)),
        ),
    ):
        summary = await polymarket_read("order_book", token_id="tok_yes")
        raw = await polymarket_read("order_book", token_id="tok_yes", summary=False)

    assert summary["ok"] is True
    result = summary["result"]
    assert result["summaryMode"] is True
    assert result["book"]["bestBid"] == 0.03
    assert result["book"]["bestAsk"] == 0.04
    assert result["book"]["spread"] == pytest.approx(0.01)
    assert result["book"]["bidLevels"] == 4
    assert result["book"]["askLevels"] == 4
    assert [level["price"] for level in result["book"]["topBids"]] == [
        0.03,
        0.025,
        0.02,
    ]
    assert [level["price"] for level in result["book"]["topAsks"]] == [
        0.04,
        0.045,
        0.05,
    ]
    assert "bids" not in result["book"]
    assert raw["ok"] is True
    assert raw["result"]["book"] == book


@pytest.mark.asyncio
async def test_polymarket_order_book_resolves_exact_market_slug_and_outcome():
    market = {
        "slug": "will-brazil-win-the-2026-fifa-world-cup-183",
        "question": "Will Brazil win the 2026 FIFA World Cup?",
        "outcomes": ["Yes", "No"],
        "clobTokenIds": ["tok_brazil_yes", "tok_brazil_no"],
    }
    book = {"bids": [{"price": "0.05", "size": "100"}], "asks": []}
    get_order_book = AsyncMock(return_value=(True, book))
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_market_by_slug",
            new=AsyncMock(return_value=(True, market)),
        ),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_order_book",
            new=get_order_book,
        ),
    ):
        out = await polymarket_read(
            "order_book",
            market_slug="will-brazil-win-the-2026-fifa-world-cup-183",
            outcome="YES",
        )

    assert out["ok"] is True
    assert out["result"]["token_id"] == "tok_brazil_yes"
    assert out["result"]["resolution"]["source"] == "market_slug"
    assert get_order_book.await_args.kwargs["token_id"] == "tok_brazil_yes"


@pytest.mark.asyncio
async def test_polymarket_order_book_resolves_loose_market_slug_with_search_fallback():
    rows = [
        {
            "slug": "will-brazil-win-the-2026-fifa-world-cup-183",
            "eventSlug": "world-cup-winner",
            "question": "Will Brazil win the 2026 FIFA World Cup?",
            "yesTokenId": "tok_brazil_yes",
            "noTokenId": "tok_brazil_no",
            "yesPrice": 0.0505,
            "noPrice": 0.9495,
            "liquidity": 3406600.0,
            "volume24h": 1283310.0,
        }
    ]
    get_order_book = AsyncMock(return_value=(True, {"bids": [], "asks": []}))
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_market_by_slug",
            new=AsyncMock(
                return_value=(
                    False,
                    {"code": "gamma_http_error", "message": "not found"},
                )
            ),
        ),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.search_markets",
            new=AsyncMock(return_value=(True, rows)),
        ),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_order_book",
            new=get_order_book,
        ),
    ):
        out = await polymarket_read(
            "order_book",
            market_slug="brazil",
            outcome="YES",
        )

    assert out["ok"] is True
    assert out["result"]["token_id"] == "tok_brazil_yes"
    assert out["result"]["resolution"]["source"] == "search_market_match"
    assert out["result"]["resolution"]["market_slug"] == rows[0]["slug"]
    assert get_order_book.await_args.kwargs["token_id"] == "tok_brazil_yes"


@pytest.mark.asyncio
async def test_polymarket_order_book_falls_back_when_slug_lookup_is_not_clob_tradable():
    non_clob_market = {
        "slug": "brazil",
        "question": "Brazil",
        "outcomes": ["Yes", "No"],
    }
    rows = [
        {
            "slug": "will-brazil-win-the-2026-fifa-world-cup-183",
            "eventSlug": "world-cup-winner",
            "question": "Will Brazil win the 2026 FIFA World Cup?",
            "yesTokenId": "tok_brazil_yes",
            "noTokenId": "tok_brazil_no",
            "yesPrice": 0.0505,
            "noPrice": 0.9495,
            "liquidity": 3406600.0,
            "volume24h": 1283310.0,
        }
    ]
    get_order_book = AsyncMock(return_value=(True, {"bids": [], "asks": []}))
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_market_by_slug",
            new=AsyncMock(return_value=(True, non_clob_market)),
        ),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.search_markets",
            new=AsyncMock(return_value=(True, rows)),
        ),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_order_book",
            new=get_order_book,
        ),
    ):
        out = await polymarket_read(
            "order_book",
            market_slug="brazil",
            outcome="YES",
        )

    assert out["ok"] is True
    assert out["result"]["token_id"] == "tok_brazil_yes"
    assert out["result"]["resolution"]["source"] == "search_market_match"
    assert get_order_book.await_args.kwargs["token_id"] == "tok_brazil_yes"


@pytest.mark.asyncio
async def test_polymarket_order_book_returns_candidates_for_ambiguous_loose_slug():
    rows = [
        {
            "slug": "will-brazil-win-the-2026-fifa-world-cup-183",
            "eventSlug": "world-cup-winner",
            "question": "Will Brazil win the 2026 FIFA World Cup?",
            "yesTokenId": "tok_brazil_yes",
            "noTokenId": "tok_brazil_no",
            "yesPrice": 0.0505,
            "noPrice": 0.9495,
            "liquidity": 3406600.0,
            "volume24h": 1283310.0,
        },
        {
            "slug": "will-brazil-win-group-c-in-the-2026-fifa-world-cup",
            "eventSlug": "world-cup-group-c-winner",
            "question": "Will Brazil win Group C in the 2026 FIFA World Cup?",
            "yesTokenId": "tok_group_yes",
            "noTokenId": "tok_group_no",
            "yesPrice": 0.65,
            "noPrice": 0.35,
            "liquidity": 100000.0,
            "volume24h": 50000.0,
        },
    ]
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_market_by_slug",
            new=AsyncMock(return_value=(False, "not found")),
        ),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.search_markets",
            new=AsyncMock(return_value=(True, rows)),
        ),
    ):
        out = await polymarket_read("order_book", market_slug="brazil", outcome="YES")

    assert out["ok"] is False
    assert out["error"]["code"] == "ambiguous_market_slug"
    assert "candidates" in out["error"]["details"]
    assert out["error"]["details"]["candidates"][0]["outcomes"][0]["tokenId"]


@pytest.mark.asyncio
async def test_polymarket_price_history_resolves_market_slug_and_outcome():
    market = {
        "slug": "will-germany-win-the-2026-fifa-world-cup-467",
        "question": "Will Germany win the 2026 FIFA World Cup?",
        "outcomes": ["Yes", "No"],
        "clobTokenIds": ["tok_germany_yes", "tok_germany_no"],
    }
    get_history = AsyncMock(return_value=(True, [{"t": 1, "p": 0.056}]))
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_market_by_slug",
            new=AsyncMock(return_value=(True, market)),
        ),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_prices_history",
            new=get_history,
        ),
    ):
        out = await polymarket_read(
            "price_history",
            market_slug="will-germany-win-the-2026-fifa-world-cup-467",
            outcome="NO",
            interval="1d",
        )

    assert out["ok"] is True
    assert out["result"]["token_id"] == "tok_germany_no"
    assert out["result"]["resolution"]["source"] == "market_slug"
    assert get_history.await_args.kwargs["token_id"] == "tok_germany_no"


@pytest.mark.asyncio
async def test_polymarket_quote_uses_adapter_quote_by_token_id():
    quote_market_order = AsyncMock(
        return_value=(
            True,
            {
                "token_id": "tok_yes",
                "side": "BUY",
                "requested_amount": 4.2,
                "filled_amount": 4.2,
                "fully_fillable": True,
                "average_price": 0.42,
                "shares": 10.0,
                "notional_usdc": 4.2,
            },
        )
    )
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.quote_market_order",
            new=quote_market_order,
        ),
    ):
        out = await polymarket_read(
            "quote",
            token_id="tok_yes",
            side="BUY",
            buy_amount_pusd=4.2,
        )
        assert out["ok"] is True
        assert out["result"]["action"] == "quote"
        assert out["result"]["token_id"] == "tok_yes"
        assert out["result"]["sizing_kind"] == "buy_amount_pusd"
        assert out["result"]["buy_amount_pusd"] == 4.2
        assert out["result"]["sell_amount_shares"] is None
        assert quote_market_order.await_args.kwargs["amount"] == 4.2
        summary = out["result"]["executionSummary"]
        assert summary["inputAmountType"] == "collateral"
        assert summary["requestedCollateral"] == 4.2
        assert summary["requestedShares"] is None
        assert summary["collateralSpent"] == 4.2
        assert summary["sharesFilled"] == 10.0
        assert summary["avgPrice"] == 0.42
        assert summary["fillRatio"] == 1.0
        assert summary["status"] == "filled"
        assert out["result"]["quote"]["average_price"] == 0.42


@pytest.mark.asyncio
async def test_polymarket_quote_uses_adapter_quote_by_market_slug():
    quote_prediction = AsyncMock(
        return_value=(
            True,
            {
                "token_id": "tok_yes",
                "side": "SELL",
                "requested_amount": 3.0,
                "filled_amount": 3.0,
                "fully_fillable": True,
                "average_price": 0.61,
                "shares": 3.0,
                "notional_usdc": 1.83,
            },
        )
    )
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.quote_prediction",
            new=quote_prediction,
        ),
    ):
        out = await polymarket_read(
            "quote",
            market_slug="market-slug",
            outcome="YES",
            side="SELL",
            sell_amount_shares=3.0,
        )
        assert out["ok"] is True
        assert out["result"]["action"] == "quote"
        assert out["result"]["side"] == "SELL"
        assert out["result"]["sizing_kind"] == "sell_amount_shares"
        assert out["result"]["buy_amount_pusd"] is None
        assert out["result"]["sell_amount_shares"] == 3.0
        assert quote_prediction.await_args.kwargs["amount"] == 3.0
        summary = out["result"]["executionSummary"]
        assert summary["inputAmountType"] == "shares"
        assert summary["requestedCollateral"] is None
        assert summary["requestedShares"] == 3.0
        assert summary["collateralReceived"] == 1.83
        assert summary["sharesFilled"] == 3.0
        assert summary["fillRatio"] == 1.0
        assert out["result"]["quote"]["average_price"] == 0.61


@pytest.mark.asyncio
async def test_polymarket_quote_buy_requires_buy_amount_pusd():
    with patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}):
        out = await polymarket_read("quote", token_id="tok_yes", side="BUY")
        assert out["ok"] is False
        assert out["error"]["code"] == "error"
        assert "buy_amount_pusd" in out["error"]["message"]


@pytest.mark.asyncio
async def test_polymarket_quote_sell_requires_sell_amount_shares():
    with patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}):
        out = await polymarket_read("quote", token_id="tok_yes", side="SELL")
        assert out["ok"] is False
        assert out["error"]["code"] == "error"
        assert "sell_amount_shares" in out["error"]["message"]


@pytest.mark.asyncio
async def test_polymarket_quote_rejects_ambiguous_or_side_mismatched_size():
    with patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}):
        both = await polymarket_read(
            "quote",
            token_id="tok_yes",
            side="BUY",
            buy_amount_pusd=4.0,
            sell_amount_shares=10.0,
        )
        assert both["ok"] is False
        assert "exactly one sizing field" in both["error"]["message"]

        side_mismatch = await polymarket_read(
            "quote",
            token_id="tok_yes",
            side="SELL",
            buy_amount_pusd=4.0,
        )
        assert side_mismatch["ok"] is False
        assert "sell_amount_shares" in side_mismatch["error"]["message"]

        non_positive = await polymarket_read(
            "quote",
            token_id="tok_yes",
            side="BUY",
            buy_amount_pusd=0,
        )
        assert non_positive["ok"] is False
        assert "positive" in non_positive["error"]["message"]


@pytest.mark.asyncio
async def test_polymarket_quote_surfaces_adapter_failure():
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.quote_market_order",
            new=AsyncMock(return_value=(False, "order book unavailable")),
        ),
    ):
        out = await polymarket_read(
            "quote",
            token_id="tok_yes",
            side="BUY",
            buy_amount_pusd=4.2,
        )
        assert out["ok"] is False
        assert out["error"]["code"] == "error"
        assert "order book unavailable" in out["error"]["message"]


@pytest.mark.asyncio
async def test_polymarket_place_market_order_buy_returns_normalized_summary(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path / "runs"))
    place_prediction = AsyncMock(
        return_value=(
            True,
            {
                "status": "matched",
                "quote": {
                    "token_id": "tok_yes",
                    "side": "BUY",
                    "requested_amount": 2.0,
                    "filled_amount": 2.0,
                    "fully_fillable": True,
                    "average_price": 0.05,
                    "shares": 40.0,
                    "notional_usdc": 2.0,
                },
            },
        )
    )

    with (
        patch(_FIND_WALLET, AsyncMock(return_value=_WALLET)),
        patch(_GET_SIGN_CB, AsyncMock(return_value=(_SIGN_CB, _ADDR))),
        patch(_GET_HASH_CB, AsyncMock(return_value=(_HASH_CB, _ADDR))),
        patch(_GET_TYPED_CB, AsyncMock(return_value=(_TYPED_CB, _ADDR))),
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.place_prediction",
            new=place_prediction,
        ),
    ):
        out = await polymarket_place_market_order(
            wallet_label="main",
            market_slug="bitcoin-above-70k-on-february-9",
            outcome="YES",
            side="BUY",
            buy_amount_pusd=2.0,
        )
        assert out["ok"] is True
        assert out["result"]["status"] == "confirmed"
        assert out["result"]["sizing_kind"] == "buy_amount_pusd"
        assert out["result"]["buy_amount_pusd"] == 2.0
        assert out["result"]["sell_amount_shares"] is None
        assert place_prediction.await_args.kwargs["amount_collateral"] == 2.0
        summary = out["result"]["executionSummary"]
        assert summary["requestedCollateral"] == 2.0
        assert summary["requestedShares"] is None
        assert summary["collateralSpent"] == 2.0
        assert summary["sharesFilled"] == 40.0
        assert summary["avgPrice"] == 0.05
        assert summary["fillRatio"] == 1.0
        assert summary["status"] == "filled"
        assert out["result"]["raw"]["status"] == "matched"
        effects = out["result"]["effects"]
        assert effects and effects[0]["label"] == "place_market_order"


@pytest.mark.asyncio
async def test_polymarket_place_market_order_sell_maps_shares_to_adapter(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path / "runs"))
    place_market_order = AsyncMock(
        return_value=(
            True,
            {
                "status": "matched",
                "quote": {
                    "token_id": "tok_yes",
                    "side": "SELL",
                    "requested_amount": 10.0,
                    "filled_amount": 10.0,
                    "fully_fillable": True,
                    "average_price": 0.052,
                    "shares": 10.0,
                    "notional_usdc": 0.52,
                },
            },
        )
    )

    with (
        patch(_FIND_WALLET, AsyncMock(return_value=_WALLET)),
        patch(_GET_SIGN_CB, AsyncMock(return_value=(_SIGN_CB, _ADDR))),
        patch(_GET_HASH_CB, AsyncMock(return_value=(_HASH_CB, _ADDR))),
        patch(_GET_TYPED_CB, AsyncMock(return_value=(_TYPED_CB, _ADDR))),
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.place_market_order",
            new=place_market_order,
        ),
    ):
        out = await polymarket_place_market_order(
            wallet_label="main",
            token_id="tok_yes",
            side="SELL",
            sell_amount_shares=10.0,
        )
        assert out["ok"] is True
        assert place_market_order.await_args.kwargs["amount"] == 10.0
        assert out["result"]["sizing_kind"] == "sell_amount_shares"
        summary = out["result"]["executionSummary"]
        assert summary["inputAmountType"] == "shares"
        assert summary["requestedCollateral"] is None
        assert summary["requestedShares"] == 10.0
        assert summary["collateralReceived"] == 0.52
        assert summary["sharesFilled"] == 10.0
        assert summary["avgPrice"] == 0.052
        assert summary["fillRatio"] == 1.0


@pytest.mark.asyncio
async def test_polymarket_place_market_order_preview_uses_side_specific_size():
    buy = await build_polymarket_place_market_order_preview(
        {
            "wallet_label": "main",
            "market_slug": "market",
            "outcome": "YES",
            "side": "BUY",
            "buy_amount_pusd": 4,
        }
    )
    assert "BUY spend: 4 pUSD" in buy["summary"]
    assert "amount_collateral" not in buy["summary"]

    sell = await build_polymarket_place_market_order_preview(
        {
            "wallet_label": "main",
            "market_slug": "market",
            "outcome": "YES",
            "side": "SELL",
            "sell_amount_shares": 77,
        }
    )
    assert "SELL size: 77 shares" in sell["summary"]
    assert "amount_collateral" not in sell["summary"]


@pytest.mark.asyncio
async def test_polymarket_place_market_order_preview_hydrates_buy_quote():
    quote_market_order = AsyncMock(
        return_value=(
            True,
            {
                "token_id": "tok_yes",
                "side": "BUY",
                "fully_fillable": True,
                "average_price": 0.052,
                "best_price": 0.051,
                "worst_price": 0.053,
                "shares": 76.923,
                "notional_usdc": 4.0,
                "levels_consumed": 2,
                "price_impact_bps": 12.3,
            },
        )
    )
    with (
        patch(_PREVIEW_FIND_WALLET, AsyncMock(return_value=_WALLET)),
        patch(_PREVIEW_GET_TOKEN_BALANCE, AsyncMock(return_value=12_340_000)),
        patch(
            "wayfinder_paths.mcp.preview.PolymarketAdapter.get_market_by_slug",
            new=AsyncMock(
                return_value=(
                    True,
                    {
                        "slug": "market",
                        "question": "Will it happen?",
                        "outcomes": ["Yes", "No"],
                        "clobTokenIds": ["tok_yes", "tok_no"],
                    },
                )
            ),
        ),
        patch(
            "wayfinder_paths.mcp.preview.PolymarketAdapter.quote_market_order",
            new=quote_market_order,
        ),
    ):
        preview = await build_polymarket_place_market_order_preview(
            {
                "wallet_label": "main",
                "market_slug": "market",
                "outcome": "YES",
                "side": "BUY",
                "buy_amount_pusd": 4,
            }
        )

    summary = preview["summary"]
    assert "market: Will it happen?" in summary
    assert "resolved token_id: tok_yes" in summary
    assert f"deposit wallet: {derive_deposit_wallet(_ADDR)}" in summary
    assert "deposit pUSD balance: 12.34 pUSD" in summary
    assert "expected pUSD spent: 4 pUSD" in summary
    assert "expected shares: 76.923" in summary
    assert "avg price: 0.052" in summary
    assert "depth: fully fillable, levels consumed: 2" in summary
    assert "slippage cap: 2.0%" in summary
    assert quote_market_order.await_args.kwargs == {
        "token_id": "tok_yes",
        "side": "BUY",
        "amount": 4.0,
    }


@pytest.mark.asyncio
async def test_polymarket_place_market_order_preview_hydrates_direct_token_sell():
    quote_market_order = AsyncMock(
        return_value=(
            True,
            {
                "token_id": "tok_yes",
                "side": "SELL",
                "fully_fillable": True,
                "average_price": 0.052,
                "best_price": 0.053,
                "worst_price": 0.052,
                "shares": 10.0,
                "notional_usdc": 0.52,
                "levels_consumed": 1,
                "price_impact_bps": 0,
            },
        )
    )
    with (
        patch(_PREVIEW_FIND_WALLET, AsyncMock(return_value=_WALLET)),
        patch(_PREVIEW_GET_TOKEN_BALANCE, AsyncMock(return_value=9_000_000)),
        patch(
            "wayfinder_paths.mcp.preview.PolymarketAdapter.quote_market_order",
            new=quote_market_order,
        ),
    ):
        preview = await build_polymarket_place_market_order_preview(
            {
                "wallet_label": "main",
                "token_id": "tok_yes",
                "side": "SELL",
                "sell_amount_shares": 10,
                "max_slippage_pct": 1.5,
            }
        )

    summary = preview["summary"]
    assert "market: not hydrated (token_id provided directly)" in summary
    assert "SELL size: 10 shares" in summary
    assert "shares to sell: 10" in summary
    assert "expected pUSD received: 0.52 pUSD" in summary
    assert "slippage cap: 1.5%" in summary
    assert quote_market_order.await_args.kwargs["amount"] == 10.0


@pytest.mark.asyncio
async def test_polymarket_place_market_order_preview_warns_on_market_resolution_failure():
    with (
        patch(_PREVIEW_FIND_WALLET, AsyncMock(return_value=_WALLET)),
        patch(_PREVIEW_GET_TOKEN_BALANCE, AsyncMock(return_value=0)),
        patch(
            "wayfinder_paths.mcp.preview.PolymarketAdapter.get_market_by_slug",
            new=AsyncMock(return_value=(False, "not found")),
        ),
    ):
        preview = await build_polymarket_place_market_order_preview(
            {
                "wallet_label": "main",
                "market_slug": "missing",
                "outcome": "YES",
                "side": "BUY",
                "buy_amount_pusd": 4,
            }
        )

    assert "MARKET RESOLUTION FAILED: not found" in preview["summary"]
    assert "QUOTE UNAVAILABLE: no resolved token_id" in preview["summary"]


@pytest.mark.asyncio
async def test_polymarket_place_market_order_preview_warns_on_partial_depth():
    with (
        patch(_PREVIEW_FIND_WALLET, AsyncMock(return_value=_WALLET)),
        patch(_PREVIEW_GET_TOKEN_BALANCE, AsyncMock(return_value=5_000_000)),
        patch(
            "wayfinder_paths.mcp.preview.PolymarketAdapter.quote_market_order",
            new=AsyncMock(
                return_value=(
                    True,
                    {
                        "token_id": "tok_yes",
                        "side": "BUY",
                        "fully_fillable": False,
                        "average_price": 0.05,
                        "shares": 50,
                        "notional_usdc": 2.5,
                        "levels_consumed": 1,
                    },
                )
            ),
        ),
    ):
        preview = await build_polymarket_place_market_order_preview(
            {
                "wallet_label": "main",
                "token_id": "tok_yes",
                "side": "BUY",
                "buy_amount_pusd": 5,
            }
        )

    assert "INSUFFICIENT DEPTH / PARTIAL FILL" in preview["summary"]
    assert "fillRatio=0.5" in preview["summary"]
    assert "depth: partial" in preview["summary"]
