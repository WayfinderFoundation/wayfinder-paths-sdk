"""Canned game-level slate pipeline: moneyline / total / spread for one game.

The game-market counterpart of :mod:`prop_slate` — run it instead of hand-modelling a
game. It fetches the matchup, builds team scoring rates from each side's **completed
events** (a provider-uniform source that works for every league with scores), models the
game with a sport-appropriate distribution, and compares against the **consensus
de-vigged sportsbook lines** pulled from the provider's odds feed (never web scrapes —
a live run burned us with fabricated web odds).

Model:
- expected scores from recent form: ``lam_home = (home_attack + away_concede)/2 * home_adv``
  (and symmetrically for the away side), attack/concede = goals/points per completed game.
- low-scoring sports (nhl/mlb/soccer): independent Poissons; moneyline from the joint
  grid with regulation ties split by strength (covers OT rules); total from the Poisson
  sum; spread from the margin grid.
- high-scoring sports (nba/nfl): normal margin/total with league-typical sigmas.

Output: per-market ``model_p`` vs ``book_p`` (consensus, de-vigged), edge / EV / Kelly,
``low_sample``/``suspect_edge`` flags, and — when the provider feed carries a
``polymarket`` vendor row — that line surfaced separately as the quasi-executable
reference. book numbers are INFORMATIONAL; executable EV is
``sports_props.market_edge(model_p, polymarket_price)``.

CLI:
    poetry run python -m wayfinder_paths.quant.game_slate \
        --sport nhl --game-id 3306714 --season 2025 --date 2026-06-14
"""

from __future__ import annotations

import asyncio
import json
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wayfinder_paths.quant import sports_props as sp
from wayfinder_paths.quant.prop_slate import _call, _next_cursor, _Pacer, _rows

SUSPECT_EDGE = 0.25
MIN_GAMES = 8
_RECENT_N = 25
_MAX_PAGES = 6

# Sport model config: distribution + home advantage + normal-sigma fallbacks.
# "draws": the moneyline is three-way (1X2) — regulation draws stand, never split.
_SPORT_CONFIG: dict[str, dict[str, Any]] = {
    "nhl": {"dist": "poisson", "home_adv": 1.05},
    "mlb": {"dist": "poisson", "home_adv": 1.04},
    "epl": {"dist": "poisson", "home_adv": 1.15, "draws": True},
    "laliga": {"dist": "poisson", "home_adv": 1.15, "draws": True},
    "seriea": {"dist": "poisson", "home_adv": 1.15, "draws": True},
    "bundesliga": {"dist": "poisson", "home_adv": 1.15, "draws": True},
    "ligue1": {"dist": "poisson", "home_adv": 1.15, "draws": True},
    "ucl": {"dist": "poisson", "home_adv": 1.12, "draws": True},
    "mls": {"dist": "poisson", "home_adv": 1.15, "draws": True},
    "worldcup": {"dist": "poisson", "home_adv": 1.05, "draws": True},
    "nba": {
        "dist": "normal",
        "home_adv": 1.015,
        "margin_sigma": 12.5,
        "total_sigma": 19.0,
    },
    "nfl": {
        "dist": "normal",
        "home_adv": 1.03,
        "margin_sigma": 13.5,
        "total_sigma": 13.5,
    },
    "wnba": {
        "dist": "normal",
        "home_adv": 1.015,
        "margin_sigma": 11.5,
        "total_sigma": 16.0,
    },
}


def _config(sport: str) -> dict[str, Any]:
    return _SPORT_CONFIG.get(str(sport).lower(), {"dist": "poisson", "home_adv": 1.05})


# ── event-shape normalization (leagues name fields differently) ──────────────


def event_teams(row: dict[str, Any]) -> tuple[dict, dict]:
    home = row.get("home_team") or {}
    away = row.get("away_team") or row.get("visitor_team") or {}
    return home, away


def event_scores(row: dict[str, Any]) -> tuple[Any, Any]:
    home = row.get("home_score", row.get("home_team_score"))
    away = row.get(
        "away_score", row.get("visitor_team_score", row.get("away_team_score"))
    )
    if home is None or away is None:  # MLB nests scores: {home,away}_team_data.runs
        home_data = row.get("home_team_data") or {}
        away_data = row.get("away_team_data") or {}
        home = home_data.get("runs") if home is None else home
        away = away_data.get("runs") if away is None else away
    return home, away


def event_date(row: dict[str, Any]) -> str:
    return str(row.get("game_date") or row.get("date") or row.get("datetime") or "")


