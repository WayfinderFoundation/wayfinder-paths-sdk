"""Bounded sports model modifiers.

LLMs may propose modifiers from context. This module validates and applies them
to model inputs only; final probabilities are produced downstream by model code.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

DEFAULT_MODIFIER_BOUNDS = {
    "team_rating_delta": {"soft": [-35, 35], "hard": [-75, 75]},
    "player_minutes_multiplier": {"soft": [0.75, 1.15], "hard": [0.50, 1.30]},
    "usage_multiplier": {"soft": [0.85, 1.15], "hard": [0.65, 1.40]},
    "pace_multiplier": {"soft": [0.94, 1.06], "hard": [0.88, 1.12]},
    "variance_multiplier": {"soft": [0.85, 1.25], "hard": [0.65, 1.60]},
    "goal_expectation_delta": {"soft": [-0.25, 0.25], "hard": [-0.60, 0.60]},
}
VALID_OPERATIONS = {"add", "multiply", "set", "remove"}


def _modifier_issue(code: str, message: str, *, severity: str = "error") -> dict[str, Any]:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "fixStage": "context",
        "autoFixable": False,
    }


def _metric(modifier: Mapping[str, Any]) -> str:
    target = modifier.get("target") or {}
    return str(target.get("metric") or modifier.get("metric") or "")


def validate_modifier(
    modifier: dict[str, Any],
    *,
    recipe: dict[str, Any],
    feature_pack: dict[str, Any] | None = None,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    metric = _metric(modifier)
    operation = str(modifier.get("operation") or "")
    if metric not in set(recipe.get("modifierSlots") or []):
        issues.append(_modifier_issue("MODIFIER_TARGET_NOT_FOUND", f"Modifier metric {metric!r} is not allowed by recipe."))
    if operation not in VALID_OPERATIONS:
        issues.append(_modifier_issue("INVALID_MODIFIER_OPERATION", f"Invalid modifier operation {operation!r}."))

    value = modifier.get("value")
    bounds = modifier.get("bounds") or DEFAULT_MODIFIER_BOUNDS.get(metric, {}).get("hard")
    if isinstance(bounds, Mapping):
        min_value = bounds.get("min")
        max_value = bounds.get("max")
    elif isinstance(bounds, list | tuple) and len(bounds) == 2:
        min_value, max_value = bounds
    else:
        hard = DEFAULT_MODIFIER_BOUNDS.get(metric, {}).get("hard")
        min_value, max_value = hard if hard else (None, None)
    if isinstance(value, int | float) and min_value is not None and max_value is not None:
        if float(value) < float(min_value) or float(value) > float(max_value):
            issues.append(_modifier_issue("MODIFIER_OUT_OF_BOUNDS", f"{metric}={value} outside [{min_value}, {max_value}]."))

    target = modifier.get("target") or {}
    entity_id = target.get("entityId")
    if feature_pack and entity_id:
        payload = feature_pack.get("payload") or {}
        entities = payload.get("entities") or payload.get("participants") or payload.get("players") or []
        ids = {str(row.get("id") or row.get("entityId") or row.get("playerId")) for row in entities if isinstance(row, Mapping)}
        if ids and str(entity_id) not in ids:
            issues.append(_modifier_issue("MODIFIER_TARGET_NOT_FOUND", f"Modifier target {entity_id!r} not found in featurePack."))

    status = "validated" if not issues else "rejected"
    normalized = dict(modifier)
    normalized["status"] = status
    return {"status": "pass" if not issues else "fail", "issues": issues, "modifier": normalized}


def apply_modifiers(
    model_inputs: dict[str, Any],
    modifiers: list[dict[str, Any]],
) -> dict[str, Any]:
    adjusted = deepcopy(model_inputs)
    applied: list[dict[str, Any]] = []
    for modifier in modifiers:
        if modifier.get("status") not in {None, "validated", "applied"}:
            continue
        metric = _metric(modifier)
        if not metric:
            continue
        operation = str(modifier.get("operation") or "")
        value = modifier.get("value")
        current = adjusted.get(metric)
        if operation == "add":
            adjusted[metric] = (current or 0) + value
        elif operation == "multiply":
            adjusted[metric] = (1 if current is None else current) * value
        elif operation == "set":
            adjusted[metric] = value
        elif operation == "remove":
            adjusted.pop(metric, None)
        else:
            continue
        applied.append({**modifier, "status": "applied"})
    adjusted.setdefault("modifiersApplied", []).extend(applied)
    return adjusted


def modifier_impact(
    *,
    base_rows: list[dict[str, Any]],
    adjusted_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    adjusted_by_id = {str(row.get("id") or row.get("marketId") or row.get("entityId")): row for row in adjusted_rows}
    impacts: list[dict[str, Any]] = []
    for base in base_rows:
        key = str(base.get("id") or base.get("marketId") or base.get("entityId"))
        adjusted = adjusted_by_id.get(key)
        if not adjusted:
            continue
        base_p = base.get("modelP") or base.get("pBase")
        adjusted_p = adjusted.get("modelP") or adjusted.get("pBase")
        impacts.append(
            {
                "id": key,
                "baseP": base_p,
                "adjustedP": adjusted_p,
                "deltaPp": None if base_p is None or adjusted_p is None else (float(adjusted_p) - float(base_p)) * 100,
                "modifiersApplied": adjusted.get("modifiersApplied", []),
            }
        )
    return impacts


def modifier_to_evidence_card(
    modifier: dict[str, Any],
    *,
    market_p: float,
) -> dict[str, Any]:
    direction = "for_yes" if float(modifier.get("value", 0) or 0) > 0 else "against_yes"
    return {
        "claim": modifier.get("rationale") or f"Model modifier {modifier.get('modifierId')}",
        "direction": direction,
        "strength": "weak" if modifier.get("confidence") == "low" else "medium",
        "sourceQuality": "reputable_secondary",
        "freshness": "fresh",
        "independence": "partially_overlapping",
        "alreadyPriced": "maybe",
        "resolutionRelevance": "direct",
        "marketPrior": market_p,
        "kind": "model_modifier",
    }
