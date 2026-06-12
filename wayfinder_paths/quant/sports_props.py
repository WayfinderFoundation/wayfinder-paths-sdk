"""Sports player-prop projection + betting-edge model.

Projects a player's stat for an upcoming game from recency-weighted, minutes-based recent
game logs blended with the season baseline, applies opponent-defense + pace adjustment,
turns the projection into ``P(over the line)`` via a normal (counting stats) or Poisson
(low-count stats) distribution, removes the sportsbook vig, and reports edge / EV / Kelly.

Pure stdlib (no numpy/scipy) so it is importable anywhere and unit-testable. Designed to be
driven by live provider data (player props + ``player_stats`` game logs + ``season_averages``
+ ``team_season_averages``) and reused by the ``wayfinder-quant`` agent, mirroring
``polymarket_edge``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_SQRT2 = math.sqrt(2.0)

# prop_type -> the season/game-log stat keys that compose it (combos sum their parts).
# NBA types map to NBA log fields; MLB types to MLB log fields (names don't collide).
# "singles" is derived by the pipeline (hits - doubles - triples - hr) onto each log row.
PROP_STATS: dict[str, tuple[str, ...]] = {
    # NBA
    "points": ("pts",),
    "rebounds": ("reb",),
    "assists": ("ast",),
    "steals": ("stl",),
    "blocks": ("blk",),
    "threes": ("fg3m",),
    "turnovers": ("turnover",),
    "points_rebounds_assists": ("pts", "reb", "ast"),
    "points_rebounds": ("pts", "reb"),
    "points_assists": ("pts", "ast"),
    "rebounds_assists": ("reb", "ast"),
    "steals_blocks": ("stl", "blk"),
    # MLB batters
    "hits": ("hits",),
    "home_runs": ("hr",),
    "rbis": ("rbi",),
    "runs_scored": ("runs",),
    "singles": ("singles",),
    "doubles": ("doubles",),
    "triples": ("triples",),
    "walks": ("bb",),
    "stolen_bases": ("stolen_bases",),
    "total_bases": ("total_bases",),
    "hits_runs_rbis": ("hits", "runs", "rbi"),
    # MLB pitchers
    "pitcher_strikeouts": ("p_k",),
    "pitcher_earned_runs": ("er",),
    "pitcher_hits_allowed": ("p_hits",),
    "pitcher_outs": ("pitching_outs",),
}
# Low-count stats modelled as Poisson; everything else as a normal approximation.
# All MLB counting props are Poisson territory (means ~0.2-5) except pitcher_outs (~15-18).
_POISSON_PROPS = frozenset(
    {
        "steals",
        "blocks",
        "threes",
        "turnovers",
        "steals_blocks",
        "hits",
        "home_runs",
        "rbis",
        "runs_scored",
        "singles",
        "doubles",
        "triples",
        "walks",
        "stolen_bases",
        "total_bases",
        "hits_runs_rbis",
        "pitcher_strikeouts",
        "pitcher_earned_runs",
        "pitcher_hits_allowed",
    }
)
# Prop families whose projection responds to opponent scoring defense (def_rating).
_SCORING_PROPS = frozenset(
    {"points", "points_rebounds_assists", "points_rebounds", "points_assists", "threes"}
)


# ── odds ─────────────────────────────────────────────────────────────────────


def american_to_implied(odds: float) -> float:
    """Implied win probability from American odds (includes vig)."""
    odds = float(odds)
    if odds == 0:
        raise ValueError("odds must be non-zero")
    return 100.0 / (odds + 100.0) if odds > 0 else (-odds) / ((-odds) + 100.0)


def american_payout(odds: float) -> float:
    """Net profit per $1 staked (decimal payout minus 1)."""
    odds = float(odds)
    if odds == 0:
        raise ValueError("odds must be non-zero")
    return odds / 100.0 if odds > 0 else 100.0 / (-odds)


def devig_two_way(over_odds: float, under_odds: float) -> tuple[float, float]:
    """Remove vig from a two-way market -> the book's true (P_over, P_under), summing to 1."""
    io = american_to_implied(over_odds)
    iu = american_to_implied(under_odds)
    total = io + iu
    if total <= 0:
        raise ValueError("invalid odds")
    return io / total, iu / total


