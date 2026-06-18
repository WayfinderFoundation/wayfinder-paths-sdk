"""Small sports rating helpers with uncertainty bands."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class Rating:
    entity_id: str
    rating: float
    rd: float
    last_played: str | None = None
    games: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RatingConfig:
    base_rating: float = 1500.0
    base_rd: float = 250.0
    min_rd: float = 40.0
    max_rd: float = 350.0
    k: float = 20.0
    home_advantage_points: float = 0.0
    draw_width: float = 0.0
    recency_half_life_days: float = 180.0
    margin_weight: float = 0.0
    rd_growth_per_day: float = 0.8


DEFAULT_CONFIGS = {
    "nba": RatingConfig(home_advantage_points=2.0, margin_weight=0.05),
    "nhl": RatingConfig(home_advantage_points=0.12),
    "mlb": RatingConfig(home_advantage_points=0.04),
    "soccer": RatingConfig(home_advantage_points=45.0, draw_width=0.85),
    "worldcup": RatingConfig(home_advantage_points=0.0, draw_width=0.90),
}


def expected_binary(
    a: Rating,
    b: Rating,
    *,
    home_advantage_points: float = 0.0,
) -> float:
    diff = float(a.rating) + float(home_advantage_points) - float(b.rating)
    return 1.0 / (1.0 + 10.0 ** (-diff / 400.0))


def expected_1x2(
    home: Rating,
    away: Rating,
    *,
    home_advantage_points: float,
    draw_width: float,
) -> dict[str, float]:
    binary = expected_binary(home, away, home_advantage_points=home_advantage_points)
    closeness = max(0.0, 1.0 - abs(binary - 0.5) * 2.0)
    draw = min(0.36, max(0.05, float(draw_width) * 0.28 * closeness))
    non_draw = 1.0 - draw
    home_p = non_draw * binary
    away_p = non_draw * (1.0 - binary)
    return {"home": home_p, "draw": draw, "away": away_p}


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(text).astimezone(UTC)


def _grow_rd(rating: Rating, config: RatingConfig, as_of: datetime) -> Rating:
    last = _parse_date(rating.last_played)
    if last is None:
        return rating
    days = max((as_of - last).days, 0)
    grown = min(config.max_rd, rating.rd + days * config.rd_growth_per_day)
    return Rating(
        entity_id=rating.entity_id,
        rating=rating.rating,
        rd=grown,
        last_played=rating.last_played,
        games=rating.games,
        metadata=dict(rating.metadata),
    )


def update_ratings(
    ratings: dict[str, Rating],
    results: list[dict[str, Any]],
    config: RatingConfig,
) -> dict[str, Rating]:
    updated = dict(ratings)
    as_of = datetime.now(UTC)
    for result in results:
        a_id = str(result["a"])
        b_id = str(result["b"])
        a = _grow_rd(updated.get(a_id, Rating(a_id, config.base_rating, config.base_rd)), config, as_of)
        b = _grow_rd(updated.get(b_id, Rating(b_id, config.base_rating, config.base_rd)), config, as_of)
        score_a = float(result.get("score_a", result.get("a_score", 0)))
        score_b = float(result.get("score_b", result.get("b_score", 0)))
        actual_a = 1.0 if score_a > score_b else 0.0 if score_a < score_b else 0.5
        expected_a = expected_binary(a, b)
        margin = abs(score_a - score_b)
        margin_scale = 1.0 + min(margin * config.margin_weight, 1.0)
        delta = config.k * margin_scale * (actual_a - expected_a)
        played_at = str(result.get("played_at") or result.get("date") or datetime.now(UTC).isoformat())
        updated[a_id] = Rating(
            a_id,
            a.rating + delta,
            max(config.min_rd, a.rd * 0.96),
            played_at,
            a.games + 1,
            dict(a.metadata),
        )
        updated[b_id] = Rating(
            b_id,
            b.rating - delta,
            max(config.min_rd, b.rd * 0.96),
            played_at,
            b.games + 1,
            dict(b.metadata),
        )
    return updated


def rating_interval_probability(
    a: Rating,
    b: Rating,
    *,
    n_samples: int = 10000,
) -> dict[str, float]:
    rng = random.Random(f"{a.entity_id}:{b.entity_id}:{n_samples}")
    p_base = expected_binary(a, b)
    probs = []
    sd_a = max(float(a.rd), 1.0) / 1.96
    sd_b = max(float(b.rd), 1.0) / 1.96
    for _ in range(max(int(n_samples), 1)):
        sampled_a = Rating(a.entity_id, rng.gauss(a.rating, sd_a), a.rd)
        sampled_b = Rating(b.entity_id, rng.gauss(b.rating, sd_b), b.rd)
        probs.append(expected_binary(sampled_a, sampled_b))
    probs.sort()
    low_idx = int(0.1 * (len(probs) - 1))
    high_idx = int(0.9 * (len(probs) - 1))
    return {
        "pLow": probs[low_idx],
        "pBase": p_base,
        "pHigh": probs[high_idx],
        "rdA": a.rd,
        "rdB": b.rd,
    }
