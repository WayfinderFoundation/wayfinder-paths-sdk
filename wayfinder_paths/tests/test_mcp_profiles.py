from __future__ import annotations

import json
from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP

from wayfinder_paths.mcp import server as mcp_server
from wayfinder_paths.mcp import tool_registry

SDK_ROOT = Path(__file__).resolve().parents[2]


def _tool_names(mcp: FastMCP) -> set[str]:
    return set(mcp._tool_manager._tools)


def _agent_frontmatter(agent_name: str) -> dict:
    text = (SDK_ROOT / ".opencode" / "agents" / f"{agent_name}.md").read_text(
        encoding="utf-8"
    )
    end = text.index("\n---", 4)
    return yaml.safe_load(text[4:end]) or {}


def _agent_permission(agent_name: str) -> dict:
    return _agent_frontmatter(agent_name)["permission"]


def _claude_settings() -> dict:
    return json.loads((SDK_ROOT / ".claude" / "settings.json").read_text())


def _claude_permission_names(section: str) -> set[str]:
    settings = _claude_settings()
    permission_names = set()
    for full_name in settings["permissions"][section]:
        assert full_name.startswith("mcp__wayfinder__")
        permission_names.add(full_name.removeprefix("mcp__wayfinder__"))
    return permission_names


def _wayfinder_permission_keys(permission: dict) -> list[str]:
    return [key for key in permission if key.startswith("wayfinder_")]


def _assert_rule_order(permission: dict, first: str, second: str) -> None:
    keys = _wayfinder_permission_keys(permission)
    assert keys.index(first) < keys.index(second)


def test_mcp_catalog_exposes_expected_non_shell_tools() -> None:
    names = _tool_names(mcp_server.build_mcp())

    assert "core_execute" in names
    assert "core_run_script" in names
    assert "core_runner" in names
    assert "research_web_search" in names
    assert "research_get_top_apy" in names
    assert "research_search_delta_lab_markets" in names
    assert "research_search_delta_lab_instruments" in names
    assert "research_get_delta_lab_pendle_market" in names
    assert "hyperliquid_place_market_order" in names
    assert "hyperliquid_deposit" in names
    assert "polymarket_place_market_order" in names
    assert "polymarket_deposit" in names
    assert "contracts_deploy" in names
    assert "shells_create_chart" not in names


def test_mcp_catalog_exposes_shells_tools_in_opencode(monkeypatch) -> None:
    monkeypatch.setattr(tool_registry, "is_opencode_instance", lambda: True)

    names = _tool_names(mcp_server.build_mcp())

    assert "shells_get_frontend_context" in names
    assert "shells_set_active_market" in names
    assert "shells_create_chart" in names
    assert "shells_notify" in names


def test_opencode_agents_scope_single_mcp_tool_names() -> None:
    primary = _agent_permission("wayfinder")
    research = _agent_permission("wayfinder-research")
    quant = _agent_permission("wayfinder-quant")
    visual = _agent_permission("wayfinder-visual")

    assert primary["wayfinder_*"] == "deny"
    assert primary["wayfinder_core_*"] == "allow"
    assert primary["wayfinder_onchain_*"] == "allow"
    assert primary["wayfinder_hyperliquid_*"] == "allow"
    assert primary["wayfinder_polymarket_*"] == "allow"
    assert primary["wayfinder_contracts_*"] == "allow"
    assert primary["wayfinder_core_run_script"] == "ask"
    assert primary["wayfinder_core_execute"] == "ask"
    assert primary["wayfinder_contracts_execute"] == "ask"
    _assert_rule_order(primary, "wayfinder_*", "wayfinder_core_*")
    _assert_rule_order(primary, "wayfinder_core_*", "wayfinder_core_run_script")
    _assert_rule_order(
        primary,
        "wayfinder_hyperliquid_*",
        "wayfinder_hyperliquid_place_*",
    )

    for permission in (research, quant):
        assert permission["wayfinder_*"] == "deny"
        assert permission["wayfinder_research_*"] == "allow"
        assert permission["wayfinder_core_get_adapters_and_strategies"] == "allow"
        assert permission["wayfinder_core_run_script"] == "ask"
        _assert_rule_order(permission, "wayfinder_*", "wayfinder_research_*")

    assert visual["wayfinder_*"] == "deny"
    assert visual["wayfinder_shells_*"] == "allow"
    assert visual["wayfinder_core_run_script"] == "ask"
    _assert_rule_order(visual, "wayfinder_*", "wayfinder_shells_*")


