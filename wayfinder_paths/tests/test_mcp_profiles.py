from __future__ import annotations

import json
from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP

from wayfinder_paths.mcp import server as mcp_server

SDK_ROOT = Path(__file__).resolve().parents[2]


def _tool_names(mcp: FastMCP) -> set[str]:
    return set(mcp._tool_manager._tools)


def _agent_frontmatter(agent_name: str) -> dict:
    text = _agent_text(agent_name)
    end = text.index("\n---", 4)
    return yaml.safe_load(text[4:end]) or {}


def _agent_text(agent_name: str) -> str:
    return (SDK_ROOT / ".opencode" / "agents" / f"{agent_name}.md").read_text(
        encoding="utf-8"
    )


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

    assert "onchain_swap" in names
    assert "onchain_send" in names
    assert "core_run_script" in names
    assert "core_runner" in names
    assert "core_web_search" in names
    assert "research_get_top_apy" in names
    assert "research_search_delta_lab_markets" in names
    assert "research_search_delta_lab_instruments" in names
    assert "research_get_delta_lab_pendle_market" in names
    assert "hyperliquid_get_trade_asset" in names
    assert "hyperliquid_place_market_order" in names
    assert "hyperliquid_deposit_usdc" in names
    assert "polymarket_place_market_order" in names
    assert "polymarket_deposit_pusd" in names
    assert "contracts_deploy" in names
    assert "visual_create_chart" not in names