# ── distributions ────────────────────────────────────────────────────────────


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / _SQRT2))


def _poisson_cdf(k: int, mean: float) -> float:
    if mean <= 0:
        return 1.0
    term = math.exp(-mean)
    total = term
    for i in range(1, k + 1):
        term *= mean / i
        total += term
    return min(total, 1.0)


def prob_over(
    mean: float, std: float, line: float, *, distribution: str = "normal"
) -> float:
    """P(stat strictly exceeds the line). Lines are X.5, so no continuity correction needed."""
    line = float(line)
    if distribution == "poisson":
        return max(0.0, min(1.0, 1.0 - _poisson_cdf(math.floor(line), max(mean, 0.0))))
    if std <= 0:
        return 1.0 if mean > line else 0.0
    return max(0.0, min(1.0, 1.0 - _normal_cdf((line - mean) / std)))


def pick_distribution(prop_type: str) -> str:
    return "poisson" if prop_type in _POISSON_PROPS else "normal"


def stat_keys(prop_type: str) -> tuple[str, ...] | None:
    return PROP_STATS.get(prop_type)


# ── adjustments ──────────────────────────────────────────────────────────────


def pace_factor(team_pace: float, opp_pace: float, league_pace: float) -> float:
    """Game-pace multiplier on counting stats: the two teams' average pace vs the league."""
    if league_pace <= 0:
        return 1.0
    return ((float(team_pace) + float(opp_pace)) / 2.0) / float(league_pace)


def opponent_factor(
    opp_def_rating: float, league_def_rating: float, *, weight: float = 1.0
) -> float:
    """Opponent scoring-defense multiplier. def_rating = pts allowed / 100 poss, so a higher
    opponent def_rating (worse defense) inflates a scoring projection. ``weight`` dampens it."""
    if league_def_rating <= 0:
        return 1.0
    raw = float(opp_def_rating) / float(league_def_rating)
    return 1.0 + weight * (raw - 1.0)


# ── projection ───────────────────────────────────────────────────────────────


