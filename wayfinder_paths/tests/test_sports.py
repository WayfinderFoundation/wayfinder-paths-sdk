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
@pytest.mark.parametrize("sport", ["fifa", "FIBA"])
async def test_snapshot_normalizes_worldcup_sport_aliases(sport: str) -> None:
    client = SportsClient()
    captured: dict = {}

    async def fake(method, url, *, json=None, **kwargs):
        captured.update(method=method, url=url, json=json)
        resp = MagicMock()
        resp.json.return_value = {"cards": []}
        return resp

    client._authed_request = fake  # type: ignore[assignment]
    await client.snapshot(action="scoreboard", sport=sport, session_id="s1")

    assert captured["json"]["sport"] == "worldcup"


@pytest.mark.asyncio
async def test_snapshot_sends_timezone_for_scoreboard_dates() -> None:
    client = SportsClient()
    captured: dict = {}

    async def fake(method, url, *, json=None, **kwargs):
        captured.update(method=method, url=url, json=json)
        resp = MagicMock()
        resp.json.return_value = {"cards": []}
        return resp

    client._authed_request = fake  # type: ignore[assignment]
    await client.snapshot(
        action="scoreboard",
        sport="MLB",
        date="2026-06-19",
        timezone="America/Toronto",
        session_id="s1",
    )

    assert captured["json"]["date"] == "2026-06-19"
    assert captured["json"]["timezone"] == "America/Toronto"


@pytest.mark.asyncio
async def test_snapshot_sends_canonical_event_filters() -> None:
    client = SportsClient()
    captured: dict = {}

    async def fake(method, url, *, json=None, **kwargs):
        captured.update(method=method, url=url, json=json)
        resp = MagicMock()
        resp.json.return_value = {"cards": []}
        return resp

    client._authed_request = fake  # type: ignore[assignment]
    await client.snapshot(
        action="player_props",
        sport="worldcup",
        event_id="10",
        match_id="10",
        fight_id="301",
        tournament_id="20",
        competitor_id="557",
        player_id="557",
        team_id="1",
        season="2026",
        prop_type="shots",
        market_type="race_winner",
        vendors="draftkings,fanduel",
        limit=20,
        offset=20,
        session_id="s1",
    )

    assert captured["json"] == {
        "action": "player_props",
        "sport": "worldcup",
        "sessionID": "s1",
        "event_id": "10",
        "match_id": "10",
        "fight_id": "301",
        "tournament_id": "20",
        "competitor_id": "557",
        "player_id": "557",
        "team_id": "1",
        "season": "2026",
        "prop_type": "shots",
        "market_type": "race_winner",
        "vendors": "draftkings,fanduel",
        "limit": 20,
        "offset": 20,
    }


