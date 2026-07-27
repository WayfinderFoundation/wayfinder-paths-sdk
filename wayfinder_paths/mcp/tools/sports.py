"""Provider-agnostic sports MCP tools.

Three tools, all backed by the backend sports gateway (the provider key never leaves
the backend):

- ``sports_snapshot``      -- bounded live reads (the primary agent's only sports read).
- ``sports_backtest_state``-- monitor canonical backtest run/job state (+ offline mirror).
- ``sports_provider``      -- full allowlisted provider facade (hidden ``wayfinder-sports``
                              subagent only; the primary is denied this tool).

Nothing here names a provider; the surface stays provider-agnostic.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from wayfinder_paths.core.clients.SportsClient import (
    SPORTS_CLIENT,
    SportsGatewayAPIError,
)
from wayfinder_paths.mcp.arg_validation import (
    MCPArgumentError,
    normalize_enum,
    optional_iana_timezone,
    optional_int,
    optional_iso8601,
    optional_json_object,
    optional_str,
)
from wayfinder_paths.mcp.state import sports_state
from wayfinder_paths.mcp.tool_annotations import JsonObjectInput, ProviderEndpointId
from wayfinder_paths.mcp.utils import catch_errors, err, ok

SportsAction = Annotated[
    str,
    Field(
        description=(
            "scoreboard, game, standings, team_lookup, player_lookup, injuries, "
            "season_averages, stats, leaders, odds, futures, player_props, or results."
        )
    ),
]
SportsCode = Annotated[
    str,
    Field(description="Provider-neutral league code such as nba, nfl, nhl, or mlb."),
]
SportsDate = Annotated[
    str,
    Field(description="ISO-8601 date/timestamp, or '_' to omit."),
]
SportsTimezone = Annotated[
    str,
    Field(description="IANA timezone such as UTC or America/Toronto, or '_' for UTC."),
]


def _gateway_err(exc: SportsGatewayAPIError) -> dict[str, Any]:
    return err(exc.code, exc.message, exc.details)


def _optional_id_list(value: Any, *, field_name: str) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        values = [str(item).strip() for item in value if str(item).strip()]
    else:
        raw = str(value).strip()
        if raw.lower() in {"", "_", "none", "null"}:
            return None
        values = [part.strip() for part in raw.split(",") if part.strip()]
    if not values:
        return None
    if len(values) > 50:
        raise MCPArgumentError(
            f"{field_name} must contain 50 ids or fewer",
            field=field_name,
            received=value,
        )
    return values


def _single_or_list_id(
    value: Any, *, field_name: str
) -> tuple[str | None, list[str] | None]:
    values = _optional_id_list(value, field_name=field_name)
    if not values:
        return None, None
    if len(values) == 1:
        return values[0], None
    return None, values


@catch_errors
async def sports_snapshot(
    action: SportsAction,
    sport: SportsCode,
    event_id: str = "_",
    game_id: str = "_",
    match_id: str = "_",
    fight_id: str = "_",
    tournament_id: str = "_",
    competitor_id: str = "_",
    competitor_ids: list[str] | str = "_",
    player_id: str = "_",
    player_ids: list[str] | str = "_",
    team_id: str = "_",
    search: str = "_",
    date: SportsDate = "_",
    timezone: SportsTimezone = "_",
    season: str = "_",
    prop_type: str = "_",
    market_type: str = "_",
    vendors: str = "_",
    limit: str | int = "_",
    offset: str | int = "_",
    sessionID: str = "_",
) -> dict[str, Any]:
    """Live sports snapshot (bounded reads, normalized cards).

    Actions: scoreboard, game, standings, team/player lookup, injuries,
    season_averages, stats, leaders, odds, futures, player_props, or results.
    Prefer canonical `event_id`; sport-specific ids remain accepted. Pass an IANA
    timezone with dates or UTC is assumed. Unsupported league/action pairs return
    the leagues that support the resource—do not retry the same pair.
    """
    parsed_limit = optional_int(limit, field_name="limit", min_value=1, max_value=50)
    parsed_offset = optional_int(offset, field_name="offset", min_value=0)
    parsed_competitor_id, parsed_competitor_ids_from_single = _single_or_list_id(
        competitor_id,
        field_name="competitor_id",
    )
    parsed_player_id, parsed_player_ids_from_single = _single_or_list_id(
        player_id,
        field_name="player_id",
    )
    parsed_competitor_ids = _optional_id_list(
        competitor_ids,
        field_name="competitor_ids",
    )
    parsed_player_ids = _optional_id_list(
        player_ids,
        field_name="player_ids",
    )
    effective_limit = (
        parsed_limit
        if parsed_limit is not None
        else 20
        if str(action).strip().lower() == "player_props"
        else None
    )
    try:
        result = await SPORTS_CLIENT.snapshot(
            action=action,
            sport=sport,
            event_id=optional_str(event_id, field_name="event_id"),
            game_id=optional_str(game_id, field_name="game_id"),
            match_id=optional_str(match_id, field_name="match_id"),
            fight_id=optional_str(fight_id, field_name="fight_id"),
            tournament_id=optional_str(tournament_id, field_name="tournament_id"),
            competitor_id=parsed_competitor_id,
            competitor_ids=parsed_competitor_ids or parsed_competitor_ids_from_single,
            player_id=parsed_player_id,
            player_ids=parsed_player_ids or parsed_player_ids_from_single,
            team_id=optional_str(team_id, field_name="team_id"),
            search=optional_str(search, field_name="search"),
            date=optional_iso8601(date, field_name="date"),
            timezone=optional_iana_timezone(timezone),
            season=optional_str(season, field_name="season"),
            prop_type=optional_str(prop_type, field_name="prop_type"),
            market_type=optional_str(market_type, field_name="market_type"),
            vendors=optional_str(vendors, field_name="vendors"),
            limit=effective_limit,
            offset=parsed_offset,
            session_id=sessionID,
        )
    except SportsGatewayAPIError as exc:
        return _gateway_err(exc)
    return ok(result)


@catch_errors
async def sports_backtest_state(
    action: str = "list_active",
    run_id: str = "_",
    limit: str | int = "_",
    sessionID: str = "_",
) -> dict[str, Any]:
    """Monitor sports backtests using backend state with a stale local fallback.

    Actions: list_active/recent, get_run, refresh_run/all_active, events, or
    provider_status. get/refresh/events require `run_id`; list limits cap at 50.
    """
    parsed_limit = optional_int(limit, field_name="limit", min_value=1, max_value=50)
    parsed_run_id = optional_str(run_id, field_name="run_id")
    try:
        result = await SPORTS_CLIENT.backtest_state(
            action=action,
            run_id=parsed_run_id,
            limit=parsed_limit,
            session_id=sessionID,
        )
    except SportsGatewayAPIError as exc:
        return _mirror_fallback(action, parsed_run_id, parsed_limit, exc)

    # Opportunistically mirror any run summaries the gateway returned.
    runs = result.get("runs") if isinstance(result, dict) else None
    if isinstance(runs, list):
        sports_state.upsert_runs(runs)
    run = result.get("run") if isinstance(result, dict) else None
    if isinstance(run, dict):
        sports_state.upsert_runs([run])
    return ok(result)


def _mirror_fallback(
    action: str,
    run_id: str | None,
    limit: int | None,
    exc: SportsGatewayAPIError,
) -> dict[str, Any]:
    """When the gateway is unreachable, serve known runs from the local mirror."""
    if action in ("list_active", "list_recent"):
        runs = sports_state.list_runs(
            active_only=(action == "list_active"), limit=limit or 10
        )
        if runs:
            return ok(
                {"runs": runs, "count": len(runs), "source": "mirror", "stale": True}
            )
    elif action in ("get_run", "refresh_run", "events") and run_id:
        run = sports_state.get_run(run_id)
        if run is not None:
            return ok({"run": run, "source": "mirror", "stale": True})
    return _gateway_err(exc)


@catch_errors
async def sports_provider(
    action: Literal["catalog", "call"] = "catalog",
    endpoint_id: ProviderEndpointId = "_",
    sport: str = "_",
    path_params: JsonObjectInput = "_",
    query: JsonObjectInput = "_",
    body: JsonObjectInput = "_",
    run_id: str = "_",
    title: str = "_",
    sessionID: str = "_",
) -> dict[str, Any]:
    """Full provider facade -- hidden wayfinder-sports subagent only.

    Call `catalog` first, then `call` an allowlisted endpoint id—never a URL.
    path_params/query/body are JSON object strings. Lab is limited to
    nba/nfl/nhl/mlb; mutations attach to `run_id` or create a tracked run.
    """
    normalized_action = normalize_enum(
        action,
        field_name="action",
        allowed_values={"catalog", "call"},
    )
    try:
        if normalized_action == "catalog":
            return ok(await SPORTS_CLIENT.provider_catalog(session_id=sessionID))
        parsed_endpoint_id = optional_str(endpoint_id, field_name="endpoint_id")
        if parsed_endpoint_id is None:
            raise MCPArgumentError(
                "endpoint_id is required for action='call'; get it from action='catalog'",
                field="endpoint_id",
                received=endpoint_id,
                suggested_arguments={"action": "catalog"},
            )

        result = await SPORTS_CLIENT.provider_call(
            endpoint_id=parsed_endpoint_id,
            sport=optional_str(sport, field_name="sport"),
            path_params=optional_json_object(path_params, field_name="path_params"),
            query=optional_json_object(query, field_name="query"),
            body=optional_json_object(body, field_name="body"),
            run_id=optional_str(run_id, field_name="run_id"),
            title=optional_str(title, field_name="title"),
            session_id=sessionID,
        )
    except SportsGatewayAPIError as exc:
        return _gateway_err(exc)

    # The run summary is mirrored on the next sports_backtest_state call; we avoid a
    # partial upsert here so we never clobber richer fields already in the mirror.
    return ok(result)
