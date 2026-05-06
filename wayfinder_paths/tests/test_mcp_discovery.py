from __future__ import annotations

import pytest

from wayfinder_paths.mcp.tools.discovery import core_get_adapters_and_strategies


@pytest.mark.asyncio
async def test_full_catalog_includes_known_entries():
    out = await core_get_adapters_and_strategies()
    assert out["ok"] is True
    result = out["result"]
    adapter_names = {i["name"] for i in result["adapters"]}
    strategy_names = {i["name"] for i in result["strategies"]}
    assert "hyperliquid_adapter" in adapter_names
    assert "boros_hype_strategy" in strategy_names
    assert "basis_trading_strategy" in strategy_names


@pytest.mark.asyncio
async def test_entries_carry_manifest():
    out = await core_get_adapters_and_strategies()
    assert out["ok"] is True
    result = out["result"]
    for entry in result["adapters"] + result["strategies"]:
        assert isinstance(entry["manifest"], dict)


@pytest.mark.asyncio
async def test_filter_by_name_returns_single_entry():
    out = await core_get_adapters_and_strategies(name="boros_hype_strategy")
    assert out["ok"] is True
    result = out["result"]
    assert result["adapters"] == []
    assert len(result["strategies"]) == 1
    assert result["strategies"][0]["name"] == "boros_hype_strategy"

    out = await core_get_adapters_and_strategies(name="hyperliquid_adapter")
    assert out["ok"] is True
    result = out["result"]
    assert len(result["adapters"]) == 1
    assert result["adapters"][0]["name"] == "hyperliquid_adapter"
    assert result["strategies"] == []


@pytest.mark.asyncio
async def test_unknown_name_returns_error():
    out = await core_get_adapters_and_strategies(name="does_not_exist")
    assert out["ok"] is False
    assert out["error"]["code"] == "not_found"
