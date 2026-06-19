"""Payoff and EV helpers for non-binary prediction-market profiles."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

RESOLUTION_PROFILES: dict[str, Any] = {
    "simple_binary": {
        "states": ["yes", "no"],
        "payoffs": {"yes_token": [1.0, 0.0], "no_token": [0.0, 1.0]},
    },
    "partial_50_50": {
        "states": ["a_wins", "b_wins", "split"],
        "payoffs": {"a_token": [1.0, 0.0, 0.5], "b_token": [0.0, 1.0, 0.5]},
    },
    "exclusive_multi": "expand_from_event_outcomes",
    "neg_risk": "expand_from_event_outcomes_with_conversion",
    "aug_neg_risk": "expand_named_only_require_other_warning",
    "custom_resolution": "requires_custom_resolver",
    "derivative_perp": "not_a_payout_token",
}

PROFILE_ALIASES = {
    "pm_simple_binary": "simple_binary",
    "pm_partial_50_50": "partial_50_50",
    "pm_exclusive_multi": "exclusive_multi",
    "pm_neg_risk": "neg_risk",
    "pm_aug_neg_risk": "aug_neg_risk",
    "pm_custom_resolution": "custom_resolution",
    "hl_event_perp": "derivative_perp",
    "hl_l2_derivative": "derivative_perp",
    "hl_oracle_settled": "derivative_perp",
}


def _canonical_profile(profile: str) -> str:
    return PROFILE_ALIASES.get(str(profile), str(profile))


def _outcome_labels(full_pack: Mapping[str, Any]) -> list[str]:
    payload = full_pack.get("payload") or {}
    labels: list[str] = []
    for market in payload.get("markets") or []:
        if not isinstance(market, Mapping):
            continue
        outcomes = market.get("outcomes") or []
        for outcome in outcomes:
            if isinstance(outcome, Mapping):
                label = str(outcome.get("label") or outcome.get("name") or "").strip()
            else:
                label = str(outcome).strip()
            if label and label not in labels:
                labels.append(label)
    if not labels and isinstance(payload.get("rows"), list):
        for row in payload["rows"]:
            if isinstance(row, Sequence) and not isinstance(row, (str, bytes)) and row:
                labels.append(str(row[0]))
    return labels


def _exclusive_model(
    labels: list[str], *, aug_neg_risk: bool = False
) -> dict[str, Any]:
    warnings: list[str] = []
    states = []
    for label in labels:
        lower = label.lower()
        if aug_neg_risk and lower in {"other", "placeholder"}:
            warnings.append("other_or_placeholder_requires_resolution_rules")
            continue
        states.append(label)
    payoffs = {
        label: [1.0 if label == state else 0.0 for state in states] for label in states
    }
    return {"states": states, "payoffs": payoffs, "warnings": warnings}


def expand_resolution_profile(
    profile: str, full_pack: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Expand a compact resolver profile into states/payoffs only when needed."""

    canonical = _canonical_profile(profile)
    built_in = RESOLUTION_PROFILES.get(canonical)
    if isinstance(built_in, Mapping):
        return {"profile": canonical, **dict(built_in), "warnings": []}
    if canonical == "exclusive_multi":
        labels = _outcome_labels(full_pack or {})
        return {"profile": canonical, **_exclusive_model(labels)}
    if canonical == "neg_risk":
        labels = _outcome_labels(full_pack or {})
        model = _exclusive_model(labels)
        model["conversion"] = "neg_risk_no_to_other_yes_outcomes"
        return {"profile": canonical, **model}
    if canonical == "aug_neg_risk":
        labels = _outcome_labels(full_pack or {})
        model = _exclusive_model(labels, aug_neg_risk=True)
        model["conversion"] = "aug_neg_risk_named_outcomes_only"
        return {"profile": canonical, **model}
    if canonical == "custom_resolution":
        resolution = ((full_pack or {}).get("payload") or {}).get("resolution") or {}
        parsed = resolution.get("parsed")
        if (
            isinstance(parsed, Mapping)
            and parsed.get("states")
            and parsed.get("payoffs")
        ):
            return {"profile": canonical, **dict(parsed), "warnings": []}
        raise ValueError("custom_resolution requires parsed resolver states/payoffs")
    if canonical == "derivative_perp":
        return {
            "profile": canonical,
            "states": [],
            "payoffs": {},
            "warnings": ["derivative_perp_uses_exit_ev_not_redemption_payoff"],
        }
    raise ValueError(f"unsupported resolution profile {profile!r}")


def expected_payout(
    payoffs: Sequence[float] | Mapping[str, float],
    state_probs: Mapping[str, float],
) -> float:
    """Return expected redemption payout from state payoffs and probabilities."""

    if isinstance(payoffs, Mapping):
        return sum(
            float(payoffs[state]) * float(prob) for state, prob in state_probs.items()
        )
    states = list(state_probs)
    if len(payoffs) != len(states):
        raise ValueError("payoff vector length must match state probabilities")
    return sum(
        float(payoff) * float(state_probs[state])
        for payoff, state in zip(payoffs, states, strict=True)
    )


def settlement_ev(
    expected_redemption_payout: float, entry: float, fees: float = 0.0
) -> float:
    """Expected hold-to-resolution EV per share."""

    return float(expected_redemption_payout) - float(entry) - float(fees)


def exit_ev(
    expected_exit_bid: float,
    entry: float,
    fees: float = 0.0,
    slippage: float = 0.0,
    liquidity_haircut: float = 0.0,
) -> float:
    """Expected sell-before-close EV per share."""

    return (
        float(expected_exit_bid)
        - float(entry)
        - float(fees)
        - float(slippage)
        - float(liquidity_haircut)
    )


def robust_gate(
    *,
    entry: float,
    settlement_low: float | None = None,
    settlement_base: float | None = None,
    settlement_high: float | None = None,
    exit_low: float | None = None,
    exit_base: float | None = None,
    exit_high: float | None = None,
    edge_mode: str,
    min_ev: float = 0.02,
) -> dict[str, Any]:
    """Gate an edge using the conservative EV for the requested edge mode."""

    mode = str(edge_mode)
    if mode == "settlement_edge":
        conservative = settlement_low
        base = settlement_base
        high = settlement_high
    elif mode == "mark_to_market_edge":
        conservative = exit_low
        base = exit_base
        high = exit_high
    elif mode in {"relative_value_edge", "arb_or_conversion_edge"}:
        conservative = settlement_low if settlement_low is not None else exit_low
        base = settlement_base if settlement_base is not None else exit_base
        high = settlement_high if settlement_high is not None else exit_high
    else:
        raise ValueError("unsupported edge_mode")
    passes = conservative is not None and float(conservative) - float(entry) >= float(
        min_ev
    )
    return {
        "edgeMode": mode,
        "entry": float(entry),
        "conservativeValue": conservative,
        "baseValue": base,
        "highValue": high,
        "conservativeEv": None
        if conservative is None
        else float(conservative) - float(entry),
        "baseEv": None if base is None else float(base) - float(entry),
        "passes": bool(passes),
        "decision": "PASS" if passes else "WATCH",
    }
