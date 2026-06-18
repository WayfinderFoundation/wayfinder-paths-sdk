from __future__ import annotations

import pytest

from wayfinder_paths.quant.hyperliquid_prediction_surface import (
    hl_exit_ev,
    normalize_hl_market,
)
from wayfinder_paths.quant.prediction_market_payoffs import (
    exit_ev,
    expand_resolution_profile,
    expected_payout,
    settlement_ev,
)
from wayfinder_paths.quant.prediction_market_surface import (
    classify_resolution_profile,
    compact_surface_lite,
)
from wayfinder_paths.quant.prediction_market_validation import (
    validate_decision_uses_correct_math,
    validate_exit_plan,
)
from wayfinder_paths.quant.workpack_dry_run import (
    validate_decision_pack,
    validate_surface_pack,
)


def _full_pm_pack() -> dict:
    return {
        "packId": "pm_surface_ipo_20260617T1530Z",
        "packType": "surfacePack",
        "domain": "prediction_markets",
        "stage": "surface",
        "schemaVersion": "1.0",
        "observedAt": "2026-06-17T15:30:00Z",
        "validUntil": "2026-06-17T15:31:00Z",
        "scope": {},
        "summary": "IPO board",
        "path": ".wayfinder_runs/packs/prediction_markets/surface/pm_surface_ipo_20260617T1530Z.json",
        "payload": {
            "venue": "polymarket",
            "eventSlug": "will-anthropic-or-openai-ipo-first",
            "resolution": {"profile": "partial_50_50"},
            "markets": [
                {
                    "question": "Anthropic YES",
                    "bestBid": 0.70,
                    "bestAsk": 0.73,
                    "conditionId": "0xa",
                },
                {
                    "question": "OpenAI YES",
                    "bestBid": 0.25,
                    "bestAsk": 0.28,
                    "conditionId": "0xb",
                },
            ],
        },
        "reusePolicy": {"mustRehydrateBefore": ["recommend_buy"], "ttlSeconds": 60},
        "lineage": {},
    }


def test_compact_surface_lite_keeps_refs_not_raw_payload() -> None:
    lite = compact_surface_lite(_full_pm_pack())

    assert lite["profile"] == "pm_partial_50_50"
    assert lite["resolutionRef"] == "pm_surface_ipo_20260617T1530Z#resolution"
    assert lite["orderbookRef"] == "pm_surface_ipo_20260617T1530Z#books"
    assert lite["fullRef"].endswith("pm_surface_ipo_20260617T1530Z.json")
    assert lite["rows"] == [
        ["Anthropic YES", 0.70, 0.73, 0.715, "ok"],
        ["OpenAI YES", 0.25, 0.28, 0.265, "ok"],
    ]
    assert "payload" not in lite


def test_partial_50_50_profile_expands_only_when_needed() -> None:
    model = expand_resolution_profile("pm_partial_50_50")

    assert model["states"] == ["a_wins", "b_wins", "split"]
    assert model["payoffs"]["a_token"] == [1.0, 0.0, 0.5]
    assert expected_payout(
        model["payoffs"]["a_token"],
        {"a_wins": 0.62, "b_wins": 0.28, "split": 0.10},
    ) == pytest.approx(0.67)
    assert settlement_ev(0.67, 0.73) == pytest.approx(-0.06)


def test_exclusive_multi_requires_all_related_outcomes() -> None:
    full = {
        "payload": {
            "markets": [
                {
                    "outcomes": [
                        {"label": "Brazil"},
                        {"label": "France"},
                        {"label": "Spain"},
                    ]
                }
            ]
        }
    }

    model = expand_resolution_profile("pm_exclusive_multi", full)

    assert model["states"] == ["Brazil", "France", "Spain"]
    assert model["payoffs"]["France"] == [0.0, 1.0, 0.0]


