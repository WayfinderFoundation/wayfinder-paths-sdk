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
    planner = _agent_permission("wayfinder-planner")
    research = _agent_permission("wayfinder-research")
    quant = _agent_permission("wayfinder-quant")
    visual = _agent_permission("wayfinder-visual")

    assert primary["task"]["wayfinder-planner"] == "allow"

    assert planner["*"] == "deny"
    assert planner["task"]["*"] == "deny"
    assert planner["question"] == "deny"
    assert planner["todowrite"] == "deny"
    assert planner["edit"] == "deny"
    assert planner["bash"] == "deny"
    assert planner["websearch"] == "deny"
    assert planner["webfetch"] == "deny"
    assert planner["wayfinder_*"] == "deny"
    assert planner["read"] == "allow"
    assert planner["grep"] == "allow"
    assert planner["glob"] == "allow"
    assert planner["list"] == "allow"
    assert "write" not in planner

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
        "wayfinder_sports_snapshot": "allow",
        "wayfinder_sports_backtest_state": "allow",
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
    assert "call `visual_set_active_market` directly" in primary
    assert "Delegate to `wayfinder-visual` only for workspace chart creation" in primary

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

    assert "Internal planning pass" in primary
    assert "wayfinder-planner" in primary
    assert "not as a hard gate" in primary
    assert "Skip `wayfinder-planner` for simple reads" in primary

    assert "1-2 web calls" in primary
    assert "Delegate only when the task needs multi-source synthesis" in primary
    assert "exact dates and windows" in primary

    assert "Polymarket read-only: `polymarket_read`" in research
    assert "use `polymarket_read` first" in research
    assert "After two failed attempts" in research
    assert "Prediction-market research" in research
    assert "Prediction Market Forecast Mode" in research
    assert "priorSource" in research
    assert "Last trade is context-only" in research
    assert "bid_ask_mid" in research
    assert "normalized_binary_prices" in research
    assert "order_book_sweep" in research
    assert "last_trade_context_only" in research
    assert "log_odds_update" in research
    assert "log_odds_evidence_update" in research
    assert "Build evidence cards" in research
    assert "evidenceDeltas" in research
    assert "evidenceCards" in research
    assert "conservative EV" in research
    assert "quote_update" in research
    assert "parentId" in research
    assert "relatedLogIds" in research
    assert "Identity guard" in research
    assert "exact venue symbol or market" in research
    assert "chain-scoped contract/token metadata" in research
    assert "Delta Lab asset/market result" in research
    assert "supporting-only" in research
    assert "openQuestions" in research
    assert "Standard task: 6-8 calls" in research
    assert "Deep task: 8-12 calls" in research
    assert "Target 6-8 high-utility calls" in research
    assert "Evidence-quality iteration gate" in research
    assert "partial_early_stop" in research
    assert "stoppedEarlyReason" in research
    assert "Known Context" in research
    assert "candidate_limit=20" in research
    assert "eventGroups" in research
    assert "nextSuggestedCalls" in research
    assert "Do not call `read` on `polymarket_edge.py`" in research
    assert "contextForNextAgent" in research
    assert "Market Research / Thesis Mode" in research
    assert "quick lookups" in research
    assert "do not force a thesis" in research
    assert "snapshot checks" in research
    assert "DeFi protocols" in research
    assert "yield routes" in research
    assert "perpSide" in research
    assert "positionIntent" in research
    assert "Only include `perpSide` and `positionIntent`" in research
    assert "changedFields" in research
    assert "effectOnThesis" in research


