from __future__ import annotations

from wayfinder_paths.quant.sports_dry_run import (
    validate_sports_decision,
    validate_sports_surface,
)
from wayfinder_paths.quant.sports_model_recipes import SPORTS_MODEL_RECIPES
from wayfinder_paths.quant.sports_modifiers import validate_modifier
from wayfinder_paths.quant.sports_posterior import posterior_from_packs
from wayfinder_paths.quant.sports_ratings import Rating, rating_interval_probability


def test_sportsbook_only_buy_is_error() -> None:
    decision = {
        "packId": "decision",
        "packType": "decisionPack",
        "payload": {
            "rows": [
                {
                    "decision": "BUY_YES",
                    "venue": "sportsbook",
                    "entryPrice": 0.4,
                    "marketPrior": 0.4,
                }
            ]
        },
    }
    surface = {
        "packId": "surface",
        "payload": {"markets": [{"venue": "polymarket", "ask": 0.4}]},
    }
    analysis = {"packId": "analysis", "payload": {"rows": [{"modelP": 0.5}]}}

    report = validate_sports_decision(decision, surface, analysis)

    assert any(
        issue["code"] == "SPORTSBOOK_ONLY_MARKED_ACTIONABLE"
        for issue in report["payload"]["issues"]
    )


def test_multioutcome_binary_collapse_is_error() -> None:
    decision = {
        "packId": "decision",
        "packType": "decisionPack",
        "payload": {
            "rows": [
                {
                    "decision": "BUY_NO",
                    "marketType": "1x2",
                    "side": "no",
                    "entryPrice": 0.4,
                }
            ]
        },
    }
    surface = {
        "packId": "surface",
        "payload": {"markets": [{"venue": "hyperliquid", "ask": 0.4}]},
    }
    analysis = {"packId": "analysis", "payload": {"rows": [{"modelP": 0.5}]}}

    report = validate_sports_decision(decision, surface, analysis)

    assert any(
        issue["code"] == "BINARY_COLLAPSED_MULTI_OUTCOME"
        for issue in report["payload"]["issues"]
    )


def test_missing_executable_prior_is_error() -> None:
    decision = {
        "packId": "decision",
        "packType": "decisionPack",
        "payload": {"rows": [{"decision": "BUY_YES"}]},
    }
    surface = {"packId": "surface", "payload": {"markets": []}}
    analysis = {"packId": "analysis", "payload": {"rows": [{"modelP": 0.5}]}}

    report = validate_sports_decision(decision, surface, analysis)

    assert any(
        issue["code"] == "MISSING_EXECUTABLE_PRIOR"
        for issue in report["payload"]["issues"]
    )


def test_three_way_surface_requires_draw_outcome() -> None:
    surface = {
        "packId": "surface",
        "packType": "surfacePack",
        "payload": {
            "markets": [
                {
                    "marketType": "1x2",
                    "outcomes": [{"label": "home"}, {"label": "away"}],
                }
            ]
        },
    }

    report = validate_sports_surface(surface)

    assert any(
        issue["code"] == "BINARY_COLLAPSED_MULTI_OUTCOME"
        for issue in report["payload"]["issues"]
    )


def test_modifier_out_of_bounds_is_error() -> None:
    recipe = SPORTS_MODEL_RECIPES["worldcup_path_mc_v1"]
    modifier = {
        "target": {
            "entityType": "team",
            "entityId": "croatia",
            "metric": "team_rating_delta",
        },
        "operation": "add",
        "value": 500,
    }

    result = validate_modifier(modifier, recipe=recipe)

    assert result["status"] == "fail"
    assert any(issue["code"] == "MODIFIER_OUT_OF_BOUNDS" for issue in result["issues"])


def test_rating_model_outputs_uncertainty_band() -> None:
    result = rating_interval_probability(
        Rating("a", 1600, 80),
        Rating("b", 1500, 120),
        n_samples=200,
    )

    assert result["pLow"] < result["pBase"] < result["pHigh"]
    assert result["rdA"] == 80
    assert result["rdB"] == 120


def test_posterior_from_packs_returns_decision_pack() -> None:
    surface = {
        "packId": "surface",
        "payload": {
            "markets": [
                {
                    "id": "croatia",
                    "venue": "polymarket",
                    "bid": 0.008,
                    "ask": 0.009,
                }
            ]
        },
    }
    analysis = {
        "packId": "analysis",
        "payload": {
            "recipeId": "worldcup_path_mc_v1",
            "rows": [{"id": "croatia", "modelP": 0.014, "pLow": 0.010, "pHigh": 0.018}],
        },
    }

    decision = posterior_from_packs(surface_pack=surface, analysis_pack=analysis)

    assert decision["packType"] == "decisionPack"
    assert decision["payload"]["rows"][0]["posteriorLedger"]
