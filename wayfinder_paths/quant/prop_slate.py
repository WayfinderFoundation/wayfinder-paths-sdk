"""Canned player-prop slate pipeline: fetch -> model -> rank, in one call.

This is the sports agent's primary path for prop analysis — run it instead of writing a
bespoke modelling script. It bakes in the lessons from live runs so they cannot be skipped:

- complete data: game-log fetches are chunked per player batch and follow ``meta.next_cursor``
  until exhausted; any props-player still missing logs is **excluded with a reason**, never
  scored as a 0.0 average.
- real math: probabilities come from :mod:`wayfinder_paths.quant.sports_props`
  (minutes-based projections, shrinkage, normal/Poisson distributions) and the book
  probability is **de-vigged** (raw implied odds include vig).
- honest output: thin samples / injuries / implausible edges are flagged and partitioned into
  ``watch`` instead of polluting ``actionable``; the season baseline is derived from the full
  season of logs, not recycled recent form.

Two-stage EV design: the ``book_edge`` / ``book_ev`` columns are measured against the
**de-vigged sportsbook odds** and are INFORMATIONAL — Wayfinder cannot execute at a
sportsbook. Each pick carries ``model_p`` so the executable second stage is a one-liner
against a prediction-market price: ``sports_props.market_edge(model_p, polymarket_price)``.

CLI:
    poetry run python -m wayfinder_paths.quant.prop_slate \
        --sport nba --game-id 21716138 --season 2025 --out .wayfinder_runs/sports
"""

from __future__ import annotations

import asyncio
import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wayfinder_paths.quant import sports_props as sp
from wayfinder_paths.quant.sports_gateway import (
    GatewayPacer,
    call_provider,
    next_cursor,
    rows_from_payload,
)

SUSPECT_EDGE = (
    0.25  # |book edge| above this is flagged: real books rarely misprice this far
)
MIN_GAMES = 8  # below this the sample is flagged low_sample (live runs showed 6-7g overconfidence)
_CHUNK = 8
_MAX_PAGES = 12
# MLB pitcher props project off outs recorded; everything else off plate appearances.
_MLB_PITCHER_PROPS = frozenset(
    {
        "pitcher_strikeouts",
        "pitcher_earned_runs",
        "pitcher_hits_allowed",
        "pitcher_outs",
    }
)


def exposure_key_for(sport: str, prop_type: str = "") -> str:
    """The game-log column measuring playing time for this sport/prop family."""
    if str(sport).lower() == "mlb":
        return (
            "pitching_outs" if prop_type in _MLB_PITCHER_PROPS else "plate_appearances"
        )
    return "min"


@dataclass
class SlateData:
    sport: str
    game_id: int | str
    season: int
    vendor: str
    props: list[dict[str, Any]]  # {player_id, prop_type, line, over_odds, under_odds}
    logs_by_player: dict[Any, list[dict[str, Any]]]  # most-recent-first
    season_baseline: dict[Any, dict[str, float]]  # per player: stat means + "min"
    player_names: dict[Any, str]
    player_team: dict[Any, Any]
    opponent_of: dict[Any, Any]  # team_id -> opposing team_id (for this game)
    team_stats: dict[Any, dict[str, Any]]  # team_id -> {pace, def_rating, abbreviation}
    league_pace: float
    league_def_rating: float
    injured: set[Any]
    excluded: list[dict[str, Any]] = field(default_factory=list)
    skipped_one_sided: int = 0  # milestone-style single-quote markets (can't de-vig)


@dataclass
class SlatePick:
    player_id: Any
    player_name: str
    team: str
    prop_type: str
    line: float
    side: str
    model_p: float
    book_p: float
    book_edge: float
    book_ev: float
    kelly: float
    proj_mean: float
    proj_std: float
    n_games: int
    flags: list[str]


@dataclass
class SlateResult:
    sport: str
    game_id: int | str
    season: int
    vendor: str
    actionable: list[SlatePick]
    watch: list[SlatePick]
    excluded: list[dict[str, Any]]
    pace_factor: float
    skipped_one_sided: int = 0
    note: str = (
        "book_edge/book_ev are vs de-vigged SPORTSBOOK odds — informational only. "
        "Executable EV must be priced on Polymarket: market_edge(model_p, polymarket_price)."
    )


# ── fetch ────────────────────────────────────────────────────────────────────


