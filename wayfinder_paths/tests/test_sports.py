from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import yaml

from wayfinder_paths.core.clients.SportsClient import (
    SportsClient,
    SportsGatewayAPIError,
)

REPO = Path(__file__).resolve().parents[2]


# ─── MCP registration ────────────────────────────────────────────────────────


def test_sports_tools_registered_without_endpoint_explosion() -> None:
    from wayfinder_paths.mcp.server import build_mcp

    names = {tool.name for tool in build_mcp()._tool_manager.list_tools()}

    assert "sports_snapshot" in names
    assert "sports_backtest_state" in names
    assert "sports_provider" in names

    # The facade is ONE tool over an allowlist -- never a per-endpoint blast.
    assert "sports_nba_get_games" not in names
    assert not any(
        n.startswith(("sports_nba", "sports_data_", "sports_lab_")) for n in names
    )
    # Exactly three sports_* tools.
    assert sorted(n for n in names if n.startswith("sports_")) == [
        "sports_backtest_state",
        "sports_provider",
        "sports_snapshot",
    ]


# ─── SportsClient request building ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_snapshot_builds_gateway_request() -> None:
    client = SportsClient()
    captured: dict = {}

    async def fake(method, url, *, json=None, **kwargs):
        captured.update(method=method, url=url, json=json)
        resp = MagicMock()
        resp.json.return_value = {"cards": []}
        return resp

    client._authed_request = fake  # type: ignore[assignment]
    await client.snapshot(action="scoreboard", sport="NBA", session_id="s1")

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/sports/snapshot/")
    assert captured["json"]["action"] == "scoreboard"
    assert captured["json"]["sport"] == "nba"  # lowercased
    assert captured["json"]["sessionID"] == "s1"


@pytest.mark.asyncio
async def test_provider_call_builds_gateway_request() -> None:
    client = SportsClient()
    captured: dict = {}

    async def fake(method, url, *, json=None, **kwargs):
        captured.update(url=url, json=json)
        resp = MagicMock()
        resp.json.return_value = {"data": {}}
        return resp

    client._authed_request = fake  # type: ignore[assignment]
    await client.provider_call(
        endpoint_id="data.games.list",
        sport="NBA",
        query={"per_page": 2},
        session_id="s",
    )

    assert captured["url"].endswith("/sports/provider/")
    assert captured["json"]["action"] == "call"
    assert captured["json"]["endpoint_id"] == "data.games.list"
    assert captured["json"]["query"] == {"per_page": 2}


@pytest.mark.asyncio
async def test_gateway_error_is_structured() -> None:
    client = SportsClient()
    request = httpx.Request("POST", "http://x/sports/provider/")
    response = httpx.Response(
        400,
        json={
            "error": {
                "type": "invalid_request",
                "code": "unknown_endpoint",
                "message": "not allowlisted",
            }
        },
        request=request,
    )

    async def fake(*args, **kwargs):
        raise httpx.HTTPStatusError("bad", request=request, response=response)

    client._authed_request = fake  # type: ignore[assignment]
    with pytest.raises(SportsGatewayAPIError) as exc_info:
        await client.provider_call(endpoint_id="https://evil", session_id="s")

    assert exc_info.value.code == "unknown_endpoint"
    assert exc_info.value.status_code == 400


