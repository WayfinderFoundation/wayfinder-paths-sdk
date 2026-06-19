"""Live smoke test for canonical sports snapshot filters.

This is intentionally opt-in. It calls the Wayfinder sports gateway with the same
SDK client the MCP tools use, then classifies each probe as:

- pass: request shaped correctly and returned data
- empty_ok: request shaped correctly but no rows were available
- auth_scope_blocked: gateway/provider credentials or account tier blocked access
- schema_error: unexpected failure that likely means our interface/request shape broke

Run:
    poetry run python scripts/sports_canonical_live_smoke.py --date 2026-06-19 --timezone America/Toronto
    poetry run python scripts/sports_canonical_live_smoke.py --api-profile dev --date 2026-06-19
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from wayfinder_paths.core import config as wf_config
from wayfinder_paths.core.clients.SportsClient import SPORTS_CLIENT, SportsGatewayAPIError


AUTH_CODES = {
    "provider_misconfigured",
    "provider_auth_failed",
    "provider_failure",
    "unauthorized",
    "forbidden",
}


async def _snapshot(label: str, *, allow_empty: bool = True, **kwargs: Any) -> dict[str, Any]:
    try:
        payload = await SPORTS_CLIENT.snapshot(**kwargs)
    except SportsGatewayAPIError as exc:
        status = "auth_scope_blocked" if _looks_auth_or_scope_blocked(exc) else "schema_error"
        return {
            "label": label,
            "status": status,
            "error": {"code": exc.code, "message": exc.message, "details": exc.details},
            "request": kwargs,
        }

    cards = payload.get("cards") if isinstance(payload, dict) else None
    count = len(cards) if isinstance(cards, list) else int(payload.get("count") or 0)
    status = "pass" if count > 0 else ("empty_ok" if allow_empty else "schema_error")
    return {
        "label": label,
        "status": status,
        "count": count,
        "warnings": payload.get("warnings", []) if isinstance(payload, dict) else [],
        "sample": cards[:2] if isinstance(cards, list) else None,
        "request": kwargs,
    }


def _looks_auth_or_scope_blocked(exc: SportsGatewayAPIError) -> bool:
    details = exc.details if isinstance(exc.details, dict) else {}
    provider_status = details.get("providerStatus")
    text = f"{exc.code} {exc.message}".lower()
    return (
        exc.status_code in (401, 403)
        or provider_status in (401, 403)
        or exc.code in AUTH_CODES
        or any(word in text for word in ("unauthorized", "forbidden", "tier", "scope", "plan"))
    )


def _first_event_id(result: dict[str, Any]) -> str | None:
    sample = result.get("sample")
    if not isinstance(sample, list) or not sample:
        return None
    card = sample[0]
    if not isinstance(card, dict):
        return None
    provider_ids = card.get("providerIds")
    return str(
        card.get("event_id")
        or card.get("id")
        or (provider_ids.get("event_id") if isinstance(provider_ids, dict) else "")
        or ""
    ).strip() or None


def _apply_api_profile(profile: str) -> None:
    if profile == "config":
        return

    system = wf_config.CONFIG.setdefault("system", {})
    if profile == "dev":
        base_url = system.get("dev_api_base_url")
        api_key = system.get("dev_api_key")
        if base_url:
            system["api_base_url"] = base_url
        if api_key:
            system["api_key"] = api_key

    SPORTS_CLIENT.headers.pop("X-API-KEY", None)
    SPORTS_CLIENT.client.headers.pop("X-API-KEY", None)
    SPORTS_CLIENT._ensure_api_key_header()


async def run(args: argparse.Namespace) -> dict[str, Any]:
    date = args.date or datetime.now(UTC).date().isoformat()
    timezone = args.timezone
    season = args.season or date[:4]
    results: list[dict[str, Any]] = []

    async def add(label: str, *, allow_empty: bool = True, **kwargs: Any) -> dict[str, Any]:
        result = await _snapshot(label, allow_empty=allow_empty, session_id=args.session_id, **kwargs)
        results.append(result)
        return result

    wc = await add(
        "worldcup.scoreboard.date_timezone",
        action="scoreboard",
        sport="worldcup",
        date=date,
        timezone=timezone,
        limit=5,
    )
    wc_event = _first_event_id(wc)
    if wc_event:
        await add("worldcup.game.event_id", action="game", sport="worldcup", event_id=wc_event)
        await add("worldcup.odds.event_id", action="odds", sport="worldcup", event_id=wc_event)
        await add(
            "worldcup.player_props.event_id",
            action="player_props",
            sport="worldcup",
            event_id=wc_event,
            prop_type="shots",
        )
    await add("worldcup.futures.season", action="futures", sport="worldcup", season=season, limit=10)

    epl = await add(
        "soccer.epl.scoreboard.date",
        action="scoreboard",
        sport="epl",
        date=date,
        timezone=timezone,
        limit=5,
    )
    epl_event = _first_event_id(epl)
    if epl_event:
        await add("soccer.epl.player_props.event_id", action="player_props", sport="epl", event_id=epl_event)

    mma = await add("mma.events", action="scoreboard", sport="mma", limit=5)
    await add("mma.fighter_lookup.mullins", action="player_lookup", sport="mma", search="Melissa Mullins")
    await add("mma.fighter_lookup.mesquita", action="player_lookup", sport="mma", search="Bia Mesquita")
    mma_event = _first_event_id(mma)
    if mma_event:
        await add("mma.odds.event_id", action="odds", sport="mma", event_id=mma_event)
        await add("mma.results.event_id", action="results", sport="mma", event_id=mma_event)

    for sport, player in (("atp", "Djokovic"), ("wta", "Gauff")):
        event = await add(f"{sport}.matches.date", action="scoreboard", sport=sport, date=date, timezone=timezone, limit=5)
        event_id = _first_event_id(event)
        await add(f"{sport}.player_lookup", action="player_lookup", sport=sport, search=player)
        await add(f"{sport}.odds.date", action="odds", sport=sport, date=date, season=season)
        if event_id:
            await add(f"{sport}.odds.event_id", action="odds", sport=sport, event_id=event_id, season=season)

    f1 = await add("f1.events.season", action="scoreboard", sport="f1", season=season, limit=5)
    f1_event = _first_event_id(f1)
    await add("f1.driver_lookup", action="player_lookup", sport="f1", search="Verstappen")
    await add("f1.futures.outright", action="futures", sport="f1", market_type="outright", season=season)
    if f1_event:
        await add("f1.futures.event_id", action="futures", sport="f1", event_id=f1_event, market_type="race_winner")
        await add("f1.results.event_id", action="results", sport="f1", event_id=f1_event, season=season)

    pga = await add("pga.tournaments.season", action="scoreboard", sport="pga", season=season, limit=5)
    pga_event = _first_event_id(pga)
    await add("pga.player_lookup", action="player_lookup", sport="pga", search="Scheffler")
    await add("pga.futures", action="futures", sport="pga", season=season)
    if pga_event:
        await add("pga.player_props.event_id", action="player_props", sport="pga", event_id=pga_event)
        await add("pga.results.event_id", action="results", sport="pga", event_id=pga_event)

    return {
        "apiProfile": args.api_profile,
        "date": date,
        "timezone": timezone,
        "season": season,
        "summary": {status: sum(1 for row in results if row["status"] == status) for status in sorted({r["status"] for r in results})},
        "results": results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-profile", choices=["config", "dev"], default="config")
    parser.add_argument("--date", default="", help="YYYY-MM-DD local date for schedule probes")
    parser.add_argument("--timezone", default="America/Toronto", help="IANA timezone")
    parser.add_argument("--season", default="", help="Season/year override")
    parser.add_argument("--session-id", default="sports-live-smoke", help="Gateway session id")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    _apply_api_profile(args.api_profile)
    print(json.dumps(asyncio.run(run(args)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