def select_vendor(prop_rows: list[dict[str, Any]]) -> str | None:
    """Vendor with the widest distinct (player, prop_type) over/under coverage."""
    coverage: Counter[str] = Counter()
    seen: set[tuple] = set()
    for row in prop_rows:
        market = row.get("market") or {}
        if market.get("over_odds") is None or market.get("under_odds") is None:
            continue
        key = (row.get("vendor"), row.get("player_id"), row.get("prop_type"))
        if key in seen:
            continue
        seen.add(key)
        coverage[str(row.get("vendor"))] += 1
    return coverage.most_common(1)[0][0] if coverage else None


async def fetch_prop_slate(
    sport: str,
    game_id: int | str,
    season: int,
    *,
    client: Any = None,
    pace_s: float = 1.0,
) -> SlateData:
    """Fetch everything the slate model needs through the backend gateway (cached/bulk)."""
    if client is None:
        from wayfinder_paths.core.clients.SportsClient import SPORTS_CLIENT

        client = SPORTS_CLIENT
    pacer = GatewayPacer(pace_s)

    # 1) the game -> teams + opponent mapping
    event = await call_provider(
        client,
        pacer,
        endpoint_id="data.event.get",
        sport=sport,
        path_params={"id": game_id},
    )
    edata = event.get("data", {}).get("data", event.get("data", {})) or {}
    home = edata.get("home_team") or {}
    away = edata.get("visitor_team") or edata.get("away_team") or {}
    opponent_of = {home.get("id"): away.get("id"), away.get("id"): home.get("id")}
    team_abbr_by_name = {
        str(t.get("display_name") or t.get("full_name") or ""): str(
            t.get("abbreviation") or t.get("name") or "?"
        )
        for t in (home, away)
    }
    await pacer.wait()

    # 2) props (cursor-followed: MLB slates exceed one page) -> best vendor -> pairs
    prop_rows: list[dict[str, Any]] = []
    cursor = None
    for _ in range(_MAX_PAGES):
        query: dict[str, Any] = {"game_id": game_id, "per_page": 100}
        if cursor is not None:
            query["cursor"] = cursor
        props_payload = await call_provider(
            client,
            pacer,
            endpoint_id="data.player_props.list",
            sport=sport,
            query=query,
        )
        prop_rows.extend(rows_from_payload(props_payload))
        cursor = next_cursor(props_payload)
        await pacer.wait()
        if cursor is None:
            break
    vendor = select_vendor(prop_rows)
    pairs: dict[tuple, dict[str, Any]] = {}
    one_sided: set[tuple] = set()  # milestone markets: single quote, can't de-vig
    for row in prop_rows:
        market = row.get("market") or {}
        key = (row.get("player_id"), row.get("prop_type"))
        if market.get("over_odds") is None or market.get("under_odds") is None:
            one_sided.add(key)
            continue
        if (
            str(row.get("vendor")) != vendor
            or row.get("prop_type") not in sp.PROP_STATS
        ):
            continue
        pairs.setdefault(
            key,
            {
                "player_id": row.get("player_id"),
                "prop_type": row.get("prop_type"),
                "line": float(row.get("line_value")),
                "over_odds": float(market["over_odds"]),
                "under_odds": float(market["under_odds"]),
            },
        )
    props = list(pairs.values())
    skipped_one_sided = len(one_sided - set(pairs))
    player_ids = sorted({p["player_id"] for p in props})
    await pacer.wait()

    # 3) full-season game logs: chunked + cursor-followed until exhausted
    logs_by_player: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    player_names: dict[Any, str] = {}
    player_team: dict[Any, Any] = {}

    async def _fetch_logs(ids: list, *, per_page: int = 100) -> None:
        cursor = None
        for _ in range(_MAX_PAGES):
            query: dict[str, Any] = {
                "player_ids": ids,
                "seasons": [season],
                "per_page": per_page,
            }
            if cursor is not None:
                query["cursor"] = cursor
            payload = await call_provider(
                client,
                pacer,
                endpoint_id="data.player_stats.list",
                sport=sport,
                query=query,
            )
            for log in rows_from_payload(payload):
                player = log.get("player") or {}
                pid = player.get("id")
                if pid is None:
                    continue
                logs_by_player[pid].append(log)
                if pid not in player_names and player.get("last_name"):
                    first = (player.get("first_name") or "")[:1]
                    player_names[pid] = f"{first}. {player['last_name']}".strip()
                team = log.get("team") or {}
                if pid not in player_team and team.get("id") is not None:
                    player_team[pid] = team.get("id")
            cursor = next_cursor(payload)
            await pacer.wait()
            if cursor is None:
                break

    for i in range(0, len(player_ids), _CHUNK):
        await _fetch_logs(player_ids[i : i + _CHUNK])

    # completeness: refetch stragglers individually once; otherwise exclude with a reason
    excluded: list[dict[str, Any]] = []
    for pid in player_ids:
        if not logs_by_player.get(pid):
            await _fetch_logs([pid])
        if not logs_by_player.get(pid):
            excluded.append({"player_id": pid, "reason": "no_game_logs"})

    # sort logs most-recent-first (score_prop expects that ordering). NBA rows embed a
    # game date; MLB rows carry only game_id, which the provider assigns in schedule
    # order — so it stands in for the date (rescheduled games misorder slightly).
    def _log_order(lg: dict[str, Any]) -> tuple[str, int]:
        date = str((lg.get("game") or {}).get("date") or "")
        try:
            gid = int(lg.get("game_id") or (lg.get("game") or {}).get("id") or 0)
        except (TypeError, ValueError):
            gid = 0
        return (date, gid)

    for logs in logs_by_player.values():
        logs.sort(key=_log_order, reverse=True)

    # MLB: derive singles onto each log row (provider gives the components only), and
    # team identity comes as a name string rather than a team object.
    if str(sport).lower() == "mlb":
        for pid, logs in logs_by_player.items():
            for lg in logs:
                lg["singles"] = (
                    float(lg.get("hits") or 0.0)
                    - float(lg.get("doubles") or 0.0)
                    - float(lg.get("triples") or 0.0)
                    - float(lg.get("hr") or 0.0)
                )
            name = next(
                (lg.get("team_name") for lg in logs if lg.get("team_name")), None
            )
            if pid not in player_team and name:
                player_team[pid] = team_abbr_by_name.get(str(name), str(name))

    # 4) season baseline from the full season of logs, per exposure family (played games
    # only). NBA has one family (minutes); MLB splits batting (plate appearances) from
    # pitching (outs recorded) so a two-way player's batting games never dilute his
    # pitching rates and vice versa.
    needed_types = {p["prop_type"] for p in props}
    exposure_families = {exposure_key_for(sport, t) for t in needed_types} or {
        exposure_key_for(sport)
    }
    season_baseline: dict[Any, dict[str, float]] = {}
    for pid, logs in logs_by_player.items():
        baseline: dict[str, float] = {}
        for exp_key in exposure_families:
            fam_types = [
                t for t in needed_types if exposure_key_for(sport, t) == exp_key
            ]
            fam_stats = sorted({k for t in fam_types for k in sp.PROP_STATS.get(t, ())})
            played = [lg for lg in logs if sp.parse_minutes(lg.get(exp_key)) > 0]
            if not played:
                continue
            baseline[exp_key] = sum(
                sp.parse_minutes(lg.get(exp_key)) for lg in played
            ) / len(played)
            for key in fam_stats:
                baseline[key] = sum(float(lg.get(key) or 0.0) for lg in played) / len(
                    played
                )
        if baseline:
            season_baseline[pid] = baseline

    # 5) team factors (one call: all teams, advanced -> pace/def_rating). Soft: leagues
    # without an advanced team surface (MLB) score with neutral factors instead of dying.
    team_stats: dict[Any, dict[str, Any]] = {}
    try:
        teams_payload = await call_provider(
            client,
            pacer,
            endpoint_id="data.team_season_averages.list",
            sport=sport,
            path_params={"category": "general"},
            query={
                "season": season,
                "season_type": "regular",
                "type": "advanced",
                "per_page": 40,
            },
        )
        for row in rows_from_payload(teams_payload):
            team, stats = row.get("team") or {}, row.get("stats") or {}
            if team.get("id") is not None:
                team_stats[team["id"]] = {
                    "pace": stats.get("pace"),
                    "def_rating": stats.get("def_rating"),
                    "abbreviation": team.get("abbreviation") or team.get("name") or "?",
                }
    except Exception:  # noqa: BLE001 - factors are an enhancement, never fatal
        pass
    paces = [t["pace"] for t in team_stats.values() if t.get("pace")]
    defs = [t["def_rating"] for t in team_stats.values() if t.get("def_rating")]
    league_pace = sum(paces) / len(paces) if paces else 100.0
    league_def = sum(defs) / len(defs) if defs else 113.0
    await pacer.wait()

    # 6) injuries (flag only)
    injured: set[Any] = set()
    try:
        inj_payload = await call_provider(
            client,
            endpoint_id="data.injuries.list",
            sport=sport,
            query={"per_page": 100},
        )
        injured = {
            (row.get("player") or {}).get("id")
            for row in rows_from_payload(inj_payload)
            if (row.get("player") or {}).get("id") is not None
        }
    except Exception:  # noqa: BLE001 - injuries are a soft signal, never fatal
        pass

    return SlateData(
        sport=sport,
        game_id=game_id,
        season=season,
        vendor=vendor or "?",
        props=props,
        logs_by_player=dict(logs_by_player),
        season_baseline=season_baseline,
        player_names=player_names,
        player_team=player_team,
        opponent_of=opponent_of,
        team_stats=team_stats,
        league_pace=league_pace,
        league_def_rating=league_def,
        injured=injured,
        excluded=excluded,
        skipped_one_sided=skipped_one_sided,
    )


