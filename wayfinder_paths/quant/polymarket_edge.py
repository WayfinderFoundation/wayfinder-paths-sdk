"""Small helpers for binary prediction-market edge calculations."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

_EPSILON = 1e-9


def _clamp_probability(value: float) -> float:
    return min(max(float(value), _EPSILON), 1.0 - _EPSILON)


def _require_positive(value: float, name: str) -> float:
    number = float(value)
    if number <= 0:
        raise ValueError(f"{name} must be positive")
    return number


def binary_yes_ev(p_yes: float, entry_yes: float) -> float:
    """Return YES expected value per share before fees."""
    return float(p_yes) - float(entry_yes)


def binary_no_ev(p_yes: float, entry_no: float) -> float:
    """Return NO expected value per share before fees."""
    return (1.0 - float(p_yes)) - float(entry_no)


def roi(ev: float, entry: float) -> float:
    """Return ROI on entry cost."""
    return float(ev) / _require_positive(entry, "entry")


def simple_annualized_roi(period_roi: float, days_to_resolution: float) -> float:
    """Annualize a realized or expected period ROI over days to resolution."""
    days = _require_positive(days_to_resolution, "days_to_resolution")
    value = float(period_roi)
    if value <= -1.0:
        return float("nan")
    return float((1.0 + value) ** (365.0 / days) - 1.0)


def binary_kelly(p: float, entry: float) -> float:
    """Return full Kelly fraction for a binary contract priced from 0 to 1."""
    probability = _clamp_probability(p)
    price = _clamp_probability(entry)
    return float((probability - price) / (1.0 - price))


def logit(p: float) -> float:
    """Convert probability to log odds."""
    probability = _clamp_probability(p)
    return math.log(probability / (1.0 - probability))


def inv_logit(value: float) -> float:
    """Convert log odds to probability."""
    if value >= 0:
        z = math.exp(-float(value))
        return float(1.0 / (1.0 + z))
    z = math.exp(float(value))
    return float(z / (1.0 + z))


def apply_log_odds_update(prior: float, deltas: Iterable[float]) -> float:
    """Apply additive evidence deltas in log-odds space."""
    return inv_logit(logit(prior) + sum(float(delta) for delta in deltas))


def normalize_binary_prices(yes_price: float, no_price: float) -> dict[str, float]:
    """Normalize executable YES/NO prices into a no-vig market prior."""
    yes = _require_positive(yes_price, "yes_price")
    no = _require_positive(no_price, "no_price")
    total = yes + no
    return {
        "marketPrior": yes / total,
        "yesPrice": yes,
        "noPrice": no,
        "totalPrice": total,
        "spreadCost": total - 1.0,
    }


def _level_price_size(
    level: Mapping[str, Any] | Sequence[float],
) -> tuple[float, float]:
    if isinstance(level, Mapping):
        price = level.get("price")
        size = level.get("size", level.get("shares"))
    else:
        price, size = level[:2]
    return _require_positive(price, "price"), _require_positive(size, "size")


def sweep_asks(
    levels: Sequence[Mapping[str, Any] | Sequence[float]],
    target_notional: float,
) -> dict[str, float | int | bool]:
    """Estimate average executable entry by sweeping ask levels by notional."""
    remaining = _require_positive(target_notional, "target_notional")
    spent = 0.0
    shares = 0.0
    levels_used = 0

    for price, size in sorted(_level_price_size(level) for level in levels):
        level_notional = price * size
        if level_notional <= 0:
            continue
        spend = min(remaining, level_notional)
        spent += spend
        shares += spend / price
        remaining -= spend
        levels_used += 1
        if remaining <= _EPSILON:
            break

    filled = shares > 0 and remaining <= _EPSILON
    return {
        "avgPrice": spent / shares if shares else float("nan"),
        "shares": shares,
        "notional": spent,
        "targetNotional": float(target_notional),
        "fillRatio": spent / float(target_notional),
        "levelsUsed": levels_used,
        "filled": filled,
    }