# ─── Tool envelopes ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_provider_tool_rejects_bad_action() -> None:
    from wayfinder_paths.mcp.tools.sports import sports_provider

    result = await sports_provider(action="bogus", sessionID="s")
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_provider_tool_surfaces_gateway_rejection(monkeypatch) -> None:
    from wayfinder_paths.mcp.tools import sports as sports_tools

    monkeypatch.setattr(
        sports_tools.SPORTS_CLIENT,
        "provider_call",
        AsyncMock(
            side_effect=SportsGatewayAPIError(
                status_code=400,
                error_type="invalid_request",
                code="unknown_endpoint",
                message="not allowlisted",
            )
        ),
    )
    result = await sports_tools.sports_provider(
        action="call", endpoint_id="https://evil", sessionID="s"
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "unknown_endpoint"


@pytest.mark.asyncio
async def test_sports_tools_validate_json_objects_and_limits() -> None:
    from wayfinder_paths.mcp.tools.sports import sports_provider, sports_snapshot

    result = await sports_provider(
        action="call",
        endpoint_id="data.games.list",
        path_params="{bad json",
        sessionID="s",
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["details"]["field"] == "path_params"

    result = await sports_snapshot(
        action="scoreboard",
        sport="nba",
        limit="0",
        sessionID="s",
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["details"]["field"] == "limit"


@pytest.mark.asyncio
async def test_backtest_state_falls_back_to_mirror(monkeypatch, tmp_path) -> None:
    from wayfinder_paths.mcp.state import sports_state
    from wayfinder_paths.mcp.tools import sports as sports_tools

    monkeypatch.setenv("WAYFINDER_SPORTS_STATE_DIR", str(tmp_path))
    sports_state.upsert_runs(
        [
            {
                "run_id": "r1",
                "status": "evaluation",
                "sport": "nba",
                "updated": "2026-01-01",
            }
        ]
    )

    monkeypatch.setattr(
        sports_tools.SPORTS_CLIENT,
        "backtest_state",
        AsyncMock(
            side_effect=SportsGatewayAPIError(
                status_code=0,
                error_type="provider_failure",
                code="gateway_unavailable",
                message="down",
            )
        ),
    )
    result = await sports_tools.sports_backtest_state(action="list_active")
    assert result["ok"] is True
    assert result["result"]["source"] == "mirror"
    assert result["result"]["runs"][0]["run_id"] == "r1"


@pytest.mark.asyncio
async def test_sports_paginated_rows_follows_next_cursor() -> None:
    from wayfinder_paths.quant.sports_gateway import GatewayPacer, fetch_paginated_rows

    calls: list[dict] = []

    class Client:
        async def provider_call(self, **kwargs):
            calls.append(dict(kwargs["query"]))
            if len(calls) == 1:
                return {
                    "data": {
                        "data": [{"id": 1}],
                        "meta": {"next_cursor": "cursor-2"},
                    }
                }
            return {"data": {"data": [{"id": 2}], "meta": {}}}

    rows = await fetch_paginated_rows(
        Client(),
        GatewayPacer(0),
        endpoint_id="data.events.list",
        sport="nba",
        query={"per_page": 100},
        max_pages=5,
    )

    assert rows == [{"id": 1}, {"id": 2}]
    assert calls == [{"per_page": 100}, {"per_page": 100, "cursor": "cursor-2"}]


# ─── Permission wiring (provider-agnostic, least-privilege) ──────────────────


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    end = text.find("\n---\n", 4)
    return yaml.safe_load(text[4:end]) or {}


def test_primary_agent_gets_reads_not_facade() -> None:
    fm = _frontmatter(REPO / ".opencode" / "agents" / "wayfinder.md")
    perm = fm["permission"]
    assert perm["task"]["wayfinder-sports"] == "allow"
    assert perm["wayfinder_sports_snapshot"] == "allow"
    assert perm["wayfinder_sports_backtest_state"] == "allow"
    # The full facade is NOT granted to the primary (covered by the wayfinder_* deny).
    assert "wayfinder_sports_provider" not in perm
    assert perm["wayfinder_*"] == "deny"
    # REGRESSION (burned a live run): a wayfinder_sports_* deny glob in the .md gets
    # APPENDED after the json block's allows by the config merge and silently removes
    # the tools (last-match-wins). The glob must not exist here.
    assert "wayfinder_sports_*" not in perm


def test_research_agent_may_delegate_to_sports() -> None:
    fm = _frontmatter(REPO / ".opencode" / "agents" / "wayfinder-research.md")
    task = fm["permission"]["task"]
    # Allow must come after the catch-all deny (last matching rule wins in OpenCode).
    assert list(task.keys()).index("wayfinder-sports") > list(task.keys()).index("*")
    assert task["*"] == "deny"
    assert task["wayfinder-sports"] == "allow"
    # ...but research must NOT hold the sports tools directly (it delegates).
    assert "wayfinder_sports_provider" not in fm["permission"]
    body = (REPO / ".opencode" / "agents" / "wayfinder-research.md").read_text("utf-8")
    assert "wayfinder-sports" in body


def test_quant_agent_has_no_direct_sports_access() -> None:
    fm = _frontmatter(REPO / ".opencode" / "agents" / "wayfinder-quant.md")
    perm = fm["permission"]
    # No direct provider access: no sports tools, and no delegation to wayfinder-sports.
    assert not any("sports" in str(k) for k in perm)
    assert perm["task"] == {"*": "deny"}
    # ...but it knows how to consume a handed-over sports/backtest context pack.
    body = (REPO / ".opencode" / "agents" / "wayfinder-quant.md").read_text("utf-8")
    assert "context pack" in body.lower()


def test_sports_subagent_is_hidden_with_full_facade() -> None:
    fm = _frontmatter(REPO / ".opencode" / "agents" / "wayfinder-sports.md")
    assert fm["mode"] == "subagent"
    assert fm["hidden"] is True
    assert fm["steps"] == 16  # analysis/modelling workflows need fetch+script headroom
    perm = fm["permission"]
    assert perm["task"]["*"] == "deny"
    assert perm["wayfinder_*"] == "deny"
    assert perm["wayfinder_sports_snapshot"] == "allow"
    assert perm["wayfinder_sports_backtest_state"] == "allow"
    assert perm["wayfinder_sports_provider"] == "allow"
    # executable-board enumeration: read-only HIP-4 access (the second venue)
    assert perm["wayfinder_hyperliquid_search_market"] == "allow"
    assert perm["wayfinder_hyperliquid_search_mid_prices"] == "allow"


def test_sports_data_skill_exists_and_agent_references_it() -> None:
    skill = REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md"
    text = skill.read_text("utf-8")
    assert text.startswith("---") and "name: using-sports-data" in text
    # the catalog specifics the agent relies on
    for needle in (
        "data.player_props.list",
        "supported_leagues",
        "resource_unavailable_for_league",
        "market_edge",
        "sum to exactly 100",
        "player_ids",
        "Scripted analysis",
        "SPORTS_CLIENT",
    ):
        assert needle in text, f"skill missing: {needle}"
    agent = (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text("utf-8")
    assert "/using-sports-data" in agent
    assert "Which sports support what" in agent


def test_sports_subagent_prompt_states_key_rules() -> None:
    body = (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text("utf-8")
    lower = body.lower()
    assert "provider-agnostic" in lower
    assert "remote mcp" in lower  # never add a provider's remote MCP
    assert "run_id" in body  # stateful-run discipline
    # analyst capability: scripted fetch+manipulate+model, artifacts in the contract
    assert "SPORTS_CLIENT" in body
    assert "Data analysis & modelling" in body
    assert '"dataFiles": []' in body
    # the canned pipelines are the primary modelling paths (hand-rolling burned a live run)
    assert "wayfinder_paths.quant.prop_slate" in body
    assert "wayfinder_paths.quant.game_slate" in body
    # dislocated book-vs-Polymarket markets are adjudicated, never traded on trust
    assert "sports_posterior" in body
    assert "needs_adjudication" in body
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )
    assert "wayfinder_paths.quant.prop_slate" in skill
    assert "wayfinder_paths.quant.game_slate" in skill
    assert "sports_posterior" in skill


def test_dislocation_adjudication_wired_across_agents() -> None:
    """The posterior flow: primary hard rule, research book-fair card, doctrine prior."""
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")
    assert "Dislocation adjudication (HARD RULE)" in primary
    assert "what explains the cheap side?" in primary
    research = (REPO / ".opencode" / "agents" / "wayfinder-research.md").read_text(
        "utf-8"
    )
    assert "book_fair_evidence_card" in research
    assert "alreadyPriced" in research  # double-counting guard
    sports = (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text("utf-8")
    # prior doctrine: executable price is the prior; book enters as an evidence card
    assert "capped evidence card" in sports


def test_delegators_describe_sports_capabilities() -> None:
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")
    sports_section = primary.split("### wayfinder-sports", 1)[1]
    for needle in ("Data analysis & modelling", "futures", "xG", "modelling"):
        assert needle in sports_section, f"primary sports overview missing: {needle}"
    assert "most complete for NBA" not in primary  # stale capability claim

    research = (REPO / ".opencode" / "agents" / "wayfinder-research.md").read_text(
        "utf-8"
    )
    for needle in ("Analyze & model", "futures", "xG", "dataFiles"):
        assert needle in research, f"research sports overview missing: {needle}"


def test_opencode_json_registers_sports_perms() -> None:
    # opencode.json is a gitignored local/deploy artifact kept in sync with the agent
    # .md frontmatter (the tracked contract). Validate it when present; skip in CI.
    config_path = REPO / ".opencode" / "opencode.json"
    if not config_path.exists():
        pytest.skip("opencode.json not present (gitignored local/deploy artifact)")
    cfg = json.loads(config_path.read_text("utf-8"))
    agents = cfg["agent"]
    primary = agents["wayfinder"]["permission"]
    assert primary["wayfinder_sports_snapshot"] == "allow"
    assert primary["wayfinder_sports_backtest_state"] == "allow"
    assert "wayfinder_sports_provider" not in primary

    sports = agents["wayfinder-sports"]["permission"]
    assert sports["wayfinder_*"] == "deny"
    assert sports["wayfinder_sports_*"] == "allow"


def test_observed_failure_modes_are_ruled_out_in_prompts() -> None:
    """Each needle pins a rule added after a specific live failure."""
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )
    research = (REPO / ".opencode" / "agents" / "wayfinder-research.md").read_text(
        "utf-8"
    )
    sports = (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text("utf-8")
    # sub-threshold gaps are noise, never edge (a live run called one '3-5pp too rich')
    assert "VENUE NOISE" in primary and "lean within noise" in primary
    assert "VENUE NOISE" in skill
    # exact helper kwargs (a live research pass TypeError'd on bid=/ask=)
    assert "yes_bid=" in research and "implied_prior_from_quote(yes_bid=" in research
    # sport slug wrong-guess guidance (a live run tried fifa/fiba)
    for text in (skill, sports):
        assert "`fifa`/`fiba`" in text and "worldcup" in text


def test_round2_eval_losses_are_ruled_out_in_prompts() -> None:
    """Round-2 eval losses: numbers summarized away (NBA) and ask-instead-of-act (q2/q3)."""
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")
    assert "Show the numbers (composition rule)" in primary
    assert "Finish the method in-session (autonomy rule)" in primary
    sports = (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text("utf-8")
    assert "Include the rendered table itself" in sports


def test_information_vs_model_division_of_labor() -> None:
    """Pipelines own market math (correctness); modeling is the agent's judgment with
    the pipeline model demoted to a labeled reference opinion."""
    sports = (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text("utf-8")
    assert "MODELING is YOUR judgment" in sports
    assert "REFERENCE MODEL" in sports and "--data-only" in sports
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )
    assert "REFERENCE MODEL" in skill and "--data-only" in skill


def test_executable_board_enumeration_is_wired() -> None:
    """A user caught both eval arms ignoring Polymarket's 26-market per-game board
    while concluding 'nothing executable'."""
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")
    assert "Enumerate the executable BOARD" in primary
    assert "mlb-lad-cws-2026-06-12" in primary  # the slug pattern, by example
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )
    assert "Executable board rule" in skill and "alt_lines" in skill


def test_executable_first_funnel_is_wired() -> None:
    """User directive: start from the PM+HL boards and layer analysis on; deep-dive
    survivors with whatever data sharpens the number."""
    sports = (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text("utf-8")
    assert "ENUMERATE THE BOARDS (always step one)" in sports
    assert "DEEP-DIVE each survivor" in sports
    assert "answer IS the annotated board" in sports
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")
    assert "Betting questions START from the executable boards" in primary
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )
    assert "FUNNEL that starts from the executable boards" in skill


def test_utc_boundary_game_disambiguation_rule() -> None:
    """Round-4 eval loss: two same-matchup games under one UTC date filter were
    conflated — live odds of one vs the pre-game board of the other."""
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")
    assert "UTC-boundary trap" in primary
    assert "NEVER mix one game's live book odds" in primary