# ── score ────────────────────────────────────────────────────────────────────


def score_prop_slate(slate: SlateData, *, kelly_fraction: float = 0.25) -> SlateResult:
    """Score every prop with the sports_props model; partition by data quality."""
    home_away = [tid for tid in slate.opponent_of if tid is not None]
    pace_factor = 1.0
    if len(home_away) == 2:
        pace_factor = sp.pace_factor(
            (slate.team_stats.get(home_away[0]) or {}).get("pace") or slate.league_pace,
            (slate.team_stats.get(home_away[1]) or {}).get("pace") or slate.league_pace,
            slate.league_pace,
        )

    excluded_ids = {entry["player_id"] for entry in slate.excluded}
    actionable: list[SlatePick] = []
    watch: list[SlatePick] = []

    for prop in slate.props:
        pid = prop["player_id"]
        if pid in excluded_ids:
            continue
        logs = slate.logs_by_player.get(pid) or []
        team_id = slate.player_team.get(pid)
        opp_id = slate.opponent_of.get(team_id)
        opp_def = (slate.team_stats.get(opp_id) or {}).get(
            "def_rating"
        ) or slate.league_def_rating
        opp_factor = sp.opponent_factor(opp_def, slate.league_def_rating, weight=0.6)

        score = sp.score_prop(
            prop,
            logs,
            slate.season_baseline.get(pid),
            opponent_factor=opp_factor,
            pace_factor=pace_factor,
            injured=pid in slate.injured,
            min_games=MIN_GAMES,
            kelly_fraction=kelly_fraction,
            exposure_key=exposure_key_for(slate.sport, prop["prop_type"]),
        )
        if score is None:
            continue

        flags = list(score.flags)
        if abs(score.edge) > SUSPECT_EDGE:
            flags.append("suspect_edge")

        team_abbr = (slate.team_stats.get(team_id) or {}).get("abbreviation") or (
            str(team_id) if team_id is not None else "?"
        )
        pick = SlatePick(
            player_id=pid,
            player_name=slate.player_names.get(pid, str(pid)),
            team=team_abbr,
            prop_type=score.prop_type,
            line=score.line,
            side=score.side,
            model_p=round(score.model_p, 4),
            book_p=round(score.book_p, 4),
            book_edge=round(score.edge, 4),
            book_ev=round(score.ev, 4),
            kelly=round(score.kelly, 4),
            proj_mean=round(score.projection.mean, 2),
            proj_std=round(score.projection.std, 2),
            n_games=score.projection.n,
            flags=flags,
        )
        (watch if flags else actionable).append(pick)

    actionable.sort(key=lambda p: p.book_ev, reverse=True)
    watch.sort(key=lambda p: p.book_ev, reverse=True)
    return SlateResult(
        sport=slate.sport,
        game_id=slate.game_id,
        season=slate.season,
        vendor=slate.vendor,
        actionable=actionable,
        watch=watch,
        excluded=slate.excluded,
        pace_factor=round(pace_factor, 4),
        skipped_one_sided=slate.skipped_one_sided,
    )


