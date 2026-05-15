from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from wayfinder_paths.mcp import server as mcp_server
from wayfinder_paths.mcp import tool_registry


def _tool_names(mcp: FastMCP) -> set[str]:
    return set(mcp._tool_manager._tools)


def test_main_profile_exposes_execution_surface_only() -> None:
    names = _tool_names(mcp_server.build_mcp("main"))

    assert "core_execute" in names
    assert "hyperliquid_execute" in names
    assert "polymarket_execute" in names
    assert "contracts_deploy" in names
    assert "core_run_strategy" in names
    assert "core_runner" in names
    assert "core_run_script" in names

    assert "research_web_search" not in names
    assert "research_get_top_apy" not in names
    assert "shells_create_chart" not in names


def test_research_profile_exposes_research_and_scripts_without_live_execution() -> None:
    names = _tool_names(mcp_server.build_mcp("research"))

    assert "research_web_search" in names
    assert "research_goldsky_graphql" in names
    assert "research_get_top_apy" in names
    assert "core_get_adapters_and_strategies" in names
    assert "core_run_script" in names

    assert "core_execute" not in names
    assert "hyperliquid_execute" not in names
    assert "polymarket_execute" not in names
    assert "contracts_deploy" not in names
    assert "core_run_strategy" not in names
    assert "core_runner" not in names
    assert "shells_create_chart" not in names


def test_visual_profile_exposes_shells_tools_and_scripts_in_opencode(
    monkeypatch,
) -> None:
    monkeypatch.setattr(tool_registry, "is_opencode_instance", lambda: True)

    names = _tool_names(mcp_server.build_mcp("visual"))

    assert "shells_get_frontend_context" in names
    assert "shells_set_active_market" in names
    assert "shells_create_chart" in names
    assert "core_run_script" in names

    assert "research_web_search" not in names
    assert "core_execute" not in names
    assert "hyperliquid_execute" not in names
    assert "polymarket_execute" not in names
    assert "core_runner" not in names


def test_visual_profile_hides_shells_tools_outside_opencode() -> None:
    names = _tool_names(mcp_server.build_mcp("visual"))

    assert names == {"core_run_script"}


def test_default_mcp_keeps_legacy_all_profile() -> None:
    names = _tool_names(mcp_server.mcp)

    assert "core_execute" in names
    assert "research_web_search" in names
    assert "core_run_script" in names
    assert "shells_create_chart" not in names