@pytest.mark.asyncio
async def test_snapshot_sends_player_id_lists() -> None:
    client = SportsClient()
    captured: dict = {}

    async def fake(method, url, *, json=None, **kwargs):
        captured.update(method=method, url=url, json=json)
        resp = MagicMock()
        resp.json.return_value = {"cards": []}
        return resp

    client._authed_request = fake  # type: ignore[assignment]
    await client.snapshot(
        action="player_lookup",
        sport="worldcup",
        competitor_ids=["20", "21"],
        player_ids=["10", "11"],
        session_id="s1",
    )

    assert captured["json"]["competitor_ids"] == ["20", "21"]
    assert captured["json"]["player_ids"] == ["10", "11"]


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

    result = await sports_snapshot(
        action="player_props",
        sport="worldcup",
        offset="-1",
        sessionID="s",
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["details"]["field"] == "offset"


@pytest.mark.asyncio
async def test_snapshot_tool_forwards_canonical_filters(monkeypatch) -> None:
    from wayfinder_paths.mcp.tools import sports as sports_tools

    captured: dict = {}

    async def fake_snapshot(**kwargs):
        captured.update(kwargs)
        return {"cards": []}

    monkeypatch.setattr(sports_tools.SPORTS_CLIENT, "snapshot", fake_snapshot)
    result = await sports_tools.sports_snapshot(
        action="futures",
        sport="f1",
        event_id="9",
        tournament_id="20",
        competitor_id="44",
        season="2026",
        market_type="race_winner",
        vendors="draftkings",
        limit="5",
        offset="20",
        sessionID="s",
    )

    assert result["ok"] is True
    assert captured["event_id"] == "9"
    assert captured["tournament_id"] == "20"
    assert captured["competitor_id"] == "44"
    assert captured["season"] == "2026"
    assert captured["market_type"] == "race_winner"
    assert captured["vendors"] == "draftkings"
    assert captured["limit"] == 5
    assert captured["offset"] == 20


@pytest.mark.asyncio
async def test_snapshot_tool_defaults_player_props_limit(monkeypatch) -> None:
    from wayfinder_paths.mcp.tools import sports as sports_tools

    captured: dict = {}

    async def fake_snapshot(**kwargs):
        captured.update(kwargs)
        return {"cards": []}

    monkeypatch.setattr(sports_tools.SPORTS_CLIENT, "snapshot", fake_snapshot)
    result = await sports_tools.sports_snapshot(
        action="player_props",
        sport="worldcup",
        event_id="123",
        sessionID="s",
    )

    assert result["ok"] is True
    assert captured["limit"] == 20


@pytest.mark.asyncio
async def test_snapshot_tool_normalizes_player_id_lists(monkeypatch) -> None:
    from wayfinder_paths.mcp.tools import sports as sports_tools

    captured: dict = {}

    async def fake_snapshot(**kwargs):
        captured.update(kwargs)
        return {"cards": []}

    monkeypatch.setattr(sports_tools.SPORTS_CLIENT, "snapshot", fake_snapshot)
    result = await sports_tools.sports_snapshot(
        action="player_lookup",
        sport="worldcup",
        player_id="10, 11",
        competitor_ids=["20", "21"],
        sessionID="s",
    )

    assert result["ok"] is True
    assert captured["player_id"] is None
    assert captured["player_ids"] == ["10", "11"]
    assert captured["competitor_ids"] == ["20", "21"]


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
    assert perm["task"]["wayfinder-planner"] == "allow"
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
    assert fm["steps"] == 28  # analysis/modelling workflows need fetch+script headroom
    perm = fm["permission"]
    assert perm["task"]["*"] == "deny"
    assert perm["wayfinder_*"] == "deny"
    assert perm["wayfinder_sports_snapshot"] == "allow"
    assert perm["wayfinder_sports_backtest_state"] == "allow"
    assert perm["wayfinder_sports_provider"] == "allow"
    # executable-board enumeration: read-only HIP-4 access (the second venue)
    assert perm["wayfinder_hyperliquid_search_hip4"] == "allow"
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


def test_broad_prop_scans_prioritize_sports_markets_before_words() -> None:
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")
    primary_lower = primary.lower()
    assert "choose the betting lens before delegating" in primary_lower
    assert "announcer/broadcast" in primary_lower
    assert "prop bets / crossbets" in primary_lower
    assert "match outcomes/game lines" in primary_lower
    assert "visible player or team stat props" in primary_lower
    assert "goals/points/totals/bands" in primary_lower
    assert "secondary novelty bucket" in primary_lower
    assert "secondary means scan after sports props, not skip" in primary_lower
    assert "hydrate the top liquid/relevant event before a global prop conclusion" in primary_lower
    assert "a broad `no edge` claim is allowed only after surfaced categories" in primary_lower
    assert "no edge in match outcomes and liquid player props checked" in primary_lower
    assert "use `limit=20` by default" in primary_lower
    assert "page with `offset=20`" in primary_lower
    assert "bounded research/context pass" in primary_lower
    assert "research_state=not_hydrated" in primary_lower
    assert "market/odds-only" in primary_lower
    assert "deeper dual sports-data + research validation" in primary_lower
    assert "surfaced-but-unhydrated event slugs" in primary_lower
    assert "search_surfaced_unhydrated" in primary_lower
    assert "exact score" in primary_lower
    assert "do **not** stop after the first category" in primary_lower
    assert "which categories were scanned" in primary_lower
    assert "do **not** default to `game_slate`" in primary_lower
    assert "delegate only when statistical props" in primary_lower

    sports = (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text("utf-8")
    sports_lower = sports.lower()
    sports_compact = " ".join(sports.split())
    sports_compact_lower = sports_compact.lower()
    assert "broad prop / crossbet scan priority" in sports_lower
    assert "start with actual sports markets, not word/phrase markets" in sports_lower
    assert "visible player or team stat props" in sports_lower
    assert "goals/points/totals/bands" in sports_lower
    assert "announcer/broadcast words as a secondary novelty bucket" in sports_lower
    assert "secondary means scan after sports props, not skip" in sports_compact_lower
    assert "more-markets" in sports_lower
    assert "scanned / found / hydrated / skipped / not found / unavailable" in sports_lower
    assert "a broad `no edge` claim is allowed only after surfaced categories" in sports_lower
    assert "`player_props` reads should default to `limit=20`" in sports_lower
    assert "page with `offset=20`" in sports_lower
    assert "bounded context/research check" in sports_lower
    assert "research_state=not_hydrated" in sports_lower
    assert "market/odds-only" in sports_lower
    assert "deeper dual sports-data + research validation" in sports_lower
    assert "compare related prices" in sports_lower
    assert "unsupported \"true probability\" claims" in sports_compact

    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )
    skill_lower = skill.lower()
    skill_compact = " ".join(skill_lower.split())
    assert "broad prop/crossbet scans are sports-first" in skill_lower
    assert "match outcomes/game lines" in skill_lower
    assert "visible player/team stat props" in skill_lower
    assert "goals" in skill_lower and "points" in skill_lower
    assert "secondary novelty bucket" in skill_lower
    assert "do not stop at the first category" in skill_compact
    assert "categories scanned/found/hydrated" in skill_lower
    assert "a broad `no edge` claim is allowed only after surfaced" in skill_lower
    assert "default to `limit=20`" in skill_lower
    assert "`offset=20`" in skill_lower
    assert "bounded context/research check" in skill_lower
    assert "buy (heuristic)" in skill_lower
    assert "avoid unsupported true-prob claims" in skill_compact


def test_june_23_world_cup_prop_scan_no_edge_guard() -> None:
    """Regression for free-seeking-moon: the June 23 scan surfaced
    Portugal/Uzbekistan more-markets and announcer events, then claimed no edge
    after checking only match outcomes plus one player-prop board."""
    surfaced_categories = {
        "fifwc-prt-uzb-2026-06-23-more-markets": "more-markets",
        "what-will-the-announcers-say-during-england-vs-ghana-world-cup-match": "announcer/broadcast",
    }
    assert surfaced_categories

    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")
    sports = (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text(
        "utf-8"
    )
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )
    combined = "\n".join([primary, sports, skill]).lower()

    assert "more-markets" in combined
    assert "announcer/broadcast" in combined
    assert "hydrate the top liquid/relevant event before a global prop conclusion" in combined
    assert "a broad `no edge` claim is allowed only after surfaced categories" in combined
    assert "otherwise scope the claim" in combined
    assert "no edge in match outcomes and liquid player props checked" in combined
    assert "search_surfaced_unhydrated" in combined


def test_dislocation_adjudication_wired_across_agents() -> None:
    """The posterior flow: primary routes to skill; skill/research preserve doctrine."""
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")
    assert "/using-sports-data" in primary
    assert "adjudicate dislocations before calling value" in primary
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )
    assert "Dislocation rule" in skill
    assert "what explains the cheap side?" in skill
    assert "capped evidence card" in skill
    research = (REPO / ".opencode" / "agents" / "wayfinder-research.md").read_text(
        "utf-8"
    )
    assert "book_fair_evidence_card" in research
    assert "alreadyPriced" in research  # double-counting guard