# ── render / artifacts ───────────────────────────────────────────────────────


def _pick_row(pick: SlatePick) -> dict[str, Any]:
    return {
        "player": pick.player_name,
        "team": pick.team,
        "prop": pick.prop_type,
        "line": pick.line,
        "side": pick.side,
        "model_p": pick.model_p,
        "book_p": pick.book_p,
        "book_edge": pick.book_edge,
        "book_ev": pick.book_ev,
        "kelly": pick.kelly,
        "proj": pick.proj_mean,
        "std": pick.proj_std,
        "n": pick.n_games,
        "flags": ",".join(pick.flags),
        "player_id": pick.player_id,
    }


def slate_rows(result: SlateResult) -> list[dict[str, Any]]:
    rows = []
    for bucket, picks in (("actionable", result.actionable), ("watch", result.watch)):
        for pick in picks:
            rows.append({"bucket": bucket, **_pick_row(pick)})
    return rows


def render_slate(result: SlateResult, *, top: int = 12) -> str:
    lines = [
        f"PROP SLATE — {result.sport} game {result.game_id} (season {result.season}, "
        f"vendor {result.vendor}, pace×{result.pace_factor})",
        "",
        f"ACTIONABLE (clean data, sane edges) — top {top} by book EV:",
    ]
    fmt = (
        "  {player:<18} {team:<4} {prop:<24} {side:<5} {line:<5} proj {proj:>5} "
        "mP {model_p:.2f} bP {book_p:.2f} edge {book_edge:+.2f} EV {book_ev:+.2f} n={n}"
    )
    for pick in result.actionable[:top]:
        lines.append(fmt.format(**_pick_row(pick)))
    if not result.actionable:
        lines.append("  (none)")
    lines.append("")
    lines.append(f"WATCH (flagged: low sample / injured / suspect edge) — top {top}:")
    for pick in result.watch[:top]:
        lines.append(fmt.format(**_pick_row(pick)) + f"  [{','.join(pick.flags)}]")
    if not result.watch:
        lines.append("  (none)")
    if result.excluded:
        lines.append("")
        lines.append(
            "EXCLUDED (not scored): "
            + ", ".join(f"{e['player_id']} ({e['reason']})" for e in result.excluded)
        )
    if result.skipped_one_sided:
        lines.append("")
        lines.append(
            f"SKIPPED: {result.skipped_one_sided} one-sided (milestone) markets — "
            "single quote, no under side to de-vig against."
        )
    lines.append("")
    lines.append("NOTE: " + result.note)
    return "\n".join(lines)


