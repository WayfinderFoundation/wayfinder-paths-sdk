"""Tests for BorosClient response-shape handling."""

from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.adapters.boros_adapter.client import BorosClient
from wayfinder_paths.adapters.boros_adapter.utils import rate_from_tick


def test_client_defaults_to_current_boros_api_mount():
    client = BorosClient()

    assert client.base_url == "https://api-boros.pendle.finance/apis"
    assert client.endpoints["markets"] == "/v1/markets"
    assert client.endpoints["build_place_order_calldata"] == (
        "/v1/calldata-builder/agent/place-order"
    )


@pytest.mark.asyncio
async def test_get_market_extracts_from_results_list():
    client = BorosClient(base_url="https://example.invalid")
    client._http = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "results": [
                {"marketId": 50, "address": "0x50"},
                {
                    "marketId": 51,
                    "address": "0x51",
                    "metadata": {"assetSymbol": "HYPE"},
                },
            ],
            "total": 2,
            "skip": 0,
        }
    )

    market = await client.get_market(51)
    assert market["marketId"] == 51
    assert market["address"] == "0x51"


@pytest.mark.asyncio
async def test_get_market_falls_back_to_pagination():
    client = BorosClient(base_url="https://example.invalid")
    client._http = AsyncMock(side_effect=Exception("boom"))  # type: ignore[method-assign]

    page_0 = [{"marketId": i, "address": f"0x{i:02x}"} for i in range(1, 101)]
    page_1 = [{"marketId": 123, "address": "0x7b"}]
    client.list_markets = AsyncMock(side_effect=[page_0, page_1])  # type: ignore[method-assign]

    market = await client.get_market(123)
    assert market["marketId"] == 123
    assert market["address"] == "0x7b"


@pytest.mark.asyncio
async def test_list_markets_clamps_limit_to_200():
    client = BorosClient(base_url="https://example.invalid")
    client._http = AsyncMock(return_value={"results": []})  # type: ignore[method-assign]

    await client.list_markets(skip=0, limit=250, is_whitelisted=True)

    call = client._http.call_args_list[0]  # type: ignore[attr-defined]
    assert call.kwargs["params"]["limit"] == 200


@pytest.mark.asyncio
async def test_list_markets_page_uses_current_cursor_params():
    client = BorosClient(base_url="https://example.invalid")
    client._http = AsyncMock(  # type: ignore[method-assign]
        return_value={"results": [{"marketId": 74}], "resumeToken": "next"}
    )

    page = await client.list_markets_page(
        is_whitelisted=True,
        is_matured=False,
        limit=250,
        resume_token="resume",
    )

    assert page["results"][0]["marketId"] == 74
    call = client._http.call_args_list[0]  # type: ignore[attr-defined]
    assert call.args[:2] == ("GET", "/v1/markets")
    assert call.kwargs["params"] == {
        "limit": 200,
        "isUiWhitelisted": "true",
        "isMatured": "false",
        "resumeToken": "resume",
    }


@pytest.mark.asyncio
async def test_list_markets_skip_fills_requested_limit_across_cursor_pages():
    client = BorosClient(base_url="https://example.invalid")
    client._http = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {
                "results": [{"marketId": i} for i in range(1, 101)],
                "resumeToken": "next",
            },
            {
                "results": [{"marketId": i} for i in range(101, 201)],
                "resumeToken": None,
            },
        ]
    )

    markets = await client.list_markets(skip=50, limit=100, is_whitelisted=True)

    assert len(markets) == 100
    assert markets[0]["marketId"] == 51
    assert markets[-1]["marketId"] == 150


@pytest.mark.asyncio
async def test_get_market_uses_by_ids_endpoint():
    client = BorosClient(base_url="https://example.invalid")
    client._http = AsyncMock(  # type: ignore[method-assign]
        return_value={"results": [{"marketId": 74, "address": "0x74"}]}
    )

    market = await client.get_market(74)

    assert market["address"] == "0x74"
    call = client._http.call_args_list[0]  # type: ignore[attr-defined]
    assert call.args[:2] == ("GET", "/v1/markets/by-ids")
    assert call.kwargs["params"] == {"marketIds": "74"}


@pytest.mark.asyncio
async def test_get_order_book_uses_current_query_shape():
    client = BorosClient(base_url="https://example.invalid")
    client._http = AsyncMock(return_value={"long": {"ia": []}, "short": {"ia": []}})  # type: ignore[method-assign]

    await client.get_order_book(74, tick_size=0.001, include_amm=False)

    call = client._http.call_args_list[0]  # type: ignore[attr-defined]
    assert call.args[:2] == ("GET", "/v1/markets/order-book")
    assert call.kwargs["params"] == {
        "marketId": 74,
        "tickSize": 0.001,
        "includeAmm": "false",
    }