def event_completed(row: dict[str, Any]) -> bool:
    state = str(row.get("game_state") or row.get("status") or "").lower()
    if any(tag in state for tag in ("off", "final", "ft", "ended", "complete")):
        home, away = event_scores(row)
        return home is not None and away is not None
    return False


def team_label(team: dict[str, Any]) -> str:
    return (
        team.get("abbreviation")
        or team.get("tricode")
        or team.get("full_name")
        or team.get("name")
        or "?"
    )


# ── probability models ───────────────────────────────────────────────────────


def _poisson_pmf(lam: float, k: int) -> float:
    return math.exp(-lam) * lam**k / math.factorial(k)


def poisson_game_probs(
    lam_home: float,
    lam_away: float,
    *,
    total_line: float,
    spread_line: float | None,
    grid: int = 20,
    split_ties: bool = True,
) -> dict[str, float]:
    """Joint-grid probabilities for moneyline / total / home-spread from two Poissons.

    With ``split_ties`` regulation ties are split by relative strength (covers OT/SO
    moneylines); soccer's three-way 1X2 passes ``split_ties=False`` and reads ``draw``.
    ``spread_line`` is the HOME spread (e.g. +1.5 means home covers if margin > -1.5).
    Whole-number lines push (bet refunded), so over/spread probabilities are
    conditioned on no-push — that is what a book's two-sided quote prices.
    """
    p_home = p_away = p_tie = 0.0
    p_over = p_total_push = 0.0
    p_home_cover = p_spread_push = 0.0
    pmf_h = [_poisson_pmf(lam_home, k) for k in range(grid + 1)]
    pmf_a = [_poisson_pmf(lam_away, k) for k in range(grid + 1)]
    for h in range(grid + 1):
        for a in range(grid + 1):
            p = pmf_h[h] * pmf_a[a]
            if h > a:
                p_home += p
            elif a > h:
                p_away += p
            else:
                p_tie += p
            if h + a > total_line:
                p_over += p
            elif h + a == total_line:
                p_total_push += p
            if spread_line is not None:
                if (h - a) > -spread_line:
                    p_home_cover += p
                elif (h - a) == -spread_line:
                    p_spread_push += p
    strength = lam_home / (lam_home + lam_away) if (lam_home + lam_away) > 0 else 0.5
    out = {
        "home_ml": (p_home + p_tie * strength) if split_ties else p_home,
        "away_ml": (p_away + p_tie * (1 - strength)) if split_ties else p_away,
        "draw": p_tie,
        "over": p_over / (1.0 - p_total_push) if p_total_push < 1.0 else 0.5,
        "total_push": p_total_push,
    }
    if spread_line is not None:
        out["home_spread"] = (
            p_home_cover / (1.0 - p_spread_push) if p_spread_push < 1.0 else 0.5
        )
        out["spread_push"] = p_spread_push
    return out


def _normal_two_sided(mu: float, sigma: float, line: float) -> float:
    """P(X > line | no push). Whole-number lines push on the exact score; approximate
    the push mass with a unit-width continuity bin around the line."""
    if line != int(line) or sigma <= 0:
        return sp.prob_over(mu, sigma, line)
    p_over = sp.prob_over(mu, sigma, line + 0.5)
    p_under = 1.0 - sp.prob_over(mu, sigma, line - 0.5)
    return p_over / (p_over + p_under) if (p_over + p_under) > 0 else 0.5


def normal_game_probs(
    lam_home: float,
    lam_away: float,
    *,
    total_line: float,
    spread_line: float | None,
    margin_sigma: float,
    total_sigma: float,
) -> dict[str, float]:
    margin_mu = lam_home - lam_away
    total_mu = lam_home + lam_away
    home_ml = sp.prob_over(margin_mu, margin_sigma, 0.0)
    out = {
        "home_ml": home_ml,
        "away_ml": 1.0 - home_ml,
        "over": _normal_two_sided(total_mu, total_sigma, total_line),
    }
    if spread_line is not None:
        out["home_spread"] = _normal_two_sided(margin_mu, margin_sigma, -spread_line)
    return out


# ── odds parsing (per-vendor flat rows -> consensus de-vigged lines) ─────────