def test_sports_scan_lens_uses_fair_value_delta_not_arbability() -> None:
    primary_paths = [
        REPO / ".opencode" / "agents" / "wayfinder.md",
        REPO / "evals" / "agent_overlays" / "sports_current" / "wayfinder.md",
        REPO
        / "evals"
        / "agent_overlays"
        / "sports_workpack_challenger"
        / "wayfinder.md",
    ]
    sports_paths = [
        REPO / ".opencode" / "agents" / "wayfinder-sports.md",
        REPO / "evals" / "agent_overlays" / "sports_current" / "wayfinder-sports.md",
        REPO
        / "evals"
        / "agent_overlays"
        / "sports_workpack_challenger"
        / "wayfinder-sports.md",
    ]
    skill_paths = [
        REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md",
        REPO
        / "evals"
        / "agent_overlays"
        / "sports_current"
        / "using-sports-data.SKILL.md",
        REPO
        / "evals"
        / "agent_overlays"
        / "sports_workpack_challenger"
        / "using-sports-data.SKILL.md",
    ]

    for path in primary_paths:
        text = path.read_text("utf-8")
        assert "fair-value delta shortlist" in text
        assert "hypothesized fair probability/range vs executable price" in text
        assert "not whether cross-venue arb is possible" in text
        assert "lack of a cross-venue arb is not a skip reason" in text
        assert "cross-venue board and value/fade shortlist" not in text

    for path in sports_paths:
        text = path.read_text("utf-8")
        compact = " ".join(text.split())
        assert "fair-value delta: hypothesized" in compact
        assert "Use PM/HL differences as" in compact
        assert "lack of cross-venue arb is not a skip reason" in compact

    for path in skill_paths:
        text = path.read_text("utf-8")
        compact = " ".join(text.split())
        assert "fair-value delta" in compact
        assert "hypothesized fair probability/range minus executable PM/HL price" in compact
        assert "Absence of a cross-venue arbitrage path is not a skip reason" in compact


def test_unmatched_sports_model_outputs_are_context_only() -> None:
    sports_paths = [
        REPO / ".opencode" / "agents" / "wayfinder-sports.md",
        REPO / "evals" / "agent_overlays" / "sports_current" / "wayfinder-sports.md",
        REPO
        / "evals"
        / "agent_overlays"
        / "sports_workpack_challenger"
        / "wayfinder-sports.md",
    ]
    skill_paths = [
        REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md",
        REPO
        / "evals"
        / "agent_overlays"
        / "sports_current"
        / "using-sports-data.SKILL.md",
        REPO
        / "evals"
        / "agent_overlays"
        / "sports_workpack_challenger"
        / "using-sports-data.SKILL.md",
    ]

    for path in sports_paths:
        text = path.read_text("utf-8")
        assert "Do not compare player-level model probabilities to team-level markets" in text
        assert "if the matching executable prop is absent, it is context-only" in text

    for path in skill_paths:
        text = path.read_text("utf-8")
        compact = " ".join(text.split())
        assert "A player anytime-goal probability is not an edge against a team moneyline" in compact
        assert "if the matching executable prop is absent" in compact
        assert "context-only / informational-only" in compact


def test_ghana_style_gap_regression_frames_fair_value_not_cross_venue_arb() -> None:
    """Regression for the June 23 Ghana case: PM 4.5%, HL 2.75%, context ~6.2%."""
    pm_price = 0.045
    hl_price = 0.0275
    fair_context = 0.062

    assert fair_context - hl_price > fair_context - pm_price
    assert round((fair_context - hl_price) * 100, 2) == 3.45

    combined = "\n".join(
        [
            (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8"),
            (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text(
                "utf-8"
            ),
            (
                REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md"
            ).read_text("utf-8"),
        ]
    )
    assert "fair-value delta" in combined
    assert "lack of a cross-venue arb is not a skip reason" in combined
    assert "Absence of a cross-venue arbitrage path is not a skip reason" in combined


def test_delegators_describe_sports_capabilities() -> None:
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")
    planner = (REPO / ".opencode" / "agents" / "wayfinder-planner.md").read_text(
        "utf-8"
    )
    sports_section = primary.split("### wayfinder-sports", 1)[1]
    for needle in ("futures", "xG", "custom sports modelling"):
        assert needle in sports_section, f"primary sports overview missing: {needle}"
    assert "Data analysis & modelling" not in sports_section
    assert "Sports edge scans" not in sports_section
    assert "most complete for NBA" not in primary  # stale capability claim

    for needle in ("sports edge scans", "eventStatePack", "final fair-value range"):
        assert needle in planner, f"planner sports routing missing: {needle}"

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
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )
    research = (REPO / ".opencode" / "agents" / "wayfinder-research.md").read_text(
        "utf-8"
    )
    sports = (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text("utf-8")
    # sub-threshold gaps are noise, never edge (a live run called one '3-5pp too rich')
    assert "VENUE NOISE" in skill and "lean within noise" in skill
    # exact helper kwargs (a live research pass TypeError'd on bid=/ask=)
    assert "yes_bid=" in research and "implied_prior_from_quote(yes_bid=" in research
    # sport slug wrong-guess guidance (a live run tried fifa/fiba)
    for text in (skill, sports):
        assert "`fifa`/`fiba`" in text and "worldcup" in text


def test_round2_eval_losses_are_ruled_out_in_prompts() -> None:
    """Round-2 eval losses: numbers summarized away (NBA) and ask-instead-of-act (q2/q3)."""
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")
    assert "show the numbers" in primary
    assert "finish the\nmethod in-session" in primary
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )
    assert "Composition and autonomy rules" in skill
    assert "Finish the executable-venue check" in skill
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
    assert "enumerate whole boards" in primary
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )
    assert "Executable board rule" in skill and "alt_lines" in skill
    assert "mlb-lad-cws-2026-06-12" in skill  # the slug pattern, by example