def test_mcp_catalog_exposes_shells_tools_in_opencode(monkeypatch) -> None:
    monkeypatch.setattr(mcp_server, "is_opencode_instance", lambda: True)

    names = _tool_names(mcp_server.build_mcp())

    assert "visual_get_frontend_context" in names
    assert "visual_set_active_market" in names
    assert "visual_create_chart" in names
    assert "visual_import_chart_spec" in names
    assert "notification_send" in names


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
    assert primary["wayfinder_visual_*"] == "deny"
    assert primary["wayfinder_visual_get_frontend_context"] == "allow"
    assert primary["wayfinder_visual_set_active_market"] == "allow"
    assert primary["wayfinder_visual_search_chart_series"] == "allow"
    assert primary["wayfinder_visual_add_workspace_chart_annotation"] == "allow"
    assert primary["wayfinder_visual_add_workspace_chart_overlay"] == "allow"
    assert "wayfinder_visual_create_chart" not in primary
    assert "wayfinder_visual_import_chart_spec" not in primary
    assert primary["wayfinder_notification_send"] == "allow"
    assert primary["wayfinder_research_*"] == "deny"
    assert primary["wayfinder_core_run_script"] == "ask"
    assert primary["wayfinder_onchain_swap"] == "ask"
    assert primary["wayfinder_onchain_send"] == "ask"
    assert primary["wayfinder_contracts_execute"] == "ask"
    _assert_rule_order(primary, "wayfinder_*", "wayfinder_core_*")
    _assert_rule_order(primary, "wayfinder_core_*", "wayfinder_core_run_script")
    _assert_rule_order(
        primary,
        "wayfinder_visual_*",
        "wayfinder_visual_get_frontend_context",
    )
    _assert_rule_order(
        primary,
        "wayfinder_hyperliquid_*",
        "wayfinder_hyperliquid_place_*",
    )

    assert research["wayfinder_*"] == "deny"
    assert research["wayfinder_research_*"] == "allow"
    assert research["wayfinder_polymarket_read"] == "allow"
    assert "wayfinder_polymarket_place_*" not in research
    assert "wayfinder_polymarket_deposit_pusd" not in research
    assert research["wayfinder_core_get_adapters_and_strategies"] == "allow"
    assert research["wayfinder_core_run_script"] == "allow"
    assert research["wayfinder_core_web_search"] == "allow"
    assert research["wayfinder_core_web_fetch"] == "allow"
    _assert_rule_order(research, "wayfinder_*", "wayfinder_research_*")
    _assert_rule_order(research, "wayfinder_*", "wayfinder_polymarket_read")

    assert quant["wayfinder_*"] == "deny"
    assert quant["wayfinder_research_*"] == "allow"
    assert quant["wayfinder_core_get_adapters_and_strategies"] == "allow"
    assert quant["wayfinder_core_run_script"] == "allow"
    assert quant["wayfinder_core_web_search"] == "allow"
    assert quant["wayfinder_core_web_fetch"] == "allow"
    assert quant["wayfinder_polymarket_read"] == "allow"
    assert "wayfinder_polymarket_place_*" not in quant
    _assert_rule_order(quant, "wayfinder_*", "wayfinder_research_*")
    _assert_rule_order(quant, "wayfinder_*", "wayfinder_polymarket_read")

    assert visual["wayfinder_*"] == "deny"
    assert visual["wayfinder_visual_*"] == "allow"
    assert visual["wayfinder_core_run_script"] == "allow"
    assert visual["wayfinder_core_web_search"] == "allow"
    assert visual["wayfinder_core_web_fetch"] == "allow"
    _assert_rule_order(visual, "wayfinder_*", "wayfinder_visual_*")


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
        "wayfinder_visual_*": "deny",
        "wayfinder_visual_get_frontend_context": "allow",
        "wayfinder_visual_set_active_market": "allow",
        "wayfinder_visual_search_chart_series": "allow",
        "wayfinder_visual_add_workspace_chart_annotation": "allow",
        "wayfinder_visual_add_workspace_chart_overlay": "allow",
        "wayfinder_notification_send": "allow",
        "wayfinder_research_*": "deny",
        "wayfinder_core_run_script": "ask",
        "wayfinder_core_run_strategy": "ask",
        "wayfinder_core_runner": "ask",
        "wayfinder_onchain_swap": "ask",
        "wayfinder_onchain_send": "ask",
        "wayfinder_hyperliquid_place_*": "ask",
        "wayfinder_hyperliquid_cancel_order": "ask",
        "wayfinder_hyperliquid_update_leverage": "ask",
        "wayfinder_hyperliquid_deposit_usdc": "ask",
        "wayfinder_hyperliquid_withdraw_usdc": "ask",
        "wayfinder_polymarket_place_*": "ask",
        "wayfinder_polymarket_cancel_order": "ask",
        "wayfinder_polymarket_deposit_pusd": "ask",
        "wayfinder_polymarket_withdraw_pusd": "ask",
        "wayfinder_polymarket_redeem_positions": "ask",
        "wayfinder_contracts_deploy": "ask",
        "wayfinder_contracts_execute": "ask",
    }
    _assert_rule_order(primary, "wayfinder_*", "wayfinder_core_*")
    _assert_rule_order(primary, "wayfinder_core_*", "wayfinder_core_run_script")
    _assert_rule_order(primary, "wayfinder_contracts_*", "wayfinder_contracts_deploy")
    _assert_rule_order(
        primary,
        "wayfinder_visual_*",
        "wayfinder_visual_search_chart_series",
    )

    research_frontmatter = _agent_frontmatter("wayfinder-research")
    assert "tools" not in research_frontmatter
    research = research_frontmatter["permission"]
    assert {
        key: value for key, value in research.items() if key.startswith("wayfinder_")
    } == {
        "wayfinder_*": "deny",
        "wayfinder_research_*": "allow",
        "wayfinder_polymarket_read": "allow",
        "wayfinder_core_get_adapters_and_strategies": "allow",
        "wayfinder_core_run_script": "allow",
        "wayfinder_core_web_search": "allow",
        "wayfinder_core_web_fetch": "allow",
    }
    _assert_rule_order(research, "wayfinder_*", "wayfinder_research_*")
    _assert_rule_order(research, "wayfinder_*", "wayfinder_polymarket_read")

    quant_frontmatter = _agent_frontmatter("wayfinder-quant")
    assert "tools" not in quant_frontmatter
    quant = quant_frontmatter["permission"]
    assert {
        key: value for key, value in quant.items() if key.startswith("wayfinder_")
    } == {
        "wayfinder_*": "deny",
        "wayfinder_research_*": "allow",
        "wayfinder_core_get_adapters_and_strategies": "allow",
        "wayfinder_core_run_script": "allow",
        "wayfinder_core_web_search": "allow",
        "wayfinder_core_web_fetch": "allow",
        "wayfinder_polymarket_read": "allow",
    }
    _assert_rule_order(quant, "wayfinder_*", "wayfinder_research_*")
    _assert_rule_order(quant, "wayfinder_*", "wayfinder_polymarket_read")

    visual_frontmatter = _agent_frontmatter("wayfinder-visual")
    assert "tools" not in visual_frontmatter
    visual = visual_frontmatter["permission"]
    assert {
        key: value for key, value in visual.items() if key.startswith("wayfinder_")
    } == {
        "wayfinder_*": "deny",
        "wayfinder_visual_*": "allow",
        "wayfinder_core_run_script": "allow",
        "wayfinder_core_web_search": "allow",
        "wayfinder_core_web_fetch": "allow",
    }
    _assert_rule_order(visual, "wayfinder_*", "wayfinder_visual_*")


