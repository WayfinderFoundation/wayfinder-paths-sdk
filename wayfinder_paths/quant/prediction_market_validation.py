"""Validation checks for compact prediction-market WorkPacks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from wayfinder_paths.quant.prediction_market_surface import require_executable_fields
from wayfinder_paths.quant.workpack_dry_run import issue, validation_report

ACTIONABLE = {
    "BUY",
    "SELL",
    "BUY_YES",
    "BUY_NO",
    "SHORT",
    "LONG",
    "ENTER",
    "TRADE",
}
SIMPLE_BINARY = {"simple_binary", "pm_simple_binary"}
NON_BINARY = {
    "pm_event_independent_binaries",
    "pm_exclusive_multi",
    "pm_neg_risk",
    "pm_aug_neg_risk",
    "pm_partial_50_50",
    "pm_custom_resolution",
    "exclusive_multi",
    "neg_risk",
    "aug_neg_risk",
    "partial_50_50",
    "custom_resolution",
}
HL_MID_ONLY = {"hl_mid_only"}
HL_UNKNOWN = {"hl_unknown_spec"}
EDGE_MODES = {
    "settlement_edge",
    "mark_to_market_edge",
    "relative_value_edge",
    "arb_or_conversion_edge",
}


def _is_actionable(value: Any) -> bool:
    text = str(value or "").upper()
    return any(token in text for token in ACTIONABLE)


def _surface_from_pack(surface_or_pack: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = surface_or_pack.get("payload") or {}
    if payload.get("surfaceLite"):
        return payload["surfaceLite"]
    if surface_or_pack.get("rows") is not None and surface_or_pack.get("profile"):
        return surface_or_pack
    return payload


def _decision_rows(decision: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    payload = decision.get("payload") or decision
    rows = payload.get("rows") or payload.get("decisions") or []
    return [row for row in rows if isinstance(row, Mapping)]


def _profile(surface: Mapping[str, Any], row: Mapping[str, Any] | None = None) -> str:
    if row and row.get("profile"):
        return str(row["profile"])
    return str(surface.get("profile") or surface.get("surfaceKind") or "")


def validate_surface_lite(surface: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the compact surface visible to agents."""

    issues: list[dict[str, Any]] = []
    profile = str(surface.get("profile") or "")
    if not profile:
        issues.append(
            issue(
                "PM_RESOLUTION_PROFILE_MISSING",
                "surfaceLite has no resolution profile.",
            )
        )
    missing = require_executable_fields(surface, for_action=False)
    if "rows" in missing:
        issues.append(
            issue(
                "PM_SURFACE_MISSING_EXECUTABLE_PRICE",
                "surfaceLite has no rows.",
                fix_stage="surface",
                auto_fixable=True,
            )
        )
    if profile in NON_BINARY and not (
        surface.get("resolutionRef")
        or surface.get("fullRef")
        or profile
        in {
            "pm_partial_50_50",
            "partial_50_50",
            "pm_exclusive_multi",
            "exclusive_multi",
            "pm_neg_risk",
            "neg_risk",
            "pm_aug_neg_risk",
            "aug_neg_risk",
        }
    ):
        issues.append(
            issue(
                "PM_RESOLUTION_PROFILE_MISSING",
                "Non-binary surface needs a built-in profile, resolutionRef, or fullRef.",
                fix_stage="surface",
                auto_fixable=True,
            )
        )
    return validation_report(stage="prediction_surface_lite", issues=issues)