def test_market_intelligence_agent_prompt_contracts() -> None:
    primary = _agent_text("wayfinder")
    planner = _agent_text("wayfinder-planner")
    research = _agent_text("wayfinder-research")
    quant = _agent_text("wayfinder-quant")

    assert _agent_frontmatter("wayfinder-research")["temperature"] == 0.1
    assert _agent_frontmatter("wayfinder-quant")["temperature"] == 0.1

    assert "fresh executable pricing as the prior" in primary
    assert "quote/snapshot updates" in planner
    assert "audit_only" in planner
    assert "relatedLogIds" in planner
    assert "exact tool inputs" in primary
    assert "Balance/gas source of truth" in primary
    assert 'core_get_wallets(label="...")' in primary
    assert 'polymarket_get_state(wallet_label="...")' in primary
    assert "web3_from_chain_id(chain_id)" in primary
    assert "Do not use Polygonscan/Etherscan/BscScan/etc." in primary
    assert "Evidence-quality gate" in primary
    assert "partial_early_stop" in primary
    assert "weak/questionable evidence" in primary
    assert "buy_amount_pusd" in primary
    assert "sell_amount_shares" in primary
    assert "executionSummary.sharesFilled" in primary
    assert "wayfinder-research" in planner
    assert "stable yield/rates" in planner
    assert "positionIntent" in planner
    assert (
        'research_search_lending(sort="combined_net_supply_apr_now", basis="USD", limit="25")'
        in planner
    )
    assert 'research_get_basis_apy_sources(basis_symbol="USD", limit="100")' in planner
    assert "Treat `YIELD_TOKEN` as vault/LP/receipt-token yield" in planner
    assert "Token/Perp Research Mode" not in primary
    assert "thesisPieces" not in primary
    assert "Known Context Handoffs" in primary
    assert "contextForNextAgent" in primary
    assert "Do not drop known Polymarket event slugs" in primary
    assert "candidate_limit=20" in primary
    assert "surfaceLite" in primary
    assert "surfaceFull" in primary
    assert "resolutionRef" in primary
    assert "edge mode" in primary
    assert "mark_to_market_edge" in primary
    assert "Single Non-Sports Prediction Market Edge" in planner
    assert "World Cup Broad Outright Scan" in planner
    assert "Trade Setup / Short Candidate" in planner

    assert "Prediction Market Forecast Mode" in research
    assert "Use the executable market/order-book distribution as the prior" in research
    assert "stale log entries" in research
    assert "Market intelligence log" in research
    assert "Do not log every tool call" in research
    assert "logRefs" in research
    assert "artifactRefs" in research
    assert "researchStatus" in research
    assert "stoppedEarlyReason" in research
    assert "buy_amount_pusd" in research
    assert "sell_amount_shares" in research
    assert "executionSummary" in research
    assert "contextForNextAgent" in research
    assert "best stable APY/rates/yield" in research
    assert (
        'research_search_lending(sort="combined_net_supply_apr_now", basis="USD", limit="25")'
        in research
    )
    assert 'research_get_basis_apy_sources(basis_symbol="USD", limit="100")' in research
    assert "Treat `YIELD_TOKEN` as vault/LP/receipt-token yield" in research
    assert "surfaceLite" in research
    assert "profile != pm_simple_binary" in research
    assert "price equals probability" in research
    assert "exit/repricing probability" in research

    assert "Market Quant Mode" in quant
    assert "wayfinder_paths.quant.polymarket_edge" in quant
    assert "prediction_market_payoffs" in quant
    assert "polymarket_edge` is binary-only" in quant
    assert "profile != simple_binary" in quant
    assert "hl_mid_only" in quant
    assert "settlement_edge" in quant
    assert "exit-before-close EV" in quant
    assert "hypothesis seeds only" in quant
    assert "positive funding means longs pay shorts" in quant
    assert "RESEARCH_ONLY" in quant
    assert "DO_NOT_TRADE" in quant
    assert "Known Context" in quant
    assert "contextForNextAgent" in quant

    visual = _agent_text("wayfinder-visual")
    assert "Known Context" in visual
    assert "contextForNextAgent" in visual


def test_polymarket_deposit_wallet_skill_documents_async_boundaries() -> None:
    text = (
        SDK_ROOT
        / ".claude"
        / "skills"
        / "using-polymarket-adapter"
        / "rules"
        / "deposit-wallet.md"
    ).read_text(encoding="utf-8")

    assert "`adapter.deposit_wallet_address()` — **sync**" in text
    assert "Do not `await` it" in text
    assert "`await adapter.fund_deposit_wallet(amount_raw=int)` — **async**" in text
    assert (
        "`await adapter.withdraw_deposit_wallet(amount_raw=int | None)` — **async**"
        in text
    )


