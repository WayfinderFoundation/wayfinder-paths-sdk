from __future__ import annotations

import json

import pytest

from wayfinder_paths.mcp.resources.catalog import guide_intent, list_intents
from wayfinder_paths.mcp.resources.discovery import (
    describe_adapter,
    describe_adapter_full,
    describe_strategy,
    describe_strategy_full,
    list_adapters,
    list_strategies,
)


@pytest.mark.asyncio
async def test_list_adapters_includes_hyperliquid():
    out = await list_adapters()
    result = json.loads(out)
    names = {i["name"] for i in result["adapters"]}
    assert "hyperliquid_adapter" in names
    assert result["detail_level"] == "route"


@pytest.mark.asyncio
async def test_list_strategies_includes_basis_and_boros():
    out = await list_strategies()
    result = json.loads(out)
    names = {i["name"] for i in result["strategies"]}
    assert "boros_hype_strategy" in names
    assert "basis_trading_strategy" in names
    assert result["detail_level"] == "route"


@pytest.mark.asyncio
async def test_describe_strategy_returns_compact_selection_view():
    out = await describe_strategy("boros_hype_strategy")
    result = json.loads(out)
    assert result["name"] == "boros_hype_strategy"
    assert result["detail_level"] == "select"
    assert "manifest" not in result
    assert "supported_actions" in result


@pytest.mark.asyncio
async def test_describe_strategy_full_returns_manifest_and_examples():
    out = await describe_strategy_full("boros_hype_strategy")
    result = json.loads(out)
    assert result["name"] == "boros_hype_strategy"
    assert isinstance(result.get("manifest"), dict)
    assert isinstance(result.get("examples"), dict)


@pytest.mark.asyncio
async def test_describe_adapter_returns_compact_view():
    out = await describe_adapter("hyperliquid_adapter")
    result = json.loads(out)
    assert result["name"] == "hyperliquid_adapter"
    assert result["detail_level"] == "select"
    assert "manifest" not in result
    assert "capabilities" in result


@pytest.mark.asyncio
async def test_describe_adapter_full_returns_manifest():
    out = await describe_adapter_full("hyperliquid_adapter")
    result = json.loads(out)
    assert result["name"] == "hyperliquid_adapter"
    assert isinstance(result.get("manifest"), dict)


@pytest.mark.asyncio
async def test_list_strategies_includes_status_field():
    out = await list_strategies()
    result = json.loads(out)
    strategies = result["strategies"]
    for strategy in strategies:
        assert "status" in strategy
        assert strategy["status"] in ("stable", "wip", "deprecated")
    stablecoin = next(
        (s for s in strategies if s["name"] == "stablecoin_yield_strategy"), None
    )
    assert stablecoin is not None
    assert stablecoin["status"] == "wip"


@pytest.mark.asyncio
async def test_intent_catalog_routes_to_balances():
    out = await list_intents()
    result = json.loads(out)
    intents = {entry["intent"] for entry in result["intents"]}
    assert "wallet_inspection" in intents

    guide = json.loads(await guide_intent("wallet_inspection"))
    next_steps = {entry["name"] for entry in guide["next_steps"]}
    assert "get_wallet_balances" in next_steps