def test_opencode_agent_frontmatter_scopes_visible_wayfinder_tools() -> None:
    primary = _agent_permission("wayfinder")
    assert {
        key: value for key, value in primary.items() if key.startswith("wayfinder_")
    } == {
        "wayfinder_*": "deny",
        "wayfinder_core_*": "allow",
        "wayfinder_onchain_*": "allow",
        "wayfinder_hyperliquid_*": "allow",
        "wayfinder_polymarket_*": "allow",
        "wayfinder_contracts_*": "allow",
        "wayfinder_core_execute": "ask",
        "wayfinder_core_run_script": "ask",
        "wayfinder_core_run_strategy": "ask",
        "wayfinder_core_runner": "ask",
        "wayfinder_hyperliquid_place_*": "ask",
        "wayfinder_hyperliquid_cancel_order": "ask",
        "wayfinder_hyperliquid_update_leverage": "ask",
        "wayfinder_hyperliquid_deposit": "ask",
        "wayfinder_hyperliquid_withdraw": "ask",
        "wayfinder_polymarket_place_*": "ask",
        "wayfinder_polymarket_cancel_order": "ask",
        "wayfinder_polymarket_deposit": "ask",
        "wayfinder_polymarket_withdraw": "ask",
        "wayfinder_polymarket_redeem_positions": "ask",
        "wayfinder_contracts_deploy": "ask",
        "wayfinder_contracts_execute": "ask",
    }
    _assert_rule_order(primary, "wayfinder_*", "wayfinder_core_*")
    _assert_rule_order(primary, "wayfinder_core_*", "wayfinder_core_run_script")
    _assert_rule_order(primary, "wayfinder_contracts_*", "wayfinder_contracts_deploy")

    for agent in ("wayfinder-research", "wayfinder-quant"):
        frontmatter = _agent_frontmatter(agent)
        assert "tools" not in frontmatter
        permission = frontmatter["permission"]
        assert {
            key: value
            for key, value in permission.items()
            if key.startswith("wayfinder_")
        } == {
            "wayfinder_*": "deny",
            "wayfinder_research_*": "allow",
            "wayfinder_core_get_adapters_and_strategies": "allow",
            "wayfinder_core_run_script": "ask",
        }
        _assert_rule_order(permission, "wayfinder_*", "wayfinder_research_*")

    visual_frontmatter = _agent_frontmatter("wayfinder-visual")
    assert "tools" not in visual_frontmatter
    visual = visual_frontmatter["permission"]
    assert {
        key: value for key, value in visual.items() if key.startswith("wayfinder_")
    } == {
        "wayfinder_*": "deny",
        "wayfinder_shells_*": "allow",
        "wayfinder_core_run_script": "ask",
    }
    _assert_rule_order(visual, "wayfinder_*", "wayfinder_shells_*")


def test_claude_settings_reference_registered_tool_names() -> None:
    registry_names = {
        tool_registry.tool_name(fn)
        for fn in tool_registry.tools_for_mcp(include_opencode_only=True)
    }
    permission_names = _claude_permission_names("allow") | _claude_permission_names(
        "ask"
    )

    assert permission_names <= registry_names

    pre_tool_hooks = _claude_settings()["hooks"]["PreToolUse"]
    for hook in pre_tool_hooks:
        matcher = hook["matcher"]
        if matcher.startswith("mcp__wayfinder__") and "(" not in matcher:
            assert matcher.removeprefix("mcp__wayfinder__") in registry_names


def test_claude_asks_for_registered_execution_and_schedule_tools() -> None:
    ask_names = _claude_permission_names("ask")
    sensitive_names = {
        tool_registry.tool_name(fn)
        for fn in tool_registry.tools_for_mcp(include_opencode_only=True)
        if tool_registry.tool_access(fn) in {"execute", "schedule"}
    }

    assert sensitive_names <= ask_names