def test_executable_first_funnel_is_wired() -> None:
    """User directive: start from the PM+HL boards and layer analysis on; deep-dive
    survivors with whatever data sharpens the number."""
    sports = (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text("utf-8")
    planner = (REPO / ".opencode" / "agents" / "wayfinder-planner.md").read_text(
        "utf-8"
    )
    assert "ENUMERATE THE BOARDS (always step one)" in sports
    assert "DEEP-DIVE each survivor" in sports
    assert "answer IS the annotated board" in sports
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")
    assert "enumerate whole boards on PM/HL" in primary
    assert "PM/HL surfacePack" in planner
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )
    assert "FUNNEL that starts from the executable boards" in skill


def test_utc_boundary_game_disambiguation_rule() -> None:
    """Round-4 eval loss: two same-matchup games under one UTC date filter were
    conflated — live odds of one vs the pre-game board of the other."""
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")
    assert "UTC-boundary game" in primary
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )
    assert "UTC-boundary trap" in skill
    assert "NEVER mix one game's live book odds" in skill


def test_path_event_market_workflow_lives_in_sports_skill() -> None:
    """Keep path-dependent sports workflow out of the primary prompt boilerplate."""
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )
    sports = (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text("utf-8")
    quant = (REPO / ".opencode" / "agents" / "wayfinder-quant.md").read_text("utf-8")
    research = (REPO / ".opencode" / "agents" / "wayfinder-research.md").read_text(
        "utf-8"
    )

    assert "/using-sports-data" in primary
    assert "minimum complete workflow" not in primary
    assert "poetry run python -m wayfinder_paths.quant.event_sim" not in primary
    assert "Path-dependent event markets" in skill
    assert "wayfinder_paths.quant.event_sim" in skill
    assert "custom simulator" in skill
    assert "clean_unplayed" in skill and "post_result_stale" in skill
    assert "eventStatePack" in sports and "missingPathFields" in sports
    assert "simulationPack" in quant and "NEEDS_MORE_STATE" in quant
    assert "evidence cards only" in research


def test_path_event_market_target_outcomes_are_wired() -> None:
    """Anti-overfit guard: path markets can be promotion/reach/stage markets,
    not just trophy winner markets."""
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )
    sports = (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text("utf-8")
    quant = (REPO / ".opencode" / "agents" / "wayfinder-quant.md").read_text("utf-8")

    for needle in ("champion", "slot", "reach_match", "match_winner"):
        assert needle in skill
        assert needle in quant
    assert "winner-take-all overfitting" in skill
    assert "promotion/relegation" in skill
    assert "target outcome" in sports


def test_path_field_scans_require_full_board_before_drilldown() -> None:
    """GPT-5.5 eval failure: the new agent found a couple of executable order
    books, but failed to answer the requested full trophy-market scan."""
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )

    for needle in (
        "annotated board before",
        "board coverage counts",
        "ranked top-candidate table",
        "path-model status",
        "Never finish with only one or two selected order books",
    ):
        assert needle in skill


def test_path_markets_shortlist_before_second_stage_simulation() -> None:
    """Broad path scans should answer now, then validate shortlisted candidates."""
    skill_paths = [
        REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md",
        REPO / "evals" / "agent_overlays" / "sports_current" / "using-sports-data.SKILL.md",
        REPO
        / "evals"
        / "agent_overlays"
        / "sports_workpack_challenger"
        / "using-sports-data.SKILL.md",
    ]

    for path in skill_paths:
        skill = path.read_text("utf-8")
        compact = " ".join(skill.split())
        for needle in (
            "Default first-pass workflow",
            "Return the desk-analyst board and value/fade shortlist before any full path model",
            "Run the first-pass board now",
            "Full path simulation is the second-stage validation",
            "state that simulation is not yet run",
            "pathAssumption",
            "missingPathFields",
        ):
            assert needle in compact


def test_default_sports_eval_questions_cover_current_battery() -> None:
    eval_script = (REPO / "scripts" / "eval_sports_ab.sh").read_text("utf-8")

    for needle in (
        "match markets, group winners, and who will win the trophy",
        "Saudi Arabia, Austria, and Jordan",
        "moneyline and estimate fair spreads and point/goal totals",
        "PM/HL as the executable betting surface",
        "creative supporting data beyond just the World Cup dataset",
        "provider sportsbook odds as optional context only",
        "Melissa Mullins vs Bia Mesquita",
        "do not invent odds, stats, or a recommendation",
    ):
        assert needle in eval_script

    assert "Canada vs Bosnia" not in eval_script


