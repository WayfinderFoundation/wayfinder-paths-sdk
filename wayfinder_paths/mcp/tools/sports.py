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

import json
from typing import Any

from wayfinder_paths.core.clients.SportsClient import (
    SPORTS_CLIENT,
    SportsGatewayAPIError,
)
from wayfinder_paths.mcp.state import sports_state
from wayfinder_paths.mcp.utils import catch_errors, err, ok


def _parse_json_obj(value: Any, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    text = str(value).strip()
    if not text or text == "_":
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field} must be a JSON object")
    return parsed


def _optional(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text and text != "_" else None


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

    Args:
        action: scoreboard | game | odds | player_props | injuries | team_lookup | player_lookup.
        sport: League code, e.g. nba, nfl, mlb, nhl, epl, laliga, f1, ... (all data leagues).
        game_id: Required for action=game and player_props; for odds, pass game_id or date.
        search: Name query for team_lookup / player_lookup.
        date: Optional ISO date (YYYY-MM-DD) for scoreboard/odds.
        limit: Max cards (1-50, default 10).
        sessionID: OpenCode session id, or "_" to resolve from the environment.
    """
    parsed_limit = None
    if _optional(limit) is not None:
        parsed_limit = int(limit)
    try:
        result = await SPORTS_CLIENT.snapshot(
            action=action,
            sport=sport,
            game_id=_optional(game_id),
            search=_optional(search),
            date=_optional(date),
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
    parsed_limit = None
    if _optional(limit) is not None:
        parsed_limit = int(limit)
    try:
        result = await SPORTS_CLIENT.backtest_state(
            action=action,
            run_id=_optional(run_id),
            limit=parsed_limit,
            session_id=sessionID,
        )
    except SportsGatewayAPIError as exc:
        return _mirror_fallback(action, _optional(run_id), parsed_limit, exc)

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
            sport=_optional(sport),
            path_params=_parse_json_obj(path_params, "path_params"),
            query=_parse_json_obj(query, "query"),
            body=_parse_json_obj(body, "body"),
            run_id=_optional(run_id),
            title=_optional(title),
            session_id=sessionID,
        )
    except SportsGatewayAPIError as exc:
        return _gateway_err(exc)

    # The run summary is mirrored on the next sports_backtest_state call; we avoid a
    # partial upsert here so we never clobber richer fields already in the mirror.
    return ok(result)