def test_opencode_agents_route_simple_onchain_token_charts_without_quant() -> None:
    primary = _agent_text("wayfinder")
    visual = _agent_text("wayfinder-visual")

    assert "Chart Fast Path" in primary
    assert "visual_get_frontend_context" in primary
    assert "visual_set_active_market" in primary
    assert "visual_search_chart_series" in primary
    assert "Do not call `wayfinder-quant`" in primary
    assert "simple iteration" in primary

    assert "Single-token chart fast path" in visual
    assert 'market_type="onchain-spot"' in visual
    assert "Do not call `visual_search_chart_series`" in visual
    assert "do not substitute a speculative perp or funding series" in visual


def test_visual_agent_prefers_source_refs_and_importable_specs() -> None:
    visual = _agent_text("wayfinder-visual")
    quant = _agent_text("wayfinder-quant")

    assert "Source References First" in visual
    assert "delta_lab.asset.lending" in visual
    assert '"market_id":17694' in visual
    assert '"asset_id":163' in visual
    assert "delta_lab.asset.funding" in visual
    assert '"instrument_id":163' in visual
    assert "visual_import_chart_spec" in visual
    assert ".wayfinder_runs/visual_specs" in visual
    assert "Empty task results are forbidden" in visual

    assert "Do not take over normal source-backed charting" in quant


def test_opencode_agents_route_research_and_polymarket_tasks() -> None:
    primary = _agent_text("wayfinder")
    research = _agent_text("wayfinder-research")

    assert "1-2 web calls" in primary
    assert "Delegate only when the task needs multi-source synthesis" in primary
    assert "exact dates and windows" in primary

    assert "Polymarket read-only: `polymarket_read`" in research
    assert "use `polymarket_read` first" in research
    assert "After two failed attempts" in research
    assert "Prediction-market research" in research
    assert "Prediction Market Forecast Mode" in research
    assert "priorSource" in research
    assert "normalized_order_book" in research
    assert "log_odds_update" in research
    assert "evidenceDeltas" in research
    assert "Token/Perp Research Mode" in research
    assert "perpSide" in research
    assert "positionIntent" in research
    assert "thesisPieces" in research


def test_market_intelligence_agent_prompt_contracts() -> None:
    primary = _agent_text("wayfinder")
    research = _agent_text("wayfinder-research")
    quant = _agent_text("wayfinder-quant")

    assert _agent_frontmatter("wayfinder-research")["temperature"] == 0.1
    assert _agent_frontmatter("wayfinder-quant")["temperature"] == 0.1

    assert "executable market/order-book distribution as the prior" in primary
    assert "Market Intelligence Modes" in primary
    assert "exact tool inputs" in primary

    assert "Prediction Market Forecast Mode" in research
    assert "Use the executable market/order-book distribution as the prior" in research
    assert "Market intelligence log" in research
    assert "logRefs" in research
    assert "artifactRefs" in research

    assert "Market Quant Mode" in quant
    assert "wayfinder_paths.quant.polymarket_edge" in quant
    assert "RESEARCH_ONLY" in quant
    assert "DO_NOT_TRADE" in quant


def test_hidden_opencode_subagents_do_not_emit_user_suggestions() -> None:
    for agent in ("wayfinder-research", "wayfinder-visual", "wayfinder-quant"):
        text = _agent_text(agent)

        assert "Do not emit `<userSuggestions>`" in text
        assert "do not call `userSuggestions`" in text


def test_claude_settings_reference_registered_tool_names(monkeypatch) -> None:
    monkeypatch.setattr(mcp_server, "is_opencode_instance", lambda: True)
    registry_names = _tool_names(mcp_server.build_mcp())
    permission_names = _claude_permission_names("allow") | _claude_permission_names(
        "ask"
    )

    assert permission_names <= registry_names

    pre_tool_hooks = _claude_settings()["hooks"]["PreToolUse"]
    for hook in pre_tool_hooks:
        matcher = hook["matcher"]
        if matcher.startswith("mcp__wayfinder__") and "(" not in matcher:
            assert matcher.removeprefix("mcp__wayfinder__") in registry_names
