from __future__ import annotations

from wayfinder_paths.quant.workpack_dry_run import (
    validate_decision_pack,
    validate_rehydrate_policy,
    validation_report,
)


def test_validate_rehydrate_policy_blocks_execution_from_stale_pack() -> None:
    pack = {
        "packId": "pack_1",
        "packType": "surfacePack",
        "domain": "sports",
        "stage": "surface",
        "schemaVersion": "1.0",
        "observedAt": "2026-06-17T15:30:00Z",
        "validUntil": "2026-06-17T15:31:00Z",
        "scope": {},
        "summary": "expired",
        "payload": {},
        "reusePolicy": {"mustRehydrateBefore": ["execute"], "ttlSeconds": 60},
        "lineage": {},
        "stale": True,
    }

    report = validate_rehydrate_policy(pack, action="execute")

    assert report["payload"]["status"] == "fail"
    codes = {issue["code"] for issue in report["payload"]["issues"]}
    assert "STALE_SURFACE_PACK" in codes
    assert "EXECUTION_FROM_AUDIT_PACK" in codes


def test_unexpired_sports_surface_pack_reusable_for_analysis_not_buy() -> None:
    pack = {
        "packId": "sports_surface_1",
        "packType": "surfacePack",
        "domain": "sports",
        "stage": "surface",
        "schemaVersion": "1.0",
        "observedAt": "2099-06-17T15:30:00Z",
        "validUntil": "2099-06-17T15:31:00Z",
        "scope": {"sport": "worldcup"},
        "summary": "fresh PM/HL board",
        "payload": {"markets": [{"venue": "polymarket", "bid": 0.45, "ask": 0.46}]},
        "reusePolicy": {
            "canReuseFor": ["analysis", "final_answer"],
            "mustRehydrateBefore": ["execute", "place_order", "recommend_buy"],
            "ttlSeconds": 60,
        },
        "lineage": {},
    }

    analysis_report = validate_rehydrate_policy(pack, action="analysis")
    buy_report = validate_rehydrate_policy(pack, action="recommend_buy")

    assert analysis_report["payload"]["status"] == "pass"
    assert buy_report["payload"]["status"] == "fail"
    assert any(
        issue["code"] == "EXECUTION_FROM_AUDIT_PACK"
        for issue in buy_report["payload"]["issues"]
    )


def test_validate_decision_requires_surface_pack() -> None:
    pack = {
        "packId": "decision_1",
        "packType": "decisionPack",
        "domain": "sports",
        "stage": "decision",
        "schemaVersion": "1.0",
        "observedAt": "2026-06-17T15:30:00Z",
        "validUntil": "2026-06-17T15:31:00Z",
        "scope": {},
        "summary": "decision",
        "payload": {"rows": []},
        "inputPacks": [],
        "reusePolicy": {"mustRehydrateBefore": ["recommend_buy"], "ttlSeconds": 60},
        "lineage": {},
    }

    report = validate_decision_pack(pack)

    assert report["payload"]["status"] == "fail"
    assert any(issue["code"] == "DECISION_WITHOUT_SURFACE" for issue in report["payload"]["issues"])


def test_validation_report_shape() -> None:
    report = validation_report(stage="pre_final", issues=[], input_packs=["pack_1"])

    assert report["packType"] == "validationReport"
    assert report["payload"]["status"] == "pass"
    assert report["inputPacks"] == ["pack_1"]
