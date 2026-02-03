"""Golden tests for BorosAdapter parsing/quoting behavior.

These are meant to be stable regression tests during refactors (types/utils split).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.adapters.boros_adapter.adapter import BorosAdapter


@pytest.fixture
def mock_boros_client():
    return AsyncMock()


@pytest.fixture
def adapter(mock_boros_client):
    mock_config = {
        "strategy_wallet": {"address": "0x1234567890123456789012345678901234567890"},
        "boros_adapter": {},
    }
    with patch(
        "wayfinder_paths.adapters.boros_adapter.adapter.BorosClient",
        return_value=mock_boros_client,
    ):
        a = BorosAdapter(config=mock_config)
        a.boros_client = mock_boros_client
        return a


@pytest.mark.asyncio
async def test_quote_markets_for_underlying_golden(adapter, mock_boros_client):
    # Make tenor_days deterministic (quote_market uses current wall-clock time).
    adapter._time_to_maturity_days = lambda maturity_ts: (maturity_ts or 0) / 1000.0  # type: ignore[method-assign]

    mock_markets = [
        {
            "marketId": 2,
            "address": "0x2222",
            "tokenId": 3,
            "imData": {
                "symbol": "HYPERLIQUID-HYPE-USD",
                "underlying": "HYPE",
                "maturity": 2000,
                "tickStep": 7,
                "collateral": "0xUSDT",
            },
        },
        {
            "marketId": 1,
            "address": "0x1111",
            "tokenId": 3,
            "imData": {
                "symbol": "HYPERLIQUID-HYPE-USD",
                "underlying": "HYPE",
                "maturity": 1000,
                "tickStep": 5,
                "collateral": "0xUSDT",
            },
        },
        # Same underlying but wrong platform should be filtered out when platform is set.
        {
            "marketId": 3,
            "address": "0x3333",
            "tokenId": 3,
            "imData": {
                "symbol": "OTHER-HYPE-USD",
                "underlying": "HYPE",
                "maturity": 1500,
                "tickStep": 1,
                "collateral": "0xUSDT",
            },
            "metadata": {"platformName": "OTHER"},
            "platform": {"name": "OTHER"},
        },
    ]

    async def _get_order_book(market_id: int, tick_size: float = 0.001):
        if market_id == 1:
            return {"long": {"ia": [100, 110]}, "short": {"ia": [120, 130]}}
        if market_id == 2:
            return {"long": {"ia": [200]}, "short": {"ia": [250]}}
        return {"long": {"ia": [1]}, "short": {"ia": [2]}}

    mock_boros_client.list_markets = AsyncMock(return_value=mock_markets)
    mock_boros_client.get_order_book = AsyncMock(side_effect=_get_order_book)

    ok, quotes = await adapter.quote_markets_for_underlying(
        "HYPE", platform="hyperliquid", tick_size=0.001
    )
    assert ok is True

    # Only the two Hyperliquid-tagged markets should remain.
    assert [q.market_id for q in quotes] == [1, 2]

    q1 = quotes[0]
    assert q1.underlying == "HYPE"
    assert q1.symbol == "HYPERLIQUID-HYPE-USD"
    assert q1.maturity_ts == 1000
    assert q1.tenor_days == 1.0
    assert q1.tick_step == 5
    assert q1.best_bid_apr == 0.11
    assert q1.best_ask_apr == 0.12
    assert q1.mid_apr == pytest.approx(0.115)

    q2 = quotes[1]
    assert q2.maturity_ts == 2000
    assert q2.tenor_days == 2.0
    assert q2.tick_step == 7
    assert q2.best_bid_apr == 0.2
    assert q2.best_ask_apr == 0.25
    assert q2.mid_apr == pytest.approx(0.225)


@pytest.mark.asyncio
async def test_get_account_balances_isolated_market_id_golden(
    adapter, mock_boros_client
):
    # marketAcc parsing: last 6 hex chars represent the market id (3 bytes).
    market_acc = "0x" + ("0" * 58) + "000012"  # 0x12 == 18

    mock_boros_client.get_collaterals = AsyncMock(
        return_value={
            "collaterals": [
                {
                    "tokenId": 3,
                    "crossPosition": {"availableBalance": "100000000000000000000"},
                    "isolatedPositions": [
                        {
                            "availableBalance": "50000000000000000000",
                            "marketAcc": market_acc,
                        }
                    ],
                }
            ]
        }
    )

    ok, balances = await adapter.get_account_balances(token_id=3)
    assert ok is True
    assert balances["cross"] == 100.0
    assert balances["isolated"] == 50.0
    assert balances["total"] == 150.0
    assert balances["isolated_market_id"] == 18
    assert balances["isolated_positions"] == [
        {
            "market_id": 18,
            "balance": 50.0,
            "balance_wei": 50000000000000000000,
            "marketAcc": market_acc,
        }
    ]
