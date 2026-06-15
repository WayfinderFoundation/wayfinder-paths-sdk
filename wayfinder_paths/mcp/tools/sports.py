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

from typing import Any

from wayfinder_paths.core.clients.SportsClient import (
    SPORTS_CLIENT,
    SportsGatewayAPIError,
)
from wayfinder_paths.mcp.arg_validation import (
    optional_int,
    optional_json_object,
    optional_str,
)
from wayfinder_paths.mcp.state import sports_state
from wayfinder_paths.mcp.utils import catch_errors, err, ok


def _gateway_err(exc: SportsGatewayAPIError) -> dict[str, Any]:
    return err(exc.code, exc.message, exc.details)


@catch_errors
async def sports_snapshot(
    action: str,
    sport: str,
    game_id: str = "_",
    search: str = "_",
    date: str = "_",
    limit: str | int = "_",
    sessionID: str = "_",
) -> dict[str, Any]:
    """Live sports snapshot (bounded reads, normalized cards).

    Resources are canonical across leagues: player_lookup returns players, fighters, or
    drivers depending on the sport; scoreboard returns games, matches, events, or sessions.
    Availability varies by league (e.g. season_averages/standings exist for NBA but not
    tennis) -- an unsupported action returns code `resource_unavailable_for_league` with the
    leagues that do support it.

    Args:
        action: scoreboard | game | standings | team_lookup | player_lookup | injuries |
            season_averages | stats | leaders | odds | player_props.
        sport: League code, e.g. nba, nfl, mlb, nhl, epl, mma, f1, atp, pga, ...
        game_id: Required for action=game and player_props; for odds, pass game_id or date.
        search: Name query for team_lookup / player_lookup.
        date: Optional ISO date (YYYY-MM-DD) for scoreboard/odds.
        limit: Max cards (1-50, default 10).
        sessionID: OpenCode session id, or "_" to resolve from the environment.
    """
    parsed_limit = optional_int(limit, field_name="limit", min_value=1, max_value=50)
    try:
        result = await SPORTS_CLIENT.snapshot(
            action=action,
            sport=sport,
            game_id=optional_str(game_id, field_name="game_id"),
            search=optional_str(search, field_name="search"),
            date=optional_str(date, field_name="date"),
            limit=parsed_limit,
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
    """Monitor sports backtest runs (backend is canonical; SQLite mirror is offline fallback).

    Args:
        action: list_active | list_recent | get_run | refresh_run | refresh_all_active |
            events | provider_status.
        run_id: Required for get_run / refresh_run / events.
        limit: Max runs for list_recent (1-50).
        sessionID: OpenCode session id, or "_" to resolve from the environment.
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
    action: str = "catalog",
    endpoint_id: str = "_",
    sport: str = "_",
    path_params: str = "_",
    query: str = "_",
    body: str = "_",
    run_id: str = "_",
    title: str = "_",
    sessionID: str = "_",
) -> dict[str, Any]:
    """Full provider facade -- hidden wayfinder-sports subagent only.

    Calls an ALLOWLISTED endpoint by its generic endpoint_id (never an arbitrary URL).
    Lab mutations are tracked as backend run/job state.

    Args:
        action: catalog | call.
        endpoint_id: Allowlisted id for action=call (e.g. data.games.list, lab.models.create).
            Run sports_provider(action="catalog") to list ids.
        sport: League code; required for league/lab endpoints (lab gated to nba/nfl/nhl/mlb).
        path_params: JSON object of path params, e.g. {"id": "..."}.
        query: JSON object of query-string params.
        body: JSON object request body (for POST/PATCH endpoints).
        run_id: Existing run to attach a Lab mutation to (created if omitted).
        title: Optional title when a run is created.
        sessionID: OpenCode session id, or "_" to resolve from the environment.
    """
    normalized_action = str(action).strip().lower()
    try:
        if normalized_action == "catalog":
            return ok(await SPORTS_CLIENT.provider_catalog(session_id=sessionID))
        if normalized_action != "call":
            return err("invalid_argument", "action must be 'catalog' or 'call'")

        result = await SPORTS_CLIENT.provider_call(
            endpoint_id=endpoint_id,
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
