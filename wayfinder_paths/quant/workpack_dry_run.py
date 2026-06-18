"""Generic dry-run validation for Wayfinder WorkPacks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from wayfinder_paths.core.packs import PACK_TYPES, is_stale


def issue(
    code: str,
    message: str,
    *,
    severity: str = "error",
    fix_stage: str | None = None,
    auto_fixable: bool = False,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "fixStage": fix_stage,
        "autoFixable": auto_fixable,
    }


def _status(issues: list[dict[str, Any]]) -> str:
    if any(row.get("severity") == "error" for row in issues):
        return "fail"
    if any(row.get("severity") == "warn" for row in issues):
        return "warn"
    return "pass"


def validation_report(
    *,
    stage: str,
    issues: list[dict[str, Any]],
    input_packs: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "packType": "validationReport",
        "domain": "validation",
        "intent": "dry_run",
        "stage": "validation",
        "schemaVersion": "1.0",
        "inputPacks": input_packs or [],
        "summary": f"{_status(issues)} validation at {stage}",
        "payload": {
            "status": _status(issues),
            "stage": stage,
            "issues": issues,
        },
        "reusePolicy": {
            "canReuseFor": ["analysis", "final_answer"],
            "mustRehydrateBefore": [],
            "ttlSeconds": 3600,
        },
        "sensitivity": "public",
    }


def validate_pack_schema(pack: Mapping[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    required = (
        "packType",
        "domain",
        "stage",
        "schemaVersion",
        "observedAt",
        "validUntil",
        "scope",
        "summary",
        "payload",
        "reusePolicy",
        "lineage",
    )
    for field in required:
        if field not in pack:
            issues.append(
                issue(
                    "MISSING_REQUIRED_FIELD",
                    f"Pack is missing `{field}`.",
                    fix_stage="pack_write",
                    auto_fixable=True,
                )
            )
    if pack.get("packType") not in PACK_TYPES:
        issues.append(issue("INVALID_PACK_TYPE", f"Invalid packType {pack.get('packType')!r}."))
    reuse_policy = pack.get("reusePolicy")
    if not isinstance(reuse_policy, Mapping):
        issues.append(issue("MISSING_REHYDRATE_POLICY", "reusePolicy must be an object."))
    elif "mustRehydrateBefore" not in reuse_policy:
        issues.append(
            issue(
                "MISSING_REHYDRATE_POLICY",
                "reusePolicy.mustRehydrateBefore is required.",
                fix_stage="pack_write",
                auto_fixable=True,
            )
        )
    return validation_report(stage="schema", issues=issues, input_packs=[str(pack.get("packId", ""))])


def validate_pack_lineage(packs: list[Mapping[str, Any]]) -> dict[str, Any]:
    seen = {str(pack.get("packId")) for pack in packs if pack.get("packId")}
    issues: list[dict[str, Any]] = []
    for pack in packs:
        for input_pack in pack.get("inputPacks") or []:
            if str(input_pack) not in seen:
                issues.append(
                    issue(
                        "BROKEN_LINEAGE",
                        f"{pack.get('packId')} references missing input pack {input_pack}.",
                        severity="warn",
                        fix_stage="handoff",
                    )
                )
    return validation_report(stage="lineage", issues=issues, input_packs=list(seen))


def validate_rehydrate_policy(pack: Mapping[str, Any], *, action: str) -> dict[str, Any]:
    issues = []
    if is_stale(pack):
        issues.append(
            issue(
                "STALE_SURFACE_PACK",
                "Pack is stale or expired and must be refreshed before use.",
                fix_stage=str(pack.get("stage") or "surface"),
                auto_fixable=True,
            )
        )
    must_rehydrate = set((pack.get("reusePolicy") or {}).get("mustRehydrateBefore") or [])
    if action in must_rehydrate:
        issues.append(
            issue(
                "EXECUTION_FROM_AUDIT_PACK",
                f"Pack declares it must be rehydrated before `{action}`.",
                fix_stage=str(pack.get("stage") or "surface"),
                auto_fixable=True,
            )
        )
    return validation_report(stage=f"pre_{action}", issues=issues, input_packs=[str(pack.get("packId", ""))])


def validate_surface_pack(pack: Mapping[str, Any]) -> dict[str, Any]:
    issues = validate_pack_schema(pack)["payload"]["issues"]
    if pack.get("packType") != "surfacePack":
        issues.append(issue("INVALID_PACK_TYPE", "Expected surfacePack."))
    payload = pack.get("payload") or {}
    if not any(key in payload for key in ("markets", "quotes", "routes", "orderBooks", "balances")):
        issues.append(
            issue(
                "MISSING_EXECUTABLE_PRIOR",
                "surfacePack payload has no markets, quotes, routes, orderBooks, or balances.",
                severity="warn",
                fix_stage="surface",
            )
        )
    if pack.get("domain") == "prediction_markets":
        from wayfinder_paths.quant.prediction_market_validation import (
            validate_prediction_market_surface_pack,
        )

        issues.extend(validate_prediction_market_surface_pack(pack)["payload"]["issues"])
    return validation_report(stage="surface", issues=issues, input_packs=[str(pack.get("packId", ""))])


def validate_decision_pack(pack: Mapping[str, Any]) -> dict[str, Any]:
    issues = validate_pack_schema(pack)["payload"]["issues"]
    if pack.get("packType") != "decisionPack":
        issues.append(issue("INVALID_PACK_TYPE", "Expected decisionPack."))
    input_packs = [str(value) for value in pack.get("inputPacks") or []]
    if not input_packs:
        issues.append(issue("DECISION_WITHOUT_SURFACE", "decisionPack has no inputPacks."))
    payload = pack.get("payload") or {}
    rows = payload.get("rows") or payload.get("decisions") or []
    if not rows:
        issues.append(
            issue(
                "DECISION_WITHOUT_ANALYSIS",
                "decisionPack has no decision rows.",
                severity="warn",
                fix_stage="decision",
            )
        )
    if pack.get("domain") == "prediction_markets":
        from wayfinder_paths.quant.prediction_market_validation import (
            validate_prediction_market_decision_pack,
        )

        issues.extend(validate_prediction_market_decision_pack(pack)["payload"]["issues"])
    return validation_report(stage="decision", issues=issues, input_packs=input_packs)
