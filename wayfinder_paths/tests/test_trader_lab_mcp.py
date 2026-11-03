"""Trader Lab MCP tools: argument validation and compact output."""

from __future__ import annotations

import importlib
from unittest.mock import AsyncMock

import pytest

tools = importlib.import_module("wayfinder_paths.mcp.tools.trader_lab")

ENV = {"release": "2026-10-30", "as_of": "2026-10-29", "rules_version": "r"}


@pytest.fixture
def client(monkeypatch):
    fake = AsyncMock()
    monkeypatch.setattr(tools, "TRADER_LAB_CLIENT", fake)
    return fake


async def test_leaderboard_maps_args_and_trims_rows(client):
    client.leaderboard.return_value = {
        **ENV,
        "data": [
            {
                "address": "0xa",
                "sharpe": 3.14159265,
                "card": {"big": 1},
                "sparkline": [1] * 30,
            }
        ],
        "page": {"next": "c2"},
    }
    out = await tools.research_trader_lab_leaderboard(
        bucket="Dip_Buyer",
        sort="-net_mtm",
        include_hft="true",
        limit="5",
        min_days="40",
    )
    assert out["ok"], out
    client.leaderboard.assert_awaited_once_with(
        include_hft=True, limit=5, bucket="dip_buyer", sort="-net_mtm", min_days=40
    )
    row = out["result"]["rows"][0]
    assert row["sharpe"] == 3.142 and "sparkline" not in row and "card" not in row
    assert (
        out["result"]["next_cursor"] == "c2"
        and out["result"]["release"] == "2026-10-30"
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"bucket": "whales"},
        {"sort": "address"},
        {"limit": "500"},
        {"include_hft": "maybe"},
    ],
)
async def test_leaderboard_rejects_bad_args(client, kwargs):
    out = await tools.research_trader_lab_leaderboard(**kwargs)
    assert not out["ok"] and out["error"]["code"] == "invalid_argument"
    client.leaderboard.assert_not_awaited()


async def test_wallet_summarises_equity_and_adds_trips(client):
    client.wallet.return_value = {
        **ENV,
        "data": {
            "address": "0xa",
            "card": {"equity": [{"date": "d", "cum": 1.0}] * 300, "tags": ["maker"]},
        },
    }
    client.trips.return_value = {**ENV, "data": [{"coin": "ETH"}]}
    out = await tools.research_trader_lab_wallet("0xA")
    result = out["result"]
    assert result["equity_points"] == 300 and "equity" not in result["card"]
    assert result["recent_trips"] == [{"coin": "ETH"}]
    client.trips.assert_awaited_once_with("0xA", limit=20)


async def test_cohort_requires_view_specific_args(client):
    assert (await tools.research_trader_lab_cohort(view="flows"))["error"][
        "code"
    ] == "invalid_argument"
    assert (await tools.research_trader_lab_cohort(view="entry_states"))["error"][
        "code"
    ] == "invalid_argument"
    client.flows.return_value = {**ENV, "data": [{"date": str(i)} for i in range(80)]}
    out = await tools.research_trader_lab_cohort(view="flows", coin="BTC", days="7")
    assert len(out["result"]["rows"]) == 50 and out["result"]["rows"][-1] == {
        "date": "79"
    }
    client.flows.assert_awaited_once_with(coin="BTC", bucket=None, days=7)


async def test_query_caps_rows_and_parses_params(client):
    client.query.return_value = {
        **ENV,
        "data": {
            "columns": [{"name": "n", "type": "BIGINT"}],
            "rows": [[i] for i in range(120)],
            "truncated": False,
        },
        "usage": {"credits": 5000},
    }
    out = await tools.research_trader_lab_query(
        recipe="bars", params_json='{"coin": "ETH"}'
    )
    result = out["result"]
    assert (
        result["columns"] == ["n"]
        and result["shown"] == 50
        and result["truncated"] is True
    )
    client.query.assert_awaited_once_with(
        sql=None, recipe="bars", params={"coin": "ETH"}, mode="sync"
    )
    bad = await tools.research_trader_lab_query(recipe="bars", params_json="[1]")
    assert bad["error"]["code"] == "invalid_argument"


async def test_server_registers_every_tool():
    server = importlib.import_module("wayfinder_paths.mcp.server")
    names = {t.name for t in await server.mcp.list_tools()}
    assert {
        "research_trader_lab_meta",
        "research_trader_lab_leaderboard",
        "research_trader_lab_wallet",
        "research_trader_lab_cohort",
        "research_trader_lab_query",
        "research_trader_lab_query_status",
    } <= names
