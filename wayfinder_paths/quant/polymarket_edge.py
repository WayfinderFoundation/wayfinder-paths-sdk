"""Small helpers for binary prediction-market edge calculations."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

_EPSILON = 1e-9
_STRENGTH_LLR = {
    "weak": 0.10,
    "medium": 0.25,
    "strong": 0.50,
    "decisive": 0.90,
}
_SOURCE_QUALITY_MULTIPLIER = {
    "primary": 1.00,
    "market_data": 0.90,
    "reputable_secondary": 0.80,
    "social": 0.35,
    "unknown": 0.25,
}
_FRESHNESS_MULTIPLIER = {
    "fresh": 1.00,
    "recent": 0.80,
    "stale": 0.35,
}
_INDEPENDENCE_MULTIPLIER = {
    "independent": 1.00,
    "partially_overlapping": 0.60,
    "duplicate": 0.15,
}
_PRICED_MULTIPLIER = {
    "unlikely": 1.00,
    "maybe": 0.60,
    "likely": 0.25,
}
_RESOLUTION_RELEVANCE_MULTIPLIER = {
    "direct": 1.00,
    "indirect": 0.60,
    "background": 0.25,
}


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


def implied_prior_from_quote(
    yes_bid: float | None,
    yes_ask: float | None,
    last: float | None = None,
) -> dict[str, float | str | bool | None]:
    """Infer market prior from current quote, with last trade as low-quality context."""
    if yes_bid is not None and yes_ask is not None:
        bid = _clamp_probability(yes_bid)
        ask = _clamp_probability(yes_ask)
        if ask < bid:
            raise ValueError("yes_ask must be greater than or equal to yes_bid")
        spread = ask - bid
        quality = "high" if spread <= 0.03 else "medium" if spread <= 0.08 else "low"
        prior = (bid + ask) / 2.0
        return {
            "p": prior,
            "marketPrior": prior,
            "priorSource": "bid_ask_mid",
            "method": "bid_ask_mid",
            "spread": spread,
            "quality": quality,
            "isExecutable": True,
        }

    if yes_ask is not None:
        prior = _clamp_probability(yes_ask)
        return {
            "p": prior,
            "marketPrior": prior,
            "priorSource": "ask_only",
            "method": "ask_only",
            "spread": None,
            "quality": "low",
            "isExecutable": True,
        }

    if yes_bid is not None:
        prior = _clamp_probability(yes_bid)
        return {
            "p": prior,
            "marketPrior": prior,
            "priorSource": "bid_only",
            "method": "bid_only",
            "spread": None,
            "quality": "low",
            "isExecutable": False,
        }

    if last is not None:
        prior = _clamp_probability(last)
        return {
            "p": prior,
            "marketPrior": prior,
            "priorSource": "last_trade_fallback",
            "method": "last_trade_fallback",
            "spread": None,
            "quality": "very_low",
            "isExecutable": False,
        }

    raise ValueError("No quote data available")


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


def sigmoid(value: float) -> float:
    """Alias for inverse logit."""
    return inv_logit(value)


def apply_log_odds_update(prior: float, deltas: Iterable[float]) -> float:
    """Apply additive evidence deltas in log-odds space."""
    return inv_logit(logit(prior) + sum(float(delta) for delta in deltas))


def _multiplier(mapping: Mapping[str, float], value: Any, default: str) -> float:
    return mapping.get(str(value or default), mapping[default])


def evidence_llr(card: Mapping[str, Any]) -> float:
    """Convert one evidence card into a signed log-likelihood-ratio proxy."""
    base = (
        abs(float(card["llr"]))
        if card.get("llr") is not None
        else _STRENGTH_LLR.get(
            str(card.get("strength", "weak")),
            _STRENGTH_LLR["weak"],
        )
    )
    multiplier = (
        _multiplier(_SOURCE_QUALITY_MULTIPLIER, card.get("sourceQuality"), "unknown")
        * _multiplier(_FRESHNESS_MULTIPLIER, card.get("freshness"), "recent")
        * _multiplier(
            _INDEPENDENCE_MULTIPLIER,
            card.get("independence"),
            "partially_overlapping",
        )
        * _multiplier(_PRICED_MULTIPLIER, card.get("alreadyPriced"), "maybe")
        * _multiplier(
            _RESOLUTION_RELEVANCE_MULTIPLIER,
            card.get("resolutionRelevance"),
            "background",
        )
    )
    llr = base * multiplier
    direction = card.get("direction")
    if direction == "for_yes":
        return llr
    if direction == "against_yes":
        return -llr
    return 0.0


def bayes_update_from_evidence(
    prior_p: float,
    evidence: Sequence[Mapping[str, Any]],
    *,
    max_abs_log_odds_move: float = 0.75,
) -> dict[str, Any]:
    """Apply capped evidence-card updates in log-odds space."""
    prior = _clamp_probability(prior_p)
    card_llrs = [evidence_llr(card) for card in evidence]
    raw_move = sum(card_llrs)
    cap = abs(float(max_abs_log_odds_move))
    capped_move = min(max(raw_move, -cap), cap)
    posterior = inv_logit(logit(prior) + capped_move)
    return {
        "prior": prior,
        "rawLogOddsMove": raw_move,
        "cappedLogOddsMove": capped_move,
        "pBase": posterior,
        "evidenceCards": [
            {**dict(card), "computedLlr": llr}
            for card, llr in zip(evidence, card_llrs, strict=True)
        ],
    }


def update_prior(
    prior_p: float,
    evidence_cards: Sequence[Mapping[str, Any]],
    *,
    max_abs_log_odds_move: float = 0.75,
) -> dict[str, Any]:
    """Alias for bayes_update_from_evidence."""
    return bayes_update_from_evidence(
        prior_p,
        evidence_cards,
        max_abs_log_odds_move=max_abs_log_odds_move,
    )


def posterior_band_from_evidence(
    prior_p: float,
    evidence: Sequence[Mapping[str, Any]],
    *,
    max_abs_log_odds_move: float = 0.75,
    fallback_uncertainty: float = 0.05,
) -> dict[str, float]:
    """Estimate posterior band by removing strongest pro/anti evidence."""
    base_update = bayes_update_from_evidence(
        prior_p,
        evidence,
        max_abs_log_odds_move=max_abs_log_odds_move,
    )
    p_base = float(base_update["pBase"])
    if not evidence:
        uncertainty = abs(float(fallback_uncertainty))
        return {
            "pLow": max(0.0, p_base - uncertainty),
            "pBase": p_base,
            "pHigh": min(1.0, p_base + uncertainty),
        }

    evidence_with_llr = list(
        zip(evidence, (evidence_llr(card) for card in evidence), strict=True)
    )
    strongest_pro = max(
        (item for item in evidence_with_llr if item[1] > 0),
        default=None,
        key=lambda item: item[1],
    )
    strongest_anti = min(
        (item for item in evidence_with_llr if item[1] < 0),
        default=None,
        key=lambda item: item[1],
    )

    low_evidence = list(evidence)
    high_evidence = list(evidence)
    if strongest_pro is not None:
        low_evidence.remove(strongest_pro[0])
    if strongest_anti is not None:
        high_evidence.remove(strongest_anti[0])

    p_low = float(
        bayes_update_from_evidence(
            prior_p,
            low_evidence,
            max_abs_log_odds_move=max_abs_log_odds_move,
        )["pBase"]
    )
    p_high = float(
        bayes_update_from_evidence(
            prior_p,
            high_evidence,
            max_abs_log_odds_move=max_abs_log_odds_move,
        )["pBase"]
    )
    return {
        "pLow": min(p_low, p_base, p_high),
        "pBase": p_base,
        "pHigh": max(p_low, p_base, p_high),
    }


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


def conservative_trade_gate(
    side: str,
    p_low: float,
    p_base: float,
    p_high: float,
    entry: float,
    min_ev: float = 0.02,
) -> dict[str, float | str | bool]:
    """Gate a binary trade using conservative posterior, not base case only."""
    normalized_side = side.upper().removeprefix("BUY_")
    if normalized_side == "YES":
        conservative_ev = binary_yes_ev(p_low, entry)
        base_ev = binary_yes_ev(p_base, entry)
    elif normalized_side == "NO":
        conservative_ev = binary_no_ev(p_high, entry)
        base_ev = binary_no_ev(p_base, entry)
    else:
        raise ValueError("side must be YES, NO, BUY_YES, or BUY_NO")

    return {
        "side": normalized_side,
        "entry": float(entry),
        "baseEv": base_ev,
        "conservativeEv": conservative_ev,
        "minEv": float(min_ev),
        "passes": conservative_ev + _EPSILON >= float(min_ev),
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
