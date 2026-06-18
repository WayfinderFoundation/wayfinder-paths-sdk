"""WorkPack-oriented sports simulation wrappers."""

from __future__ import annotations

from typing import Any

from wayfinder_paths.quant.event_sim import load_config, rows_as_dicts, run_simulation
from wayfinder_paths.quant.sports_model_recipes import SPORTS_MODEL_RECIPES
from wayfinder_paths.quant.sports_modifiers import apply_modifiers, validate_modifier


def _analysis_pack(
    *,
    sport: str,
    recipe_id: str,
    rows: list[dict[str, Any]],
    consumed: list[str],
    summary: str,
) -> dict[str, Any]:
    return {
        "packType": "analysisPack",
        "domain": "sports",
        "intent": "sports_model",
        "stage": "analysis",
        "schemaVersion": "1.0",
        "inputPacks": consumed,
        "summary": summary,
        "payload": {
            "sport": sport,
            "recipeId": recipe_id,
            "rows": rows,
        },
        "reusePolicy": {
            "canReuseFor": ["analysis", "final_answer"],
            "mustRehydrateBefore": [],
            "ttlSeconds": 3600,
        },
        "sensitivity": "public",
    }


def _validated_modifiers(recipe_id: str, feature_pack: dict[str, Any], modifiers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    recipe = SPORTS_MODEL_RECIPES[recipe_id]
    validated = []
    for modifier in modifiers:
        result = validate_modifier(modifier, recipe=recipe, feature_pack=feature_pack)
        if result["status"] == "pass":
            validated.append(result["modifier"])
    return validated


def run_game_simulation(
    *,
    sport: str,
    feature_pack: dict[str, Any],
    context_pack: dict[str, Any],
    recipe_id: str,
    modifiers: list[dict[str, Any]],
) -> dict[str, Any]:
    validated = _validated_modifiers(recipe_id, feature_pack, modifiers)
    inputs = apply_modifiers(dict(feature_pack.get("payload") or {}), validated)
    rows = []
    for row in inputs.get("games") or inputs.get("rows") or []:
        model_p = row.get("modelP") or row.get("pBase") or row.get("probability")
        rows.append(
            {
                **row,
                "modelP": model_p,
                "pLow": row.get("pLow"),
                "pHigh": row.get("pHigh"),
                "modifiersApplied": inputs.get("modifiersApplied", []),
            }
        )
    return _analysis_pack(
        sport=sport,
        recipe_id=recipe_id,
        rows=rows,
        consumed=[str(feature_pack.get("packId", "")), str(context_pack.get("packId", ""))],
        summary=f"{sport} game simulation rows from {recipe_id}",
    )


def run_prop_projection(
    *,
    sport: str,
    feature_pack: dict[str, Any],
    context_pack: dict[str, Any],
    recipe_id: str,
    modifiers: list[dict[str, Any]],
) -> dict[str, Any]:
    validated = _validated_modifiers(recipe_id, feature_pack, modifiers)
    inputs = apply_modifiers(dict(feature_pack.get("payload") or {}), validated)
    rows = []
    for row in inputs.get("props") or inputs.get("rows") or []:
        rows.append({**row, "modifiersApplied": inputs.get("modifiersApplied", [])})
    return _analysis_pack(
        sport=sport,
        recipe_id=recipe_id,
        rows=rows,
        consumed=[str(feature_pack.get("packId", "")), str(context_pack.get("packId", ""))],
        summary=f"{sport} prop projection rows from {recipe_id}",
    )


def run_path_simulation(
    *,
    event_state_pack: dict[str, Any],
    feature_pack: dict[str, Any],
    recipe_id: str,
    modifiers: list[dict[str, Any]],
) -> dict[str, Any]:
    validated = _validated_modifiers(recipe_id, feature_pack, modifiers)
    payload = dict(event_state_pack.get("payload") or event_state_pack)
    payload = apply_modifiers(payload, validated)
    config = load_config(payload)
    rows = rows_as_dicts(run_simulation(config))
    return _analysis_pack(
        sport=str(payload.get("sport") or "sports"),
        recipe_id=recipe_id,
        rows=rows,
        consumed=[str(event_state_pack.get("packId", "")), str(feature_pack.get("packId", ""))],
        summary=f"path simulation rows from {recipe_id}",
    )