def test_binary_math_on_non_binary_profile_fails_validation() -> None:
    surface = {"profile": "pm_partial_50_50", "rows": [["Anthropic YES", 0.70, 0.73, 0.715, "ok"]]}
    decision = {
        "payload": {
            "rows": [
                {
                    "decision": "BUY_YES",
                    "profile": "pm_partial_50_50",
                    "mathHelper": "binary_yes_ev",
                    "edgeMode": "settlement_edge",
                    "settlementEv": 0.04,
                }
            ]
        }
    }

    report = validate_decision_uses_correct_math(surface, decision)

    assert any(issue["code"] == "PM_BINARY_MATH_USED_FOR_NON_BINARY" for issue in report["payload"]["issues"])


def test_hyperliquid_mid_only_cannot_be_actionable_buy() -> None:
    normalized = normalize_hl_market({"coin": "#1234", "mid": 0.38})
    decision = {
        "payload": {
            "rows": [
                {
                    "decision": "BUY",
                    "profile": normalized["profile"],
                    "edgeMode": "mark_to_market_edge",
                    "expectedExitBid": 0.45,
                }
            ]
        }
    }

    report = validate_decision_uses_correct_math(normalized, decision)

    assert normalized["profile"] == "hl_mid_only"
    assert any(issue["code"] == "PM_HL_MID_ONLY_ACTIONABLE_BUY" for issue in report["payload"]["issues"])


def test_prediction_market_decision_requires_exit_or_settlement_plan() -> None:
    decision = {
        "packId": "decision",
        "packType": "decisionPack",
        "domain": "prediction_markets",
        "stage": "decision",
        "schemaVersion": "1.0",
        "observedAt": "2026-06-17T15:30:00Z",
        "validUntil": "2026-06-17T15:31:00Z",
        "scope": {},
        "summary": "decision",
        "payload": {
            "surfaceLite": {"profile": "pm_simple_binary", "rows": [["YES", 0.48, 0.50, 0.49, "ok"]]},
            "rows": [{"decision": "BUY_YES", "entry": 0.50}],
        },
        "inputPacks": ["surface"],
        "reusePolicy": {"mustRehydrateBefore": ["recommend_buy"], "ttlSeconds": 60},
        "lineage": {},
    }

    report = validate_decision_pack(decision)

    assert any(issue["code"] == "PM_BUY_WITHOUT_EXIT_OR_SETTLEMENT_PLAN" for issue in report["payload"]["issues"])


def test_mark_to_market_edge_needs_future_bid_assumption() -> None:
    surface = {"profile": "pm_simple_binary", "rows": [["YES", 0.48, 0.50, 0.49, "ok"]]}
    decision = {"payload": {"rows": [{"decision": "BUY_YES", "edgeMode": "mark_to_market_edge"}]}}

    report = validate_exit_plan(surface, decision)

    assert any(issue["code"] == "PM_EXIT_EDGE_WITHOUT_FUTURE_BID_ASSUMPTION" for issue in report["payload"]["issues"])


def test_exit_and_hl_ev_helpers() -> None:
    assert exit_ev(expected_exit_bid=0.62, entry=0.55, slippage=0.01) == pytest.approx(0.06)
    short = hl_exit_ev(
        side="short",
        entry=100,
        expected_exit=92,
        funding_cost=1,
        fees=0.5,
        slippage=0.5,
    )
    assert short["ev"] == pytest.approx(6.0)


def test_prediction_surface_pack_validation_runs_domain_checks() -> None:
    pack = _full_pm_pack()
    pack["payload"]["surfaceLite"] = {"rows": [], "profile": "pm_partial_50_50"}

    report = validate_surface_pack(pack)

    assert any(issue["code"] == "PM_SURFACE_MISSING_EXECUTABLE_PRICE" for issue in report["payload"]["issues"])


def test_profile_classifier_detects_augmented_other() -> None:
    full = {
        "payload": {
            "venue": "polymarket",
            "markets": [
                {
                    "negRisk": True,
                    "outcomes": [{"label": "Alice"}, {"label": "Other"}],
                }
            ],
        }
    }

    result = classify_resolution_profile(full)

    assert result["profile"] == "pm_aug_neg_risk"
    assert result["warnings"] == ["augmented_other_requires_rules"]