def _median_devig(pairs: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Median de-vigged (first_p, second_p) over vendor odds pairs."""
    probs = []
    for first, second in pairs:
        try:
            probs.append(sp.devig_two_way(first, second))
        except (ValueError, ZeroDivisionError):
            continue
    if not probs:
        return None
    p1 = statistics.median(p[0] for p in probs)
    return p1, 1.0 - p1


def _devig_three_way(h: float, d: float, a: float) -> tuple[float, float, float]:
    """Normalize a 1X2 triple's implied probabilities (soccer moneyline with draw)."""
    ih, idr, ia = (
        sp.american_to_implied(h),
        sp.american_to_implied(d),
        sp.american_to_implied(a),
    )
    total = ih + idr + ia
    if total <= 0:
        raise ValueError("invalid 1X2 odds")
    return ih / total, idr / total, ia / total


def _nested_totals(row: dict[str, Any]):
    """Yield (line, over_odds, under_odds) from a row's nested market objects (soccer
    rows carry totals only there — the flat total_* fields are null)."""
    for mk in row.get("markets") or []:
        if (
            mk.get("type") != "total"
            or mk.get("period") != "match"
            or mk.get("scope") != "match"
            or "over/under" not in str(mk.get("key", ""))
        ):
            continue
        over = under = None
        for outcome in mk.get("outcomes") or []:
            if outcome.get("type") == "over":
                over = outcome.get("american_odds")
            elif outcome.get("type") == "under":
                under = outcome.get("american_odds")
        try:
            if over and under and mk.get("line_value") is not None:
                yield float(mk["line_value"]), float(over), float(under)
        except (TypeError, ValueError):
            continue


def parse_game_odds(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Consensus markets from per-vendor odds rows (+ the polymarket vendor row if any)."""
    ml_pairs: list[tuple[float, float]] = []
    ml_triples: list[tuple[float, float, float]] = []  # 1X2 (home, draw, away)
    total_by_line: dict[float, list[tuple[float, float]]] = {}
    spread_by_line: dict[float, list[tuple[float, float]]] = {}
    polymarket_row = None
    vendors = set()

    for row in rows:
        vendors.add(row.get("vendor"))
        if str(row.get("vendor")).lower() == "polymarket":
            polymarket_row = row
        try:
            if row.get("moneyline_home_odds") and row.get("moneyline_away_odds"):
                if row.get("moneyline_draw_odds"):
                    ml_triples.append(
                        (
                            float(row["moneyline_home_odds"]),
                            float(row["moneyline_draw_odds"]),
                            float(row["moneyline_away_odds"]),
                        )
                    )
                else:
                    ml_pairs.append(
                        (
                            float(row["moneyline_home_odds"]),
                            float(row["moneyline_away_odds"]),
                        )
                    )
            if (
                row.get("total_value")
                and row.get("total_over_odds")
                and row.get("total_under_odds")
            ):
                line = float(row["total_value"])
                total_by_line.setdefault(line, []).append(
                    (float(row["total_over_odds"]), float(row["total_under_odds"]))
                )
            for line, over, under in _nested_totals(row):
                total_by_line.setdefault(line, []).append((over, under))
            if (
                row.get("spread_home_value")
                and row.get("spread_home_odds")
                and row.get("spread_away_odds")
            ):
                line = float(row["spread_home_value"])
                spread_by_line.setdefault(line, []).append(
                    (float(row["spread_home_odds"]), float(row["spread_away_odds"]))
                )
        except (TypeError, ValueError):
            continue

    markets: dict[str, Any] = {"vendors": sorted(str(v) for v in vendors if v)}
    if ml_triples:  # three-way (1X2) takes precedence: a draw is a real outcome
        probs = []
        for h, d, a in ml_triples:
            try:
                probs.append(_devig_three_way(h, d, a))
            except (ValueError, ZeroDivisionError):
                continue
        if probs:
            mh = statistics.median(p[0] for p in probs)
            md = statistics.median(p[1] for p in probs)
            ma = statistics.median(p[2] for p in probs)
            norm = mh + md + ma
            markets["moneyline"] = {
                "home_p": mh / norm,
                "draw_p": md / norm,
                "away_p": ma / norm,
                "three_way": True,
                "n_vendors": len(probs),
            }
    elif ml_pairs:
        devig = _median_devig(ml_pairs)
        if devig:
            markets["moneyline"] = {
                "home_p": devig[0],
                "away_p": devig[1],
                "n_vendors": len(ml_pairs),
            }
    if total_by_line:
        # Modal line; ties (soccer carries every alternate line at full vendor count)
        # break toward the most balanced market — that IS the main line.
        max_n = max(len(v) for v in total_by_line.values())
        candidates = [ln for ln, v in total_by_line.items() if len(v) == max_n]

        def _imbalance(ln: float) -> float:
            devig = _median_devig(total_by_line[ln])
            return abs((devig[0] if devig else 1.0) - 0.5)

        line = min(candidates, key=_imbalance)
        devig = _median_devig(total_by_line[line])
        if devig:
            markets["total"] = {
                "line": line,
                "over_p": devig[0],
                "under_p": devig[1],
                "n_vendors": len(total_by_line[line]),
            }
    if spread_by_line:
        line = max(spread_by_line, key=lambda k: len(spread_by_line[k]))
        devig = _median_devig(spread_by_line[line])
        if devig:
            markets["spread"] = {
                "home_line": line,
                "home_p": devig[0],
                "away_p": devig[1],
                "n_vendors": len(spread_by_line[line]),
            }
    if polymarket_row is not None:
        try:
            if polymarket_row.get("moneyline_draw_odds"):
                ph, pd, pa = _devig_three_way(
                    float(polymarket_row["moneyline_home_odds"]),
                    float(polymarket_row["moneyline_draw_odds"]),
                    float(polymarket_row["moneyline_away_odds"]),
                )
                markets["polymarket_vendor"] = {
                    "home_ml_p": ph,
                    "draw_p": pd,
                    "away_ml_p": pa,
                }
            else:
                pm = sp.devig_two_way(
                    float(polymarket_row["moneyline_home_odds"]),
                    float(polymarket_row["moneyline_away_odds"]),
                )
                markets["polymarket_vendor"] = {"home_ml_p": pm[0], "away_ml_p": pm[1]}
        except (KeyError, TypeError, ValueError):
            pass
    return markets


# ── fetch ────────────────────────────────────────────────────────────────────


@dataclass
class GameSlate:
    sport: str
    game_id: int | str
    season: int
    home: dict[str, Any]
    away: dict[str, Any]
    home_form: dict[str, float]  # {"for": .., "against": .., "n": ..}
    away_form: dict[str, float]
    markets: dict[str, Any]
    date: str = ""
    flags: list[str] = field(default_factory=list)
    # MLB: probable starters inferred from the game's pitcher props (books only post
    # pitcher props for scheduled starters). {"name", "ra9", "starts", "factor"} per side.
    home_pitcher: dict[str, Any] | None = None
    away_pitcher: dict[str, Any] | None = None


# MLB starting pitchers dominate totals; team form alone misses them (a live eval lost
# to a baseline that simply named the 1.87-ERA starter). The adjustment multiplies the
# OPPOSING team's lambda by a shrunk, clipped starter-quality factor.
_LEAGUE_RA9 = 4.3  # MLB-wide earned runs per 9 innings, stable enough as a constant
_PITCHER_FACTOR_CLIP = (0.70, 1.30)
_PITCHER_SHRINK_STARTS = 6.0  # starts count where we trust the starter's own RA9 ~50%


def _pitcher_quality(logs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Starter RA9 from pitching appearances (er + pitching_outs), shrunk toward league."""
    starts = [
        lg
        for lg in logs
        if float(lg.get("pitching_outs") or 0) > 0
    ]
    outs = sum(float(lg.get("pitching_outs") or 0) for lg in starts)
    er = sum(float(lg.get("er") or 0) for lg in starts)
    if outs <= 0:
        return None
    ra9 = er * 27.0 / outs
    n = len(starts)
    weight = n / (n + _PITCHER_SHRINK_STARTS)
    blended = weight * ra9 + (1 - weight) * _LEAGUE_RA9
    factor = min(
        max(blended / _LEAGUE_RA9, _PITCHER_FACTOR_CLIP[0]), _PITCHER_FACTOR_CLIP[1]
    )
    return {"ra9": round(ra9, 2), "starts": n, "factor": round(factor, 3)}


async def _mlb_probable_pitchers(
    client, pacer, game_id: Any, season: int, home_name: str, away_name: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Infer the probable starters from the game's pitcher props, then rate them from
    their season pitching logs. Returns (home_pitcher, away_pitcher) or Nones."""
    payload = await _call(
        client,
        pacer,
        endpoint_id="data.player_props.list",
        sport="mlb",
        query={"game_id": game_id, "per_page": 100},
    )
    pitcher_ids = {
        row.get("player_id")
        for row in _rows(payload)
        if str(row.get("prop_type", "")).startswith("pitcher_")
        and row.get("player_id") is not None
    }
    await pacer.wait()
    if not pitcher_ids:
        return None, None

    stats_payload = await _call(
        client,
        pacer,
        endpoint_id="data.player_stats.list",
        sport="mlb",
        query={"player_ids": sorted(pitcher_ids), "seasons": [season], "per_page": 100},
    )
    await pacer.wait()
    logs_by_pid: dict[Any, list[dict[str, Any]]] = {}
    names: dict[Any, str] = {}
    teams: dict[Any, str] = {}
    for lg in _rows(stats_payload):
        pid = (lg.get("player") or {}).get("id")
        if pid is None:
            continue
        logs_by_pid.setdefault(pid, []).append(lg)
        player = lg.get("player") or {}
        names.setdefault(pid, str(player.get("full_name") or player.get("last_name") or pid))
        if lg.get("team_name"):
            teams[pid] = str(lg["team_name"])

    home_p = away_p = None
    for pid, logs in logs_by_pid.items():
        quality = _pitcher_quality(logs)
        if quality is None:
            continue
        entry = {"name": names.get(pid, str(pid)), **quality}
        team = teams.get(pid, "")
        if team == home_name and home_p is None:
            home_p = entry
        elif team == away_name and away_p is None:
            away_p = entry
    return home_p, away_p


async def _team_form(
    client, pacer, sport: str, team_id: Any, season: int, exclude_game: Any
) -> dict[str, float]:
    """Scored/conceded per completed game from the team's season events. The rows are
    client-filtered by team id below, so a provider that ignores ``team_ids`` is fine;
    one that returns NOTHING for unknown params gets an unfiltered retry."""
    rows: list[dict[str, Any]] = []
    for use_team_filter in (True, False):
        cursor = None
        for _ in range(_MAX_PAGES):
            query: dict[str, Any] = {"seasons": [season], "per_page": 100}
            if use_team_filter:
                query["team_ids"] = [team_id]
            if cursor is not None:
                query["cursor"] = cursor
            payload = await _call(
                client, pacer, endpoint_id="data.events.list", sport=sport, query=query
            )
            rows.extend(_rows(payload))
            cursor = _next_cursor(payload)
            await pacer.wait()
            if cursor is None:
                break
        if rows:
            break

    completed = []
    for row in rows:
        if row.get("id") == exclude_game or not event_completed(row):
            continue
        home, away = event_teams(row)
        hs, as_ = event_scores(row)
        if home.get("id") == team_id:
            completed.append((event_date(row), float(hs), float(as_)))
        elif away.get("id") == team_id:
            completed.append((event_date(row), float(as_), float(hs)))
    completed.sort(key=lambda t: t[0], reverse=True)
    recent = completed[:_RECENT_N]
    if not recent:
        return {"for": 0.0, "against": 0.0, "n": 0}
    return {
        "for": sum(t[1] for t in recent) / len(recent),
        "against": sum(t[2] for t in recent) / len(recent),
        "n": len(recent),
    }


async def fetch_game_slate(
    sport: str,
    game_id: int | str,
    season: int,
    *,
    date: str | None = None,
    client: Any = None,
    pace_s: float = 1.0,
) -> GameSlate:
    if client is None:
        from wayfinder_paths.core.clients.SportsClient import SPORTS_CLIENT

        client = SPORTS_CLIENT
    pacer = _Pacer(pace_s)

    # 1) the game row: by-id GET where supported; date-filtered list otherwise (NHL etc.)
    game_row: dict[str, Any] | None = None
    try:
        payload = await _call(
            client,
            pacer,
            endpoint_id="data.event.get",
            sport=sport,
            path_params={"id": game_id},
        )
        data = payload.get("data", {})
        game_row = data.get("data", data) or None
    except Exception:  # noqa: BLE001 - fall back to a date-filtered list lookup
        game_row = None
    if not game_row or not (event_teams(game_row)[0]):
        if not date:
            raise ValueError(
                "Could not fetch the game by id (this league may not support it) — pass the game date."
            )
        await pacer.wait()
        payload = await _call(
            client,
            pacer,
            endpoint_id="data.events.list",
            sport=sport,
            query={"dates": [date], "per_page": 50},
        )
        for row in _rows(payload):
            if str(row.get("id")) == str(game_id):
                game_row = row
                break
        if not game_row:
            raise ValueError(f"Game {game_id} not found on {date} for {sport}.")
    home, away = event_teams(game_row)
    await pacer.wait()

    # 2) recent form per team from completed events
    home_form = await _team_form(client, pacer, sport, home.get("id"), season, game_id)
    away_form = await _team_form(client, pacer, sport, away.get("id"), season, game_id)

    # 3) provider odds for the game. Filter params differ by league (NHL: game_ids
    # array, soccer: scalar game_id, worldcup: match_ids array) — send every form, the
    # provider ignores the unused ones. Some surfaces ignore ALL filters, so also
    # client-filter the rows back to this game.
    odds_rows: list[dict[str, Any]] = []
    try:
        payload = await _call(
            client,
            pacer,
            endpoint_id="data.odds.list",
            sport=sport,
            query={
                "game_id": game_id,
                "game_ids": [game_id],
                "match_ids": [game_id],
                "per_page": 100,
            },
        )
        odds_rows = [
            row
            for row in _rows(payload)
            if str(row.get("game_id") or row.get("match_id") or game_id) == str(game_id)
        ]
    except Exception:  # noqa: BLE001 - model-only output is still useful without odds
        pass
    markets = parse_game_odds(odds_rows)

    # 4) MLB only: starting pitchers dominate totals — infer probables from the game's
    # pitcher props and rate them from season logs. Soft: missing data just flags.
    home_pitcher = away_pitcher = None
    if str(sport).lower() == "mlb":
        try:
            home_pitcher, away_pitcher = await _mlb_probable_pitchers(
                client,
                pacer,
                game_id,
                season,
                str(home.get("display_name") or home.get("full_name") or ""),
                str(away.get("display_name") or away.get("full_name") or ""),
            )
        except Exception:  # noqa: BLE001 - pitcher layer is an enhancement, never fatal
            pass

    flags: list[str] = []
    if home_form["n"] < MIN_GAMES or away_form["n"] < MIN_GAMES:
        flags.append(f"low_sample(h={home_form['n']},a={away_form['n']})")
    if not markets.get("moneyline"):
        flags.append("no_provider_odds")
    if str(sport).lower() == "mlb" and (home_pitcher is None or away_pitcher is None):
        flags.append("pitchers_not_modeled")

    return GameSlate(
        sport=sport,
        game_id=game_id,
        season=season,
        home=home,
        away=away,
        home_form=home_form,
        away_form=away_form,
        markets=markets,
        date=event_date(game_row),
        flags=flags,
        home_pitcher=home_pitcher,
        away_pitcher=away_pitcher,
    )


# ── score ────────────────────────────────────────────────────────────────────


@dataclass
class MarketView:
    market: str  # moneyline_home|moneyline_draw|moneyline_away|over|under|spread_home|spread_away
    line: float | None
    model_p: float | None  # None = odds-only view (no form basis for a model)
    book_p: float | None
    book_edge: float | None
    flags: list[str]


@dataclass
class GameResult:
    slate: GameSlate
    lam_home: float
    lam_away: float
    views: list[MarketView]
    note: str = (
        "book probabilities are consensus de-vigged SPORTSBOOK lines — informational only. "
        "Executable EV must be priced on Polymarket: market_edge(model_p, polymarket_price)."
    )


def score_game_slate(slate: GameSlate) -> GameResult:
    cfg = _config(slate.sport)
    draws = bool(cfg.get("draws"))
    hf, af = slate.home_form, slate.away_form
    # No completed games for a side (tournament just started, expansion team, ...)
    # means the form model has NO basis — emit odds-only views, never degenerate λ=0.
    form_ok = hf["n"] > 0 and af["n"] > 0
    if not form_ok and "no_form_model" not in slate.flags:
        slate.flags.append("no_form_model")

    league_avg = (hf["for"] + hf["against"] + af["for"] + af["against"]) / 4 or 1.0
    lam_home = max(((hf["for"] + af["against"]) / 2) * cfg["home_adv"], 0.05)
    lam_away = max(((af["for"] + hf["against"]) / 2) / cfg["home_adv"], 0.05)
    # The OPPOSING starter's quality scales each side's expected runs (MLB only;
    # factor is shrunk toward 1.0 by starts count and clipped — see _pitcher_quality).
    if slate.home_pitcher:
        lam_away = max(lam_away * float(slate.home_pitcher["factor"]), 0.05)
    if slate.away_pitcher:
        lam_home = max(lam_home * float(slate.away_pitcher["factor"]), 0.05)

    total = slate.markets.get("total") or {}
    spread = slate.markets.get("spread") or {}
    total_line = float(total.get("line") or round(league_avg * 2) + 0.5)
    spread_line = (
        float(spread["home_line"]) if spread.get("home_line") is not None else None
    )

    probs: dict[str, float] = {}
    if form_ok:
        if cfg["dist"] == "poisson":
            probs = poisson_game_probs(
                lam_home,
                lam_away,
                total_line=total_line,
                spread_line=spread_line,
                split_ties=not draws,
            )
        else:
            probs = normal_game_probs(
                lam_home,
                lam_away,
                total_line=total_line,
                spread_line=spread_line,
                margin_sigma=cfg.get("margin_sigma", 12.0),
                total_sigma=cfg.get("total_sigma", 18.0),
            )

    ml = slate.markets.get("moneyline") or {}
    views: list[MarketView] = []

    def _view(market: str, line, model_p, book_p) -> None:
        edge = (
            (model_p - book_p) if (model_p is not None and book_p is not None) else None
        )
        flags = list(slate.flags)
        if edge is not None and abs(edge) > SUSPECT_EDGE:
            flags.append("suspect_edge")
        views.append(
            MarketView(
                market=market,
                line=line,
                model_p=round(model_p, 4) if model_p is not None else None,
                book_p=round(book_p, 4) if book_p is not None else None,
                book_edge=round(edge, 4) if edge is not None else None,
                flags=flags,
            )
        )

    _view("moneyline_home", None, probs.get("home_ml"), ml.get("home_p"))
    if draws or ml.get("draw_p") is not None:
        _view("moneyline_draw", None, probs.get("draw"), ml.get("draw_p"))
    _view("moneyline_away", None, probs.get("away_ml"), ml.get("away_p"))
    _view("over", total_line, probs.get("over"), total.get("over_p"))
    over = probs.get("over")
    _view(
        "under", total_line, (1.0 - over) if over is not None else None, total.get("under_p")
    )
    if spread_line is not None and (not form_ok or "home_spread" in probs):
        _view("spread_home", spread_line, probs.get("home_spread"), spread.get("home_p"))
        cover = probs.get("home_spread")
        _view(
            "spread_away",
            -spread_line,
            (1.0 - cover) if cover is not None else None,
            spread.get("away_p"),
        )

    return GameResult(
        slate=slate,
        lam_home=round(lam_home, 3) if form_ok else 0.0,
        lam_away=round(lam_away, 3) if form_ok else 0.0,
        views=views,
    )


# ── render / artifacts / CLI ─────────────────────────────────────────────────


def render_information(slate: GameSlate) -> str:
    """The FACTS section: data + market math only, no modeling opinions. This is the
    primary product — the agent decides how to model from these ingredients."""
    home, away = team_label(slate.home), team_label(slate.away)
    lines = [
        f"GAME SLATE — {slate.sport} game {slate.game_id}: {away} @ {home} "
        f"({slate.date or 'date ?'})",
        "",
        "== INFORMATION (facts + market math — model it however the question demands) ==",
        f"form (last completed games): {home} scored {slate.home_form['for']:.2f} / "
        f"allowed {slate.home_form['against']:.2f} per game (n={slate.home_form['n']}) | "
        f"{away} {slate.away_form['for']:.2f} / {slate.away_form['against']:.2f} "
        f"(n={slate.away_form['n']})",
    ]
    if slate.home_pitcher or slate.away_pitcher:
        parts = []
        for side, p in (("home", slate.home_pitcher), ("away", slate.away_pitcher)):
            if p:
                parts.append(f"{side}: {p['name']} (RA9 {p['ra9']}, {p['starts']} starts)")
        lines.append("probable starters (inferred from pitcher props): " + " | ".join(parts))
    elif str(slate.sport).lower() == "mlb":
        lines.append(
            "probable starters: UNKNOWN (no pitcher props posted yet) — starters dominate "
            "MLB totals; any model without them is form-only"
        )
    vendors = slate.markets.get("vendors", [])
    lines.append(f"books ({len(vendors)}): {', '.join(vendors) or 'none'}")
    ml = slate.markets.get("moneyline") or {}
    if ml:
        draw = f" / draw {ml['draw_p']:.3f}" if ml.get("draw_p") is not None else ""
        lines.append(
            f"de-vigged consensus ML: home {ml['home_p']:.3f}{draw} / away {ml['away_p']:.3f} "
            f"({ml.get('n_vendors', '?')} vendors)"
        )
    total = slate.markets.get("total") or {}
    if total:
        lines.append(
            f"de-vigged consensus total {total['line']}: over {total['over_p']:.3f} / "
            f"under {total['under_p']:.3f}"
        )
    spread = slate.markets.get("spread") or {}
    if spread:
        lines.append(
            f"de-vigged consensus spread home {spread['home_line']:+}: "
            f"cover {spread['home_p']:.3f}"
        )
    pm = slate.markets.get("polymarket_vendor")
    if pm:
        draw = f" / draw {pm['draw_p']:.3f}" if pm.get("draw_p") is not None else ""
        lines.append(
            f"polymarket vendor line (quasi-executable reference): "
            f"home {pm['home_ml_p']:.3f}{draw} / away {pm['away_ml_p']:.3f}"
        )
    if slate.flags:
        lines.append(f"flags: {', '.join(slate.flags)}")
    return "\n".join(lines)


def render_game(result: GameResult, *, data_only: bool = False) -> str:
    s = result.slate
    lines = [render_information(s)]
    if data_only:
        lines.append(
            "\nNOTE: information only — de-vigged book numbers are market FACTS "
            "(informational; the executable venue is Polymarket). Build your own view "
            "from these ingredients and gate it through sports_posterior."
        )
        return "\n".join(lines)
    lines.append("")
    lines.append(
        "== REFERENCE MODEL (one opinion: completed-game form Poisson"
        + (
            " + starter RA9 factors"
            if (s.home_pitcher or s.away_pitcher)
            else ""
        )
        + " — adjust or replace it with your own view) =="
    )
    home, away = team_label(s.home), team_label(s.away)
    lines.append(f"expected: {away} {result.lam_away} @ {home} {result.lam_home}")
    lines.append(f"{'market':<16}{'line':>7}  {'model':>6}  {'book':>6}  {'edge':>7}  flags")
    for v in result.views:
        line = f"{v.line:+.1f}" if isinstance(v.line, float) else "-"
        model = f"{v.model_p:.3f}" if v.model_p is not None else "  n/a"
        book = f"{v.book_p:.3f}" if v.book_p is not None else "  n/a"
        edge = f"{v.book_edge:+.3f}" if v.book_edge is not None else "    n/a"
        lines.append(
            f"{v.market:<16}{line:>7}  {model:>6}  {book:>6}  {edge:>7}  {','.join(v.flags)}"
        )
    lines.append("\nNOTE: " + result.note)
    return "\n".join(lines)


def game_rows(result: GameResult) -> list[dict[str, Any]]:
    return [
        {
            "market": v.market,
            "line": v.line,
            "model_p": v.model_p,
            "book_p": v.book_p,
            "book_edge": v.book_edge,
            "flags": ",".join(v.flags),
        }
        for v in result.views
    ]


async def run_game_slate(
    sport: str,
    game_id: int | str,
    season: int,
    *,
    date: str | None = None,
    client: Any = None,
    out_dir: str | Path | None = None,
) -> tuple[GameResult, list[str]]:
    slate = await fetch_game_slate(sport, game_id, season, date=date, client=client)
    result = score_game_slate(slate)
    artifacts: list[str] = []
    if out_dir:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"game_slate_{game_id}.json"
        path.write_text(
            json.dumps(
                {
                    "sport": sport,
                    "game_id": game_id,
                    "season": season,
                    "date": slate.date,
                    "home": team_label(slate.home),
                    "away": team_label(slate.away),
                    # facts an agent models from (the primary product)
                    "information": {
                        "home_form": slate.home_form,
                        "away_form": slate.away_form,
                        "home_pitcher": slate.home_pitcher,
                        "away_pitcher": slate.away_pitcher,
                        "markets": slate.markets,
                        "flags": slate.flags,
                    },
                    # one labeled opinion, not truth
                    "reference_model": {
                        "kind": "completed-game form Poisson + starter RA9 factors",
                        "lam_home": result.lam_home,
                        "lam_away": result.lam_away,
                        "views": game_rows(result),
                    },
                    "note": result.note,
                },
                indent=2,
                default=str,
            )
        )
        artifacts.append(str(path))
    return result, artifacts


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Gather a game's information (form, starters, de-vigged markets) "
        "plus an optional labeled reference model."
    )
    parser.add_argument("--sport", required=True)
    parser.add_argument(
        "--game-id", required=True, help="game id, or comma-separated ids for a slate"
    )
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument(
        "--date",
        default=None,
        help="Game date YYYY-MM-DD (required for leagues without by-id game lookup)",
    )
    parser.add_argument(
        "--data-only",
        action="store_true",
        help="print the INFORMATION section only (no reference model)",
    )
    parser.add_argument("--out", default=".wayfinder_runs/sports")
    args = parser.parse_args()

    all_artifacts: list[str] = []
    for game_id in str(args.game_id).split(","):
        result, artifacts = asyncio.run(
            run_game_slate(
                args.sport, game_id.strip(), args.season, date=args.date, out_dir=args.out
            )
        )
        all_artifacts.extend(artifacts)
        print(render_game(result, data_only=args.data_only))
        print()
    print("artifacts:", " ".join(all_artifacts) if all_artifacts else "(none)")


if __name__ == "__main__":
    _main()