async def run_prop_slate(
    sport: str,
    game_id: int | str,
    season: int,
    *,
    client: Any = None,
    out_dir: str | Path | None = None,
    top: int = 12,
) -> tuple[SlateResult, list[str]]:
    """Fetch + score + (optionally) write artifacts. Returns (result, artifact_paths)."""
    slate = await fetch_prop_slate(sport, game_id, season, client=client)
    result = score_prop_slate(slate)
    artifacts: list[str] = []
    if out_dir:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        rows = slate_rows(result)
        csv_path = out / f"prop_slate_{game_id}.csv"
        if rows:
            with csv_path.open("w", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
            artifacts.append(str(csv_path))
        json_path = out / f"prop_slate_{game_id}.json"
        json_path.write_text(
            json.dumps(
                {
                    "sport": result.sport,
                    "game_id": result.game_id,
                    "season": result.season,
                    "vendor": result.vendor,
                    "pace_factor": result.pace_factor,
                    "note": result.note,
                    "rows": rows,
                    "excluded": result.excluded,
                },
                indent=2,
                default=str,
            )
        )
        artifacts.append(str(json_path))
    return result, artifacts


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Score a game's player-prop slate.")
    parser.add_argument("--sport", required=True)
    parser.add_argument(
        "--game-id", required=True, help="game id, or comma-separated ids for a slate"
    )
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--out", default=".wayfinder_runs/sports")
    parser.add_argument("--top", type=int, default=12)
    args = parser.parse_args()

    all_artifacts: list[str] = []
    any_picks = False
    for game_id in str(args.game_id).split(","):
        result, artifacts = asyncio.run(
            run_prop_slate(
                args.sport, game_id.strip(), args.season, out_dir=args.out, top=args.top
            )
        )
        all_artifacts.extend(artifacts)
        any_picks = any_picks or bool(result.actionable or result.watch)
        print(render_slate(result, top=args.top))
        print()
    print("artifacts:", " ".join(all_artifacts) if all_artifacts else "(none)")
    if not any_picks:
        raise SystemExit(2)


if __name__ == "__main__":
    _main()
