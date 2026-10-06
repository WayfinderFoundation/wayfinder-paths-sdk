from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_get_trade_results

_ADDRESS = "0x000000000000000000000000000000000000dEaD"


@pytest.mark.asyncio
async def test_hyperliquid_trade_results_maps_perp_period_and_compacts_fills() -> None:
    fills = [
        {
            "coin": "@107",
            "px": "1.2",
            "sz": "3",
            "side": "B",
            "time": 1_999_999,
            "closedPnl": "0",
            "fee": "0.01",
            "feeToken": "USDC",
            "oid": 1,
            "tid": 2,
            "hash": "0xspot",
        },
        {
            "coin": "BTC",
            "px": "100000",
            "sz": "0.01",
            "side": "A",
            "dir": "Close Long",
            "time": 2_000_000,
            "closedPnl": "25.5",
            "fee": "0.4",
            "feeToken": "USDC",
            "oid": 3,
            "tid": 4,
            "hash": "0xperp",
        },
    ]
    portfolios = [
        [
            "perpWeek",
            {
                "accountValueHistory": [[1, "100"], [2, "125"]],
                "pnlHistory": [[1, "0"], [2, "25"]],
                "vlm": "5000",
            },
        ]
    ]

    async def post(body):
        return fills if body["type"] == "userFillsByTime" else portfolios

    with (
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.resolve_wallet_address",
            new=AsyncMock(return_value=(_ADDRESS, "main")),
        ),
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.HYPERLIQUID_INFO_CLIENT.post",
            new=AsyncMock(side_effect=post),
        ) as mock_post,
        patch("wayfinder_paths.mcp.tools.hyperliquid.time.time", return_value=2000),
    ):
        result = await hyperliquid_get_trade_results(
            label="main", period="perpWeek", limit=10
        )

    assert result["ok"] is True
    assert result["result"]["summary"] == {
        "period": "perpWeek",
        "pnl": 25.0,
        "account_value": 125.0,
        "volume": 5000.0,
        "matching_trade_count": 1,
        "returned_trade_count": 1,
        "trade_history_may_be_truncated": False,
    }
    assert result["result"]["trades"] == [
        {
            "asset": "BTC",
            "direction": "Close Long",
            "side": "A",
            "price": 100000.0,
            "size": 0.01,
            "closed_pnl": 25.5,
            "fee": 0.4,
            "fee_token": "USDC",
            "timestamp_ms": 2_000_000,
            "order_id": 3,
            "trade_id": 4,
            "transaction_hash": "0xperp",
        }
    ]
    fill_call, portfolio_call = [call.args[0] for call in mock_post.await_args_list]
    assert fill_call == {
        "type": "userFillsByTime",
        "user": _ADDRESS,
        "startTime": 2_000_000 - 7 * 24 * 60 * 60 * 1000,
        "endTime": 2_000_000,
        "aggregateByTime": True,
    }
    assert portfolio_call == {"type": "portfolio", "user": _ADDRESS}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("period", "expected_start"),
    (("day", 2_000_000 - 24 * 60 * 60 * 1000), ("allTime", 0)),
)
async def test_hyperliquid_trade_results_maps_fill_window(
    period: str,
    expected_start: int,
) -> None:
    async def post(body):
        if body["type"] == "userFillsByTime":
            return []
        return [[period, {"accountValueHistory": [], "pnlHistory": [], "vlm": "0"}]]

    with (
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.resolve_wallet_address",
            new=AsyncMock(return_value=(_ADDRESS, "main")),
        ),
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.HYPERLIQUID_INFO_CLIENT.post",
            new=AsyncMock(side_effect=post),
        ) as mock_post,
        patch("wayfinder_paths.mcp.tools.hyperliquid.time.time", return_value=2000),
    ):
        result = await hyperliquid_get_trade_results(label="main", period=period)

    assert result["ok"] is True
    assert mock_post.await_args_list[0].args[0]["startTime"] == expected_start


@pytest.mark.asyncio
async def test_hyperliquid_trade_results_validates_period_and_limit() -> None:
    with patch(
        "wayfinder_paths.mcp.tools.hyperliquid.resolve_wallet_address",
        new=AsyncMock(return_value=(_ADDRESS, "main")),
    ):
        bad_period = await hyperliquid_get_trade_results(
            label="main",
            period="quarter",  # type: ignore[arg-type]
        )
        bad_limit = await hyperliquid_get_trade_results(label="main", limit=501)

    assert bad_period["error"]["code"] == "invalid_argument"
    assert bad_limit["error"]["code"] == "invalid_argument"