def parse_minutes(value: Any) -> float:
    """Parse minutes played: 'MM:SS' string or a number -> float minutes."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return 0.0
    if ":" in s:
        mm, _, ss = s.partition(":")
        try:
            return int(mm) + int(ss) / 60.0
        except ValueError:
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _recency_weights(n: int, half_life: float) -> list[float]:
    # index 0 = most recent game; weight decays by an exponential half-life.
    if n <= 0:
        return []
    decay = math.log(2.0) / max(half_life, 1e-9)
    return [math.exp(-decay * i) for i in range(n)]


def _wmean(values: Sequence[float], weights: Sequence[float]) -> float:
    tw = sum(weights)
    return (
        sum(v * w for v, w in zip(values, weights, strict=False)) / tw
        if tw > 0
        else 0.0
    )


@dataclass
class Projection:
    mean: float
    std: float
    n: int
    proj_minutes: float
    minutes_cv: float  # coefficient of variation of minutes (volatility flag)


def project_stat(
    game_logs: Sequence[Mapping[str, Any]],
    season_avg: Mapping[str, Any] | None,
    keys: Sequence[str],
    *,
    recent_n: int = 15,
    half_life: float = 5.0,
    shrinkage: float = 6.0,
    opponent_factor: float = 1.0,
    pace_factor: float = 1.0,
    std_floor_frac: float = 0.25,
    exposure_key: str = "min",
) -> Projection:
    """Project the (possibly combined) stat for the next game.

    Mean = projected exposure x recency-weighted per-exposure rate (shrunk toward the
    season rate by sample size) x opponent x pace. Exposure is minutes for NBA, plate
    appearances for MLB batters, outs recorded for MLB pitchers (``exposure_key``) — a
    game counts only if the player had exposure, which also splits two-way players'
    batting vs pitching appearances. Std comes from the dispersion of the recent
    per-game totals (so combo correlation is captured empirically), floored to avoid
    overconfidence.
    """
    # Most-recent-first per-game totals + exposure for games actually played.
    played = []
    for log in game_logs:
        mins = parse_minutes(log.get(exposure_key))
        if mins <= 0:
            continue
        total = sum(float(log.get(k) or 0.0) for k in keys)
        played.append((total, mins))
        if len(played) >= recent_n:
            break

    n = len(played)
    if n == 0:
        # No usable logs: fall back to the season per-game average if present.
        season_total = sum(float((season_avg or {}).get(k) or 0.0) for k in keys)
        return Projection(
            mean=season_total * opponent_factor * pace_factor,
            std=max(season_total * std_floor_frac, 1.0),
            n=0,
            proj_minutes=0.0,
            minutes_cv=0.0,
        )

    weights = _recency_weights(n, half_life)
    totals = [t for t, _ in played]
    minutes = [m for _, m in played]
    proj_minutes = _wmean(minutes, weights)
    recent_rate = _wmean([t / m for t, m in played], weights)

    # Season per-exposure rate for shrinkage (regress small samples toward season form).
    season_rate = recent_rate
    if season_avg:
        s_min = parse_minutes(season_avg.get(exposure_key))
        s_total = sum(float(season_avg.get(k) or 0.0) for k in keys)
        if s_min > 0:
            season_rate = s_total / s_min
    w_recent = n / (n + shrinkage)
    blended_rate = w_recent * recent_rate + (1.0 - w_recent) * season_rate

    mean = proj_minutes * blended_rate * opponent_factor * pace_factor

    # Std from recent per-game dispersion (empirical; captures combo correlation), floored.
    rmean = _wmean(totals, weights)
    var = _wmean([(t - rmean) ** 2 for t in totals], weights)
    std = max(math.sqrt(max(var, 0.0)), std_floor_frac * max(mean, 1.0))

    min_mean = _wmean(minutes, weights)
    min_var = _wmean([(m - min_mean) ** 2 for m in minutes], weights)
    minutes_cv = math.sqrt(max(min_var, 0.0)) / min_mean if min_mean > 0 else 0.0

    return Projection(
        mean=mean, std=std, n=n, proj_minutes=proj_minutes, minutes_cv=minutes_cv
    )


# ── value (edge / EV / Kelly) ────────────────────────────────────────────────


@dataclass
class PropValue:
    side: str  # "OVER" | "UNDER"
    model_p: float  # model probability of the chosen side
    book_p: float  # de-vigged book probability of the chosen side
    edge: float  # model_p - book_p
    ev: float  # expected profit per $1 staked
    kelly: float  # fractional Kelly stake (>=0)


def _side_value(
    model_p: float, payout: float, kelly_fraction: float
) -> tuple[float, float]:
    ev = model_p * payout - (1.0 - model_p)
    b = payout
    kelly_full = (model_p * (b + 1.0) - 1.0) / b if b > 0 else 0.0
    return ev, max(0.0, kelly_full) * kelly_fraction


def prop_value(
    model_p_over: float,
    book_p_over: float,
    over_odds: float,
    under_odds: float,
    *,
    kelly_fraction: float = 0.25,
) -> PropValue:
    """Pick the side with positive edge and report edge / EV / Kelly."""
    model_p_over = max(0.0, min(1.0, model_p_over))
    over_ev, over_kelly = _side_value(
        model_p_over, american_payout(over_odds), kelly_fraction
    )
    under_p = 1.0 - model_p_over
    under_ev, under_kelly = _side_value(
        under_p, american_payout(under_odds), kelly_fraction
    )
    over_edge = model_p_over - book_p_over
    under_edge = under_p - (1.0 - book_p_over)
    if over_edge >= under_edge:
        return PropValue(
            "OVER", model_p_over, book_p_over, over_edge, over_ev, over_kelly
        )
    return PropValue(
        "UNDER", under_p, 1.0 - book_p_over, under_edge, under_ev, under_kelly
    )


@dataclass
class MarketEdge:
    side: str  # "YES" | "NO"
    model_p: float  # model probability of the chosen side
    market_price: float  # executable prediction-market price of the chosen side (0..1)
    edge: float  # model_p - market_price
    ev: float  # expected profit per $1 staked
    kelly: float  # fractional Kelly stake (>=0)


def market_edge(
    model_p_yes: float, market_price_yes: float, *, kelly_fraction: float = 0.25
) -> MarketEdge:
    """Edge of a model probability against an *executable* prediction-market (Polymarket) price.

    Unlike a sportsbook line, a Polymarket YES price IS the implied probability AND the cost to
    buy a $1-payout share, so the edge is simply ``model_p - price`` and the payout is
    ``(1 - price) / price``. Picks YES or NO by whichever side the model favors over the market.
    """
    p_yes = max(0.0, min(1.0, model_p_yes))
    q_yes = max(1e-6, min(1.0 - 1e-6, market_price_yes))
    yes_edge = p_yes - q_yes
    no_edge = (1.0 - p_yes) - (1.0 - q_yes)
    if yes_edge >= no_edge:
        side, p, price = "YES", p_yes, q_yes
    else:
        side, p, price = "NO", 1.0 - p_yes, 1.0 - q_yes
    payout = (1.0 - price) / price  # net profit per $1 staked
    ev = p * payout - (1.0 - p)
    kelly = max(0.0, (p * (payout + 1.0) - 1.0) / payout) * kelly_fraction
    return MarketEdge(side, p, price, p - price, ev, kelly)


# ── orchestration ────────────────────────────────────────────────────────────


@dataclass
class PropScore:
    player_id: Any
    prop_type: str
    line: float
    side: str
    model_p: float
    book_p: float
    edge: float
    ev: float
    kelly: float
    projection: Projection
    flags: list[str]


def score_prop(
    prop: Mapping[str, Any],
    game_logs: Sequence[Mapping[str, Any]],
    season_avg: Mapping[str, Any] | None,
    *,
    opponent_factor: float = 1.0,
    pace_factor: float = 1.0,
    injured: bool = False,
    min_games: int = 5,
    kelly_fraction: float = 0.25,
    exposure_key: str = "min",
) -> PropScore | None:
    """Score one prop. ``prop`` needs prop_type, line, over_odds, under_odds, player_id.

    Returns None for props we cannot model (unknown type, missing odds). ``flags`` records
    soft warnings (injured, low sample, volatile minutes) without dropping the bet.
    """
    prop_type = str(prop.get("prop_type") or "")
    keys = stat_keys(prop_type)
    if keys is None:
        return None
    try:
        line = float(prop["line"])
        over_odds = float(prop["over_odds"])
        under_odds = float(prop["under_odds"])
    except (KeyError, TypeError, ValueError):
        return None

    opp = opponent_factor if prop_type in _SCORING_PROPS else 1.0
    proj = project_stat(
        game_logs,
        season_avg,
        keys,
        opponent_factor=opp,
        pace_factor=pace_factor,
        exposure_key=exposure_key,
    )
    model_p_over = prob_over(
        proj.mean, proj.std, line, distribution=pick_distribution(prop_type)
    )
    book_p_over, _ = devig_two_way(over_odds, under_odds)
    value = prop_value(
        model_p_over, book_p_over, over_odds, under_odds, kelly_fraction=kelly_fraction
    )

    flags: list[str] = []
    if injured:
        flags.append("injured")
    if proj.n < min_games:
        flags.append(f"low_sample({proj.n})")
    if proj.minutes_cv > 0.35:
        flags.append("volatile_minutes")

    return PropScore(
        player_id=prop.get("player_id"),
        prop_type=prop_type,
        line=line,
        side=value.side,
        model_p=value.model_p,
        book_p=value.book_p,
        edge=value.edge,
        ev=value.ev,
        kelly=value.kelly,
        projection=proj,
        flags=flags,
    )