def test_sports_eval_judge_scores_unsupported_and_estimated_lines() -> None:
    judge = (REPO / "scripts" / "eval_sports_ab_judge.md").read_text("utf-8")

    for needle in (
        "Unsupported sports/data trick questions",
        "reporting unavailable coverage",
        "invented fight odds",
        "Estimated spreads/totals",
        "separated from executable PM/HL lines",
        "point/goal totals",
    ):
        assert needle in judge


def test_sports_eval_judge_rewards_executable_market_math_not_sportsbook_gates() -> (
    None
):
    judge = (REPO / "scripts" / "eval_sports_ab_judge.md").read_text("utf-8")

    for needle in (
        "Executable market math",
        "PM/HL order-book prices are the executable surface",
        "preserve multi-outcome mappings",
        "sportsbook context is used",
        "not required",
        "sportsbook_context_optional",
        "executable_market_math",
    ):
        assert needle in judge

    assert "De-vig correctness" not in judge
    assert "sportsbook_vendors" not in judge


def test_sports_eval_judge_rewards_first_pass_before_simulation() -> None:
    judge = (REPO / "scripts" / "eval_sports_ab_judge.md").read_text("utf-8")

    for needle in (
        "Path/simulation discipline",
        "first build an\n   executable board",
        "use or clearly reserve bracket/state simulation as second-stage validation",
        "simulation not yet run",
        "does not claim final fair value",
        "World Cup countries/outrights",
        "opinionated value/fade shortlist",
        "HYPE/SPCX short setup",
        "conditions before adjacent ideas",
        "when directly requested",
    ):
        assert needle in judge


def test_sports_eval_harness_rejects_partial_handoff_answers() -> None:
    eval_script = (REPO / "scripts" / "eval_sports_ab.sh").read_text("utf-8")
    eval_script_lower = eval_script.lower()

    for needle in (
        "validate_harvested_answer",
        "extract_final_from_log",
        "DB harvest invalid",
        "FINAL ANSWER",
        "checkpoint/handoff answer",
        "Continue if you have next steps",
        "EVAL_IDLE_TIMEOUT",
        "idle timeout observed",
        "EVAL_ONLY_INDEXES",
        "Reserve one call for current",
        "generous limit",
        "home/draw/away",
        "compact TTL'd PM/HL surfacePack",
        "surfacePackRefs",
        "Do not use\nprogress-only headings",
        "Critical Context",
        "final answer observed before checkpoint marker",
        "LIKE '%final answer%'",
    ):
        assert needle in eval_script
    assert "use at most 16 external tool calls" in eval_script_lower


def test_sports_skill_requires_exact_market_hydration_and_bounded_scans() -> None:
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )

    for needle in (
        "Exact-market hydration rule",
        'never conclude "no',
        "immediately hydrate it with `get_event`",
        "bounded fallback queries",
        "each competitor/team surname",
        "same-card/same-date",
        "truncation-prone discovery only",
        "not negative proof",
        "still search the direct matchup on PM",
        "Under a hard tool budget",
        "cap primary-agent collection at **sixteen external calls**",
        "Reserve one\ncall for current state/results",
        "generous `limit`",
        "Prioritize hydrating or directly using group boards",
        "search_surfaced_unhydrated",
        "World Cup: `worldcup`, not `soccer`",
        "Missing category coverage is a finding",
        "rather than checkpointing",
    ):
        assert needle in skill

    assert "Do not call" in skill
    assert "`mid_prices`" in skill
    assert "every encoded outcome in a large field" in skill.replace("\n", " ")


