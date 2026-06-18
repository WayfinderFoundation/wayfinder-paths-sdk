"""Registry of reusable sports model recipes."""

from __future__ import annotations

from typing import Any


def _recipe(
    *,
    sport: str,
    market_types: list[str],
    base_models: list[dict[str, str]],
    outputs: list[str],
) -> dict[str, Any]:
    return {
        "domain": "sports",
        "sport": sport,
        "marketTypes": market_types,
        "requiredPacks": ["surfacePack", "contextPack", "featurePack"],
        "baseModels": base_models,
        "modifierSlots": [
            "team_rating_delta",
            "player_minutes_multiplier",
            "usage_multiplier",
            "pace_multiplier",
            "variance_multiplier",
            "starter_override",
            "goal_expectation_delta",
        ],
        "outputs": outputs,
        "validation": [
            "no_future_data",
            "sample_size",
            "market_mapping",
            "modifier_bounds",
        ],
    }


SPORTS_MODEL_RECIPES: dict[str, dict[str, Any]] = {
    "nba_game_elo_mc_v1": _recipe(
        sport="nba",
        market_types=["moneyline", "spread", "total"],
        base_models=[
            {"id": "rating_model", "type": "glicko_like_elo"},
            {"id": "score_distribution", "type": "monte_carlo_normal"},
        ],
        outputs=["modelP", "pLow", "pHigh", "fairSpread", "fairTotal", "scenarioSensitivity"],
    ),
    "nhl_game_elo_poisson_v1": _recipe(
        sport="nhl",
        market_types=["moneyline", "puckline", "total"],
        base_models=[
            {"id": "rating_model", "type": "glicko_like_elo"},
            {"id": "goal_distribution", "type": "poisson"},
        ],
        outputs=["modelP", "pLow", "pHigh", "fairSpread", "fairTotal"],
    ),
    "mlb_game_pitcher_adjusted_v1": _recipe(
        sport="mlb",
        market_types=["moneyline", "runline", "total"],
        base_models=[
            {"id": "rating_model", "type": "glicko_like_elo"},
            {"id": "pitcher_adjustment", "type": "starter_context"},
        ],
        outputs=["modelP", "pLow", "pHigh", "fairSpread", "fairTotal"],
    ),
    "soccer_1x2_elo_poisson_v1": _recipe(
        sport="soccer",
        market_types=["1x2", "spread", "total"],
        base_models=[
            {"id": "rating_model", "type": "glicko_like_elo"},
            {"id": "goal_distribution", "type": "poisson"},
        ],
        outputs=["homeP", "drawP", "awayP", "pLow", "pHigh", "fairSpread", "fairTotal"],
    ),
    "worldcup_path_mc_v1": _recipe(
        sport="worldcup",
        market_types=["outright", "group_winner", "reach_stage", "match_winner"],
        base_models=[
            {"id": "rating_model", "type": "glicko_like_elo"},
            {"id": "path_model", "type": "monte_carlo_event_sim"},
        ],
        outputs=["modelP", "pLow", "pHigh", "pathAssumption", "scenarioSensitivity"],
    ),
    "nba_prop_projection_v1": _recipe(
        sport="nba",
        market_types=["player_prop"],
        base_models=[{"id": "prop_projection", "type": "minutes_usage_distribution"}],
        outputs=["modelP", "pLow", "pHigh", "projection", "line", "scenarioSensitivity"],
    ),
    "nhl_prop_projection_v1": _recipe(
        sport="nhl",
        market_types=["player_prop"],
        base_models=[{"id": "prop_projection", "type": "usage_goal_distribution"}],
        outputs=["modelP", "pLow", "pHigh", "projection", "line"],
    ),
    "mlb_prop_projection_v1": _recipe(
        sport="mlb",
        market_types=["player_prop"],
        base_models=[{"id": "prop_projection", "type": "batter_pitcher_distribution"}],
        outputs=["modelP", "pLow", "pHigh", "projection", "line"],
    ),
}
