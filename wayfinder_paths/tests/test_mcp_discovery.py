from __future__ import annotations

import json

import pytest

from wayfinder_paths.mcp.resources.discovery import (
    describe_adapter,
    describe_strategy,
    list_adapters,
    list_strategies,
)


@pytest.mark.asyncio
async def test_list_adapters_includes_hyperliquid():
    out = await list_adapters()
    result = json.loads(out)
    names = {i["name"] for i in result["adapters"]}
    assert "hyperliquid_adapter" in names


@pytest.mark.asyncio
async def test_list_strategies_includes_basis_and_boros():
    out = await list_strategies()
    result = json.loads(out)
    names = {i["name"] for i in result["strategies"]}
    assert "boros_hype_strategy" in names
    assert "basis_trading_strategy" in names


@pytest.mark.asyncio
async def test_describe_strategy_returns_manifest_and_readme_excerpt():
    out = await describe_strategy("boros_hype_strategy")
    result = json.loads(out)
    assert result["name"] == "boros_hype_strategy"
    assert isinstance(result.get("manifest"), dict)


@pytest.mark.asyncio
async def test_describe_adapter_returns_manifest():
    out = await describe_adapter("hyperliquid_adapter")
    result = json.loads(out)
    assert result["name"] == "hyperliquid_adapter"
    assert isinstance(result.get("manifest"), dict)


@pytest.mark.asyncio
async def test_list_strategies_includes_status_field():
    out = await list_strategies()
    result = json.loads(out)
    strategies = result["strategies"]
    # All strategies should have a status field
    for s in strategies:
        assert "status" in s, f"Strategy {s['name']} missing status field"
        assert s["status"] in ("stable", "wip", "deprecated")
    # stablecoin_yield_strategy should be marked as wip
    stablecoin = next(
        (s for s in strategies if s["name"] == "stablecoin_yield_strategy"), None
    )
    assert stablecoin is not None, "stablecoin_yield_strategy not found"
    assert stablecoin["status"] == "wip"