@pytest.mark.asyncio
async def test_get_collaterals_transforms_market_acc_infos_to_legacy_shape():
    client = BorosClient(
        base_url="https://example.invalid",
        user_address="0x1234567890123456789012345678901234567890",
    )
    client.get_market_acc_infos_by_root = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "results": [
                {
                    "marketAcc": "0x1234567890123456789012345678901234567890000003ffffff",
                    "totalCash": "1000000000000000000",
                    "netBalance": "1000000000000000000",
                    "positions": [
                        {
                            "marketId": 74,
                            "signedSize": "-2000000000000000000",
                            "side": 1,
                        }
                    ],
                },
                {
                    "marketAcc": "0x123456789012345678901234567890123456789000000300004a",
                    "totalCash": "3000000000000000000",
                    "netBalance": "3000000000000000000",
                    "positions": [],
                },
            ],
            "syncStatus": {"blockNumber": 1, "timestamp": 2},
        }
    )

    data = await client.get_collaterals()

    assert data["syncStatus"] == {"blockNumber": 1, "timestamp": 2}
    coll = data["collaterals"][0]
    assert coll["tokenId"] == 3
    assert coll["crossPosition"]["availableBalance"] == "1000000000000000000"
    assert coll["crossPosition"]["marketPositions"][0]["sizeWei"] == (
        "2000000000000000000"
    )
    assert coll["isolatedPositions"][0]["marketAcc"].endswith("00004a")


@pytest.mark.asyncio
async def test_current_calldata_builders_use_post_bodies():
    client = BorosClient(
        base_url="https://example.invalid",
        user_address="0x1234567890123456789012345678901234567890",
    )
    client._http = AsyncMock(return_value={"calldata": "0xabc"})  # type: ignore[method-assign]

    await client.build_deposit_calldata(
        token_id=3,
        amount_wei=1_000_000,
        market_id=0xFFFFFF,
    )
    await client.build_cash_transfer_calldata(
        market_id=74,
        amount_wei=10**18,
        is_deposit=False,
    )
    await client.build_place_order_calldata(
        market_acc="0x1234567890123456789012345678901234567890000003ffffff",
        market_id=74,
        side=1,
        size_wei=2 * 10**18,
        limit_tick=410,
        rate=0.041,
        tif=1,
        slippage=0.005,
    )

    deposit_call, transfer_call, order_call = client._http.call_args_list  # type: ignore[attr-defined]
    assert deposit_call.args[:2] == ("POST", "/v1/calldata-builder/user/deposit")
    assert deposit_call.kwargs["json_body"] == {
        "marketAcc": "0x1234567890123456789012345678901234567890000003ffffff",
        "amount": "1000000",
    }
    assert transfer_call.args[:2] == (
        "POST",
        "/v1/calldata-builder/agent/cash-transfer",
    )
    assert transfer_call.kwargs["json_body"]["direction"] == "ISOLATED_TO_CROSS"
    assert order_call.args[:2] == (
        "POST",
        "/v1/calldata-builder/agent/place-order",
    )
    assert order_call.kwargs["json_body"]["rate"] == 0.041
    assert "limitTick" not in order_call.kwargs["json_body"]


@pytest.mark.asyncio
async def test_place_order_builder_converts_limit_tick_to_rate():
    client = BorosClient(base_url="https://example.invalid")
    client._http = AsyncMock(return_value={"calls": []})  # type: ignore[method-assign]
    client.get_market = AsyncMock(  # type: ignore[method-assign]
        return_value={"imData": {"tickStep": 2}}
    )

    await client.build_place_order_calldata(
        market_acc="0x1234567890123456789012345678901234567890000003ffffff",
        market_id=74,
        side=1,
        size_wei=2 * 10**18,
        limit_tick=410,
        tif=1,
        slippage=0.005,
    )

    call = client._http.call_args_list[0]  # type: ignore[attr-defined]
    assert call.args[:2] == ("POST", "/v1/calldata-builder/agent/place-order")
    assert call.kwargs["json_body"]["rate"] == pytest.approx(rate_from_tick(410, 2))
    assert "limitTick" not in call.kwargs["json_body"]


@pytest.mark.asyncio
async def test_simulate_place_order_uses_current_endpoint():
    client = BorosClient(base_url="https://example.invalid")
    client._http = AsyncMock(return_value={"ok": True})  # type: ignore[method-assign]

    await client.simulate_place_order(
        market_acc="0x1234567890123456789012345678901234567890000003ffffff",
        market_id=74,
        side=1,
        size_wei=10**18,
        tif=1,
        rate=0.041,
        slippage=0.005,
    )

    call = client._http.call_args_list[0]  # type: ignore[attr-defined]
    assert call.args[:2] == ("POST", "/v1/simulations/place-order")
    assert call.kwargs["json_body"]["marketId"] == 74
    assert call.kwargs["json_body"]["rate"] == 0.041
    assert "limitTick" not in call.kwargs["json_body"]