def validate_resolution_profile(
    surface: Mapping[str, Any],
    full_pack: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate profile support and resolver availability."""

    issues: list[dict[str, Any]] = []
    profile = str(surface.get("profile") or "")
    if not profile:
        issues.append(
            issue("PM_RESOLUTION_PROFILE_MISSING", "Missing resolution profile.")
        )
    if profile in {"pm_custom_resolution", "custom_resolution"}:
        parsed = (((full_pack or {}).get("payload") or {}).get("resolution") or {}).get(
            "parsed"
        )
        if not parsed:
            issues.append(
                issue(
                    "PM_RESOLUTION_PROFILE_UNSUPPORTED",
                    "custom_resolution requires a parsed resolver in surfaceFull or a non-actionable decision.",
                    fix_stage="resolution",
                )
            )
    if profile in HL_UNKNOWN:
        issues.append(
            issue(
                "PM_HL_UNKNOWN_SETTLEMENT_SPEC",
                "Hyperliquid surface lacks enough settlement/spec detail for actionable EV.",
                fix_stage="surface",
            )
        )
    return validation_report(stage="prediction_resolution", issues=issues)


def validate_decision_uses_correct_math(
    surface: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> dict[str, Any]:
    """Ensure decisions use math compatible with PM/HL profile."""

    issues: list[dict[str, Any]] = []
    for row in _decision_rows(decision):
        profile = _profile(surface, row)
        method = str(
            row.get("mathHelper")
            or row.get("posteriorMethod")
            or row.get("evMethod")
            or ""
        ).lower()
        action = row.get("decision") or row.get("action") or row.get("side")
        if profile in NON_BINARY and (
            "binary" in method or method in {"binary_yes_ev", "binary_no_ev"}
        ):
            issues.append(
                issue(
                    "PM_BINARY_MATH_USED_FOR_NON_BINARY",
                    f"Decision uses binary math for non-binary profile {profile}.",
                    fix_stage="decision",
                )
            )
        if _is_actionable(action) and profile in HL_MID_ONLY:
            issues.append(
                issue(
                    "PM_HL_MID_ONLY_ACTIONABLE_BUY",
                    "Hyperliquid mid-only surface cannot support an actionable trade.",
                    fix_stage="surface",
                    auto_fixable=True,
                )
            )
        if _is_actionable(action) and profile in HL_UNKNOWN:
            issues.append(
                issue(
                    "PM_HL_UNKNOWN_SETTLEMENT_SPEC",
                    "Hyperliquid settlement/spec is unknown; return WATCH/NEEDS_REPAIR.",
                    fix_stage="surface",
                )
            )
    return validation_report(stage="prediction_math", issues=issues)


def validate_exit_plan(
    surface: Mapping[str, Any], decision: Mapping[str, Any]
) -> dict[str, Any]:
    """Every actionable edge needs settlement, exit, RV, or arb plan metadata."""

    issues: list[dict[str, Any]] = []
    for row in _decision_rows(decision):
        action = row.get("decision") or row.get("action") or row.get("side")
        if not _is_actionable(action):
            continue
        edge_mode = str(row.get("edgeMode") or "")
        if edge_mode not in EDGE_MODES:
            issues.append(
                issue(
                    "PM_BUY_WITHOUT_EXIT_OR_SETTLEMENT_PLAN",
                    "Actionable prediction-market decision lacks edgeMode.",
                    fix_stage="decision",
                )
            )
            continue
        holding_plan = row.get("holdingPlan") or {}
        if not isinstance(holding_plan, Mapping):
            holding_plan = {}
        if edge_mode == "mark_to_market_edge":
            target_exit = holding_plan.get("targetExit") or row.get("targetExit") or {}
            future = row.get("expectedExitBid") or (
                target_exit.get("price") if isinstance(target_exit, Mapping) else None
            )
            if future is None:
                issues.append(
                    issue(
                        "PM_EXIT_EDGE_WITHOUT_FUTURE_BID_ASSUMPTION",
                        "Exit-before-close edge lacks expected future bid or target exit price.",
                        fix_stage="decision",
                    )
                )
        if edge_mode == "settlement_edge":
            if row.get("settlementEv") is None and not row.get("evBreakdown", {}).get(
                "settlementEv"
            ):
                issues.append(
                    issue(
                        "PM_BUY_WITHOUT_EXIT_OR_SETTLEMENT_PLAN",
                        "Settlement edge lacks settlement EV or evBreakdown.settlementEv.",
                        fix_stage="decision",
                    )
                )
    return validation_report(stage="prediction_exit_plan", issues=issues)


def validate_prediction_market_surface_pack(pack: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a full prediction-market surfacePack with embedded surfaceLite."""

    surface = _surface_from_pack(pack)
    issues = validate_surface_lite(surface)["payload"]["issues"]
    return validation_report(
        stage="prediction_surface",
        issues=issues,
        input_packs=[str(pack.get("packId", ""))],
    )


def validate_prediction_market_decision_pack(pack: Mapping[str, Any]) -> dict[str, Any]:
    """Standalone decision validation when the surface is embedded or row-local."""

    payload = pack.get("payload") or {}
    surface = payload.get("surfaceLite") or {}
    issues: list[dict[str, Any]] = []
    if surface:
        issues.extend(
            validate_decision_uses_correct_math(surface, pack)["payload"]["issues"]
        )
        issues.extend(validate_exit_plan(surface, pack)["payload"]["issues"])
    else:
        for row in _decision_rows(pack):
            surface_row = {"profile": row.get("profile")}
            issues.extend(
                validate_decision_uses_correct_math(
                    surface_row, {"payload": {"rows": [row]}}
                )["payload"]["issues"]
            )
            issues.extend(
                validate_exit_plan(surface_row, {"payload": {"rows": [row]}})[
                    "payload"
                ]["issues"]
            )
    return validation_report(
        stage="prediction_decision",
        issues=issues,
        input_packs=[str(pack.get("packId", ""))],
    )