def test_polymarket_docs_use_side_specific_mcp_sizing() -> None:
    claude = (SDK_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    execution = (
        SDK_ROOT
        / ".claude"
        / "skills"
        / "using-polymarket-adapter"
        / "rules"
        / "execution-opportunities.md"
    ).read_text(encoding="utf-8")
    reads = (
        SDK_ROOT
        / ".claude"
        / "skills"
        / "using-polymarket-adapter"
        / "rules"
        / "high-value-reads.md"
    ).read_text(encoding="utf-8")
    gotchas = (
        SDK_ROOT
        / ".claude"
        / "skills"
        / "using-polymarket-adapter"
        / "rules"
        / "gotchas.md"
    ).read_text(encoding="utf-8")

    combined = "\n".join([claude, execution, reads, gotchas])
    assert "buy_amount_pusd" in combined
    assert "sell_amount_shares" in combined
    assert "executionSummary.sharesFilled" in combined
    assert "BUY size is pUSD spend" in claude
    assert "Do not reuse BUY spend as a share count" in gotchas


def test_primary_agent_warns_against_silent_similar_token_substitution() -> None:
    text = (SDK_ROOT / ".opencode" / "agents" / "wayfinder.md").read_text(
        encoding="utf-8"
    )

    assert "Do not silently substitute similar tokens or wrappers" in text
    assert "ETH ↔ WETH" in text
    assert "fresh quote and explicit user confirmation" in text


def test_stable_apy_research_and_adapter_docs_are_current() -> None:
    delta_high_value = (
        SDK_ROOT
        / ".claude"
        / "skills"
        / "using-delta-lab"
        / "rules"
        / "high-value-reads.md"
    ).read_text(encoding="utf-8")
    delta_gotchas = (
        SDK_ROOT / ".claude" / "skills" / "using-delta-lab" / "rules" / "gotchas.md"
    ).read_text(encoding="utf-8")
    hyperlend_reads = (
        SDK_ROOT
        / ".claude"
        / "skills"
        / "using-hyperlend-adapter"
        / "rules"
        / "high-value-reads.md"
    ).read_text(encoding="utf-8")
    avantis_reads = (
        SDK_ROOT
        / ".claude"
        / "skills"
        / "using-avantis-adapter"
        / "rules"
        / "high-value-reads.md"
    ).read_text(encoding="utf-8")
    morpho_reads = (
        SDK_ROOT
        / ".claude"
        / "skills"
        / "using-morpho-adapter"
        / "rules"
        / "high-value-reads.md"
    ).read_text(encoding="utf-8")

    assert (
        'research_search_lending(sort="combined_net_supply_apr_now", basis="USD", '
        'limit="25")'
    ) in delta_high_value
    assert (
        'research_get_basis_apy_sources(basis_symbol="USD", limit="100")'
        in delta_high_value
    )
    assert "`YIELD_TOKEN` rows are vault/LP/receipt-token yields" in delta_gotchas
    assert "HyperlendClient.get_stable_markets(chain_id" not in hyperlend_reads
    assert (
        "HyperlendAdapter.get_stable_markets(required_underlying_tokens?"
        in hyperlend_reads
    )
    assert "available_liquidity_tokens/usd" in hyperlend_reads
    assert "fetch_trailing_apy()" in avantis_reads
    assert "trailing APY" in avantis_reads
    assert 'm.get("loan", {}).get("symbol")' in morpho_reads
    assert '(m.get("state") or {}).get("supply_apy")' in morpho_reads


def test_hidden_opencode_subagents_do_not_emit_user_suggestions() -> None:
    for agent in (
        "wayfinder-planner",
        "wayfinder-research",
        "wayfinder-visual",
        "wayfinder-quant",
    ):
        text = _agent_text(agent)

        assert "Do not emit `<userSuggestions>`" in text
        assert "do not call `userSuggestions`" in text


def test_wayfinder_planner_is_hidden_advisory_and_non_mutating() -> None:
    frontmatter = _agent_frontmatter("wayfinder-planner")
    permission = frontmatter["permission"]
    text = _agent_text("wayfinder-planner")

    assert frontmatter["mode"] == "subagent"
    assert frontmatter["hidden"] is True
    assert frontmatter["steps"] == 8
    assert frontmatter["temperature"] == 0.1
    assert permission["*"] == "deny"
    assert permission["task"]["*"] == "deny"
    assert permission["question"] == "deny"
    assert permission["edit"] == "deny"
    assert permission["bash"] == "deny"
    assert permission["wayfinder_*"] == "deny"
    assert permission["read"] == "allow"
    assert permission["grep"] == "allow"
    assert permission["glob"] == "allow"
    assert permission["list"] == "allow"
    assert "write" not in permission

    assert "Return one JSON object only" in text
    assert '"recommendedFlow"' in text
    assert '"knownContextToPass"' in text
    assert '"packStrategy"' in text
    assert '"avoidOverkill"' in text
    assert '"stopConditions"' in text
    assert "do not let it delay a direct answer" not in text
    assert "You may inspect local prompt/skill files" in text
    assert "Do not inspect secrets or `.env` files" in text
    assert "Simple Sports Schedule" in text
    assert "Specific Game Lines" in text


def test_hidden_analysis_subagents_can_write_bounded_artifacts() -> None:
    for agent, artifact_dir in (
        ("wayfinder-research", ".wayfinder_runs/research/"),
        ("wayfinder-sports", ".wayfinder_runs/sports/"),
        ("wayfinder-quant", ".wayfinder_runs/quant/"),
    ):
        permission = _agent_permission(agent)
        text = _agent_text(agent)

        assert permission["write"] == "allow"
        assert permission["question"] == "deny"
        assert artifact_dir in text
        assert "Never edit repo-tracked source" in text
        assert "approval-gated" in text

    assert "write" not in _agent_permission("wayfinder-visual")


def test_primary_agent_integrates_or_reports_hidden_subagent_blockers() -> None:
    primary = _agent_text("wayfinder")

    assert "After delegating, integrate the returned artifacts/findings" in primary
    assert "pending tool" in primary
    assert "parent task running" in primary


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
