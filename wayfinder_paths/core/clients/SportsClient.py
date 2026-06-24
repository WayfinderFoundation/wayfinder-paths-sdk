from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.GatewayClient import GatewayAPIError, GatewayClient

DEFAULT_SESSION_ID = "mcp"
SESSION_ENV_KEYS = (
    "WAYFINDER_SPORTS_SESSION_ID",
    "OPENCODE_SESSION_ID",
    "OPENCODE_SESSIONID",
    "OPENCODE_INSTANCE_ID",
)
SPORT_ALIASES = {
    "fifa": "worldcup",
    "fiba": "worldcup",
}


class SportsGatewayAPIError(GatewayAPIError):
    """Structured error raised when the sports gateway returns a non-2xx body."""


class SportsClient(GatewayClient):
    """Client for the backend-mediated, provider-agnostic Wayfinder Sports Gateway.

    The provider API key lives only in the backend; this client only ever talks to
    ``/api/v1/sports/*`` with the user's Wayfinder API key (``X-API-KEY``).
    """

    gateway_path = "sports"
    gateway_name = "Sports"
    gateway_error_class = SportsGatewayAPIError
    session_env_keys = SESSION_ENV_KEYS
    default_session_id = DEFAULT_SESSION_ID
    truncate_explicit_session_id = True

    async def snapshot(
        self,
        *,
        action: str,
        sport: str,
        event_id: str | None = None,
        game_id: str | None = None,
        match_id: str | None = None,
        fight_id: str | None = None,
        tournament_id: str | None = None,
        competitor_id: str | None = None,
        competitor_ids: list[str] | None = None,
        player_id: str | None = None,
        player_ids: list[str] | None = None,
        team_id: str | None = None,
        search: str | None = None,
        date: str | None = None,
        timezone: str | None = None,
        season: str | None = None,
        prop_type: str | None = None,
        market_type: str | None = None,
        vendors: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        session_id: str | None = None,
    ) -> Any:
        sport_slug = str(sport).strip().lower()
        payload: dict[str, Any] = {
            "action": str(action).strip(),
            "sport": SPORT_ALIASES.get(sport_slug, sport_slug),
            "sessionID": self.resolve_session_id(session_id),
        }
        if event_id:
            payload["event_id"] = str(event_id).strip()
        if game_id:
            payload["game_id"] = str(game_id).strip()
        if match_id:
            payload["match_id"] = str(match_id).strip()
        if fight_id:
            payload["fight_id"] = str(fight_id).strip()
        if tournament_id:
            payload["tournament_id"] = str(tournament_id).strip()
        if competitor_id:
            payload["competitor_id"] = str(competitor_id).strip()
        if competitor_ids:
            payload["competitor_ids"] = [
                str(v).strip() for v in competitor_ids if str(v).strip()
            ]
        if player_id:
            payload["player_id"] = str(player_id).strip()
        if player_ids:
            payload["player_ids"] = [
                str(v).strip() for v in player_ids if str(v).strip()
            ]
        if team_id:
            payload["team_id"] = str(team_id).strip()
        if search:
            payload["search"] = str(search).strip()
        if date:
            payload["date"] = str(date).strip()
        if timezone:
            payload["timezone"] = str(timezone).strip()
        if season:
            payload["season"] = str(season).strip()
        if prop_type:
            payload["prop_type"] = str(prop_type).strip()
        if market_type:
            payload["market_type"] = str(market_type).strip()
        if vendors:
            payload["vendors"] = str(vendors).strip()
        if limit is not None:
            payload["limit"] = int(limit)
        if offset is not None:
            payload["offset"] = int(offset)
        return await self._post_gateway("snapshot", payload)

    async def backtest_state(
        self,
        *,
        action: str,
        run_id: str | None = None,
        limit: int | None = None,
        session_id: str | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "action": str(action).strip(),
            "sessionID": self.resolve_session_id(session_id),
        }
        if run_id:
            payload["run_id"] = str(run_id).strip()
        if limit is not None:
            payload["limit"] = int(limit)
        return await self._post_gateway("backtests/state", payload)

    async def provider_catalog(self, *, session_id: str | None = None) -> Any:
        return await self._post_gateway(
            "provider",
            {"action": "catalog", "sessionID": self.resolve_session_id(session_id)},
        )

    async def provider_call(
        self,
        *,
        endpoint_id: str,
        sport: str | None = None,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        body: Any = None,
        run_id: str | None = None,
        title: str | None = None,
        session_id: str | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "action": "call",
            "endpoint_id": str(endpoint_id).strip(),
            "sessionID": self.resolve_session_id(session_id),
        }
        if sport:
            payload["sport"] = str(sport).strip().lower()
        if path_params:
            payload["path_params"] = path_params
        if query:
            payload["query"] = query
        if body is not None:
            payload["body"] = body
        if run_id:
            payload["run_id"] = str(run_id).strip()
        if title:
            payload["title"] = str(title).strip()
        return await self._post_gateway("provider", payload)


SPORTS_CLIENT = SportsClient()