def test_primary_agent_has_enough_steps_for_broad_sports_scans() -> None:
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")
    sports = (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text("utf-8")
    quant = (REPO / ".opencode" / "agents" / "wayfinder-quant.md").read_text("utf-8")
    research = (REPO / ".opencode" / "agents" / "wayfinder-research.md").read_text(
        "utf-8"
    )

    assert "steps: 38" in primary
    assert "steps: 28" in sports
    assert "steps: 22" in quant
    assert "steps: 14" in research


def test_primary_routes_broad_sports_scans_through_ttl_surface_pack() -> None:
    """Observed q1 failure: the primary spent its budget enumerating venues and
    checkpointed before synthesis. Broad scans should share one TTL'd odds
    surface before sports/quant work instead of making every worker re-fetch."""
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")
    planner = (REPO / ".opencode" / "agents" / "wayfinder-planner.md").read_text(
        "utf-8"
    )
    sports = (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text("utf-8")
    quant = (REPO / ".opencode" / "agents" / "wayfinder-quant.md").read_text("utf-8")

    for needle in (
        "For broad sports scans",
        "ask `wayfinder-planner` for the workflow",
        "one shared executable PM/HL surface pack",
        "surfacePackRefs",
        "Do not make every subagent re-fetch the same odds board",
    ):
        assert needle in primary

    for needle in (
        "Broad edge scans across sports",
        "Prefer one shared `surfacePack`",
        "sports edge scans",
        "`wayfinder-sports` for modelling/context",
        "`wayfinder-quant` only for decision/validation",
        "Always include explicit stop conditions",
        "ttlSeconds: 60",
        "ttlSeconds: 30",
        "ttlSeconds: 300",
        "do not enumerate every outcome in the primary",
    ):
        assert needle in planner

    for needle in (
        "For broad multi-category scans",
        "coverage counts by executable venue/category",
        "`missingModelArtifact`",
        "instead of a progress checkpoint",
        "Never return a progress checkpoint",
        "consume those PM/HL executable surfaces",
        "Do not re-fetch the same PM/HL board",
    ):
        assert needle in sports

    for needle in (
        "surfacePackRefs",
        "use unexpired\nPM/HL bid/ask/mid/depth rows",
        "targeted refresh request",
    ):
        assert needle in quant


def test_sports_surface_pack_ttl_and_resume_contract_is_explicit() -> None:
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")
    planner = (REPO / ".opencode" / "agents" / "wayfinder-planner.md").read_text(
        "utf-8"
    )
    sports = (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text("utf-8")
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )

    for text in (planner, sports, skill):
        assert "ttlSeconds: 60" in text
        assert "ttlSeconds: 30" in text
        assert "surfacePackRefs" in text

    for needle in (
        "resume the next missing step",
        "incomplete_fair_value",
        "not `BUY`",
    ):
        assert needle in primary

    assert "resume from those pack refs" in skill
    assert "packRefs" in sports


def test_sports_worker_hyperliquid_tool_contract_is_explicit() -> None:
    """Sports prediction-market searches should use the HIP-4 filter to avoid
    bloated perp/spot payloads."""
    sports = (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text("utf-8")
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )

    for needle in (
        "You have eight tools",
        'wayfinder_hyperliquid_search_hip4(query="world cup", limit=15)',
        "perps/spots",
        "compact rows",
        "include_details=true",
        "shortlisted `#...` assets",
        "never infer paired asset ids",
    ):
        assert needle in sports

    for text in (primary, skill):
        assert "wayfinder_hyperliquid_search_hip4" in text
        assert "perps/spots" in text

    assert "Do not pass extra filters such as `market_type`" not in sports


def test_broad_scan_budget_reserves_state_and_unhydrated_coverage() -> None:
    """Observed q1 loss: the answer called surfaced group/match markets unavailable
    and skipped current World Cup state. The bounded plan must preserve both."""
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )
    sports = (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text("utf-8")

    for text in (skill, sports):
        assert "search_surfaced_unhydrated" in text
        assert "worldcup" in text

    assert "candidate coverage to surface multiple event slugs" in skill
    assert "one for match-market mids" in skill
    assert "Never output a progress checkpoint" in skill
    assert "Critical Context" in skill
    assert "Do not classify a category as absent" in sports


def test_sports_prop_hydration_uses_pm_child_events_and_sports_worker_enrichment() -> None:
    """Observed CH-CAN failure: the parent PM event had only moneyline markets,
    while hundreds of props lived under child events. Player identity/stat
    enrichment should stay in the sports worker."""
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")
    sports = (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text("utf-8")
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )

    for needle in (
        "Player lookup, competitor-id hydration, player props, and player/team prop enrichment belong to `wayfinder-sports`",
        "use returned `sportsBoard`, `childEvents`, and `categorySummary`",
        "For sportsbook/statistical `player_props`, delegate to `wayfinder-sports`",
    ):
        assert needle in primary

    for needle in (
        "consume that executable board before searching again",
        "child events often hold player props/specials",
        "do not say \"no Polymarket props\" until surfaced child",
    ):
        assert needle in sports

    for needle in (
        "board may be split across parent and child events",
        "use `sportsBoard`, `childEvents`, and\n`categorySummary`",
        "page candidates with `offset`",
    ):
        assert needle in skill

    assert "Polymarket lists mostly **game-level / outcome** markets" not in sports


def test_multi_outcome_sports_boards_are_not_binary_collapsed() -> None:
    """Observed q1 loss: HL match markets were treated as binary favorite/no-favorite
    markets even though soccer boards include an explicit draw outcome."""
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )
    sports = (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text("utf-8")

    for text in (skill, sports):
        assert "home/draw/away" in text
        assert "draw is" in text.lower() or "three-way soccer board" in text.lower()
        assert (
            "never infer paired asset ids" in text.lower()
            or "do not derive sibling asset ids" in text.lower()
        )

    assert "buy No on the favorite" in skill


def test_sports_skill_does_not_block_on_script_auth_failures() -> None:
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )

    for needle in (
        "Local sports scripts are optional accelerators, not blockers",
        "script_auth_unavailable",
        "missingModelArtifact",
        "Never turn a script-auth failure into a checkpoint",
        "still return an `eventStatePack`",
        "`futures_slate` or\nsportsbook futures fail auth",
    ):
        assert needle in skill


def test_canonical_live_smoke_script_is_gateway_mediated() -> None:
    script = (REPO / "scripts" / "sports_canonical_live_smoke.py").read_text("utf-8")

    assert "SPORTS_CLIENT.snapshot" in script
    assert "https://api.balldontlie.io" not in script
    for status in ("pass", "empty_ok", "auth_scope_blocked", "schema_error"):
        assert status in script
    assert '"provider_misconfigured"' in script
    assert "or exc.code in AUTH_CODES" in script
    for needle in ("worldcup", "mma", "atp", "wta", "f1", "pga", "event_id"):
        assert needle in script


def test_sports_prompts_do_not_require_sportsbook_futures_for_event_state_pack() -> (
    None
):
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )
    sports = (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text("utf-8")
    quant = (REPO / ".opencode" / "agents" / "wayfinder-quant.md").read_text("utf-8")

    for text in (skill, sports):
        assert "PM/HL" in text
        assert "not block this pack" in text or "still return the pack" in text
        assert "script_auth_unavailable" in text
        assert "missingModelArtifact" in text

    assert "do not treat sportsbook odds in the pack as executable or required" in quant
    assert "MUST run the matching pipeline FIRST" not in sports


def test_path_market_answers_require_multi_model_distillation() -> None:
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")
    planner = (REPO / ".opencode" / "agents" / "wayfinder-planner.md").read_text(
        "utf-8"
    )
    sports = (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text("utf-8")
    quant = (REPO / ".opencode" / "agents" / "wayfinder-quant.md").read_text("utf-8")
    judge = (REPO / "scripts" / "eval_sports_ab_judge.md").read_text("utf-8")

    for text in (skill, planner, sports, quant, judge):
        assert (
            "latest sim" in text
            or "latest simulator" in text
            or "simulator output" in text
        )
        assert "final fair value" in text

    for text in (skill, planner, sports):
        assert "PM/HL prior" in text or "PM/HL priors" in text
        assert "qualitative evidence" in text

    assert "workflow selection lives in `wayfinder-planner`" in primary

    assert "diagnostic_only" in skill
    assert "approx_bracket" in skill
    assert "market-implied" in sports
    assert "RESEARCH_ONLY" in quant


def test_balanced_rigor_budget_ladder_is_declared() -> None:
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")
    planner = (REPO / ".opencode" / "agents" / "wayfinder-planner.md").read_text(
        "utf-8"
    )
    quant = (REPO / ".opencode" / "agents" / "wayfinder-quant.md").read_text("utf-8")

    for needle in (
        "Balanced Rigor Budget",
        "Tier 0",
        "Tier 1",
        "Tier 2",
        "Tier 3",
        "Tier 4",
        "one repair",
    ):
        assert needle in primary

    for needle in (
        '"budgetTier"',
        '"maxExternalCalls"',
        '"allowedSubagents"',
        '"scriptPolicy"',
        "smoke simulation",
        "one repair max",
    ):
        assert needle in planner

    assert "one failed run plus one repair" in quant


def test_trader_first_pass_is_behavior_first_not_forced_template() -> None:
    primary_paths = [
        REPO / ".opencode" / "agents" / "wayfinder.md",
        REPO / "evals" / "agent_overlays" / "sports_current" / "wayfinder.md",
        REPO / "evals" / "agent_overlays" / "sports_workpack_challenger" / "wayfinder.md",
    ]

    for path in primary_paths:
        text = path.read_text("utf-8")
        for needle in (
            "Trader First Pass",
            "desk-analyst first pass",
            "behavior, not a fixed template",
            "do not force rigid taxonomies",
            "Return 1-3 concrete",
            "sports_state=not_hydrated",
            "simulation on the shortlist as second-stage validation",
            "fair-value delta shortlist",
            "hypothesized fair probability/range vs executable price",
        ):
            assert needle in text


def test_world_cup_outright_planner_shortlists_before_simulation() -> None:
    planner = (REPO / ".opencode" / "agents" / "wayfinder-planner.md").read_text(
        "utf-8"
    )

    for needle in (
        '"rigorTier": 3',
        '"budgetTier": "tier3_broad_scan"',
        "market board + bounded sports/research context -> shortlist -> optional simulation",
        "no full simulation until after shortlist",
        "PM/HL country surfacePack",
        "first-pass value/fade shortlist",
        "optional event_sim validation on shortlisted candidates",
        "eventStatePack only after shortlist",
    ):
        assert needle in planner


def test_sports_tool_unavailable_path_fails_fast_to_market_board() -> None:
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")

    for needle in (
        "Fail fast if sports tools are unavailable",
        "do not repeatedly retry the same invalid call",
        "do not debug ad hoc `/tmp` scripts",
        "sports_state=not_hydrated",
        "one repair max",
        "Continue from executable PM/HL surfaces",
    ):
        assert needle in primary


def test_market_intel_historical_analog_is_second_stage_validation() -> None:
    prompt_paths = [
        REPO / ".opencode" / "agents" / "wayfinder.md",
        REPO / ".opencode" / "agents" / "wayfinder-quant.md",
        REPO / "evals" / "agent_overlays" / "sports_current" / "wayfinder.md",
        REPO / "evals" / "agent_overlays" / "sports_current" / "wayfinder-quant.md",
        REPO / "evals" / "agent_overlays" / "sports_workpack_challenger" / "wayfinder.md",
        REPO
        / "evals"
        / "agent_overlays"
        / "sports_workpack_challenger"
        / "wayfinder-quant.md",
    ]

    for path in prompt_paths:
        text = path.read_text("utf-8")
        assert "second-stage validation" in text
        assert "first-pass" in text
        assert "entry" in text and "invalidation" in text and "risk" in text


def test_polymarket_research_prompt_preserves_token_ids_for_depth_calls() -> None:
    prompt_paths = [
        REPO / ".opencode" / "agents" / "wayfinder-research.md",
        REPO / "evals" / "agent_overlays" / "sports_current" / "wayfinder-research.md",
        REPO
        / "evals"
        / "agent_overlays"
        / "sports_workpack_challenger"
        / "wayfinder-research.md",
    ]

    for path in prompt_paths:
        text = path.read_text("utf-8")
        assert "Carry `outcomes[].tokenId`" in text
        assert "handoffs, and compaction" in text
        assert "prefer the exact `token_id`" in text
        assert "Never pass loose natural labels" in text
        assert "rehydrate with `search`/`get_event` first" in text


def test_path_market_research_evidence_must_be_structured_before_quant() -> None:
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")
    sports = (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text("utf-8")
    research = (REPO / ".opencode" / "agents" / "wayfinder-research.md").read_text(
        "utf-8"
    )
    quant = (REPO / ".opencode" / "agents" / "wayfinder-quant.md").read_text("utf-8")
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )

    for text in (primary, sports, research, quant, skill):
        assert "contextPack" in text
        assert "modelModifiers" in text
        assert "final-synthesis-only" in text

    assert "after the first shortlist" in primary
    assert "after a first shortlist/model pass" in skill
    assert "do not imply" in primary
    assert "do not claim the simulator consumed it" in sports


def test_research_influence_pack_is_flexible_and_ledgered() -> None:
    primary_paths = [
        REPO / ".opencode" / "agents" / "wayfinder.md",
        REPO / "evals" / "agent_overlays" / "sports_current" / "wayfinder.md",
        REPO / "evals" / "agent_overlays" / "sports_workpack_challenger" / "wayfinder.md",
    ]
    research_paths = [
        REPO / ".opencode" / "agents" / "wayfinder-research.md",
        REPO / "evals" / "agent_overlays" / "sports_current" / "wayfinder-research.md",
        REPO
        / "evals"
        / "agent_overlays"
        / "sports_workpack_challenger"
        / "wayfinder-research.md",
    ]
    sports_paths = [
        REPO / ".opencode" / "agents" / "wayfinder-sports.md",
        REPO / "evals" / "agent_overlays" / "sports_current" / "wayfinder-sports.md",
        REPO
        / "evals"
        / "agent_overlays"
        / "sports_workpack_challenger"
        / "wayfinder-sports.md",
    ]
    quant_paths = [
        REPO / ".opencode" / "agents" / "wayfinder-quant.md",
        REPO / "evals" / "agent_overlays" / "sports_current" / "wayfinder-quant.md",
        REPO
        / "evals"
        / "agent_overlays"
        / "sports_workpack_challenger"
        / "wayfinder-quant.md",
    ]
    skill_paths = [
        REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md",
        REPO / "evals" / "agent_overlays" / "sports_current" / "using-sports-data.SKILL.md",
        REPO
        / "evals"
        / "agent_overlays"
        / "sports_workpack_challenger"
        / "using-sports-data.SKILL.md",
    ]

    for path in primary_paths:
        text = path.read_text("utf-8")
        assert "researchInfluencePack" in text
        assert "not a prerequisite for the research to matter" in text
        assert "research consumption ledger" in text
        assert "accepted, rejected, and deferred" in text
        assert "deskOverride" in text
        assert "must not silently overwrite executable market priors" in text

    for path in research_paths:
        text = path.read_text("utf-8")
        assert "researchInfluencePack" in text
        assert "researcherOpinion" in text
        assert "influenceHints" in text
        assert "unsupported markets into modifier slots" in text
        assert "not mandatory math" in text
        assert "deskOverride" in text
        assert "override candidate" in text

    for path in sports_paths:
        text = path.read_text("utf-8")
        assert "researchInfluencePack" in text
        assert "researcherOpinion" in text
        assert "influenceHints" in text
        assert "deskOverride" in text
        assert "final-synthesis-only" in text

    for path in quant_paths:
        text = path.read_text("utf-8")
        assert "researchInfluencePack" in text
        assert "consume it before starting overlapping" in text
        assert "consumption ledger" in text
        assert "accepted, rejected, and" in text
        assert "deskOverride" in text

    for path in skill_paths:
        text = path.read_text("utf-8")
        assert "researchInfluencePack" in text
        assert "researcherOpinion" in text
        assert "influenceHints" in text
        assert "visible desk override candidate" in text
        assert "accepted, rejected, or deferred" in text


def test_path_market_sim_requires_validation_before_full_run() -> None:
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")
    planner = (REPO / ".opencode" / "agents" / "wayfinder-planner.md").read_text(
        "utf-8"
    )
    sports = (REPO / ".opencode" / "agents" / "wayfinder-sports.md").read_text("utf-8")
    quant = (REPO / ".opencode" / "agents" / "wayfinder-quant.md").read_text("utf-8")
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )

    for text in (primary, planner, quant, skill):
        assert "smoke" in text
        assert "NEEDS_MORE_STATE" in text

    for needle in (
        "missing group slots",
        "impossible wildcard slots",
        "unknown participants",
        "unsupported target shapes",
    ):
        assert needle in skill

    assert "validation-friendly" in sports
    assert "wildcard counts must match" in sports


def test_grounded_eval_judge_uses_current_hyperliquid_search_contract() -> None:
    judge = (REPO / ".opencode" / "agents" / "wayfinder-eval-judge.md").read_text(
        "utf-8"
    )

    assert "wayfinder_hyperliquid_search_hip4" in judge
    assert "perps/spots" in judge
    assert "Do not pass extra" not in judge


def test_sports_skill_has_llm_prediction_market_research_stubs() -> None:
    skill = (REPO / ".claude" / "skills" / "using-sports-data" / "SKILL.md").read_text(
        "utf-8"
    )
    for needle in (
        "LLM forecasting / prediction-market notes",
        "Approaching Human-Level Forecasting",
        "PolyBench",
        "Beyond Accuracy",
        "KalshiBench",
        "calibration checks",
    ):
        assert needle in skill
