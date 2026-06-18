"""Sports-specific WorkPack dry-run checks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from wayfinder_paths.quant.sports_modifiers import validate_modifier
from wayfinder_paths.quant.workpack_dry_run import issue, validation_report


def _rows(pack: Mapping[str, Any], *keys: str) -> list[Mapping[str, Any]]:
    payload = pack.get("payload") or {}
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, Mapping)]
    return []


def _is_actionable(row: Mapping[str, Any]) -> bool:
    decision = str(row.get("decision") or row.get("action") or "").upper()
    return decision.startswith("BUY") or decision in {"BET", "TRADE", "READY_FOR_APPROVAL"}


def validate_sports_surface(surface_pack: Mapping[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    if surface_pack.get("packType") != "surfacePack":
        issues.append(issue("INVALID_PACK_TYPE", "Expected sports surfacePack."))
    markets = _rows(surface_pack, "markets", "orderBooks", "boards")
    if not markets:
        issues.append(
            issue(
                "MISSING_EXECUTABLE_PRIOR",
                "Sports surface has no executable PM/HL markets.",
                fix_stage="surface",
                auto_fixable=True,
            )
        )
    for market in markets:
        market_type = str(market.get("marketType") or market.get("type") or "").lower()
        outcomes = [str(out.get("label") or out.get("name") or "").lower() for out in market.get("outcomes") or [] if isinstance(out, Mapping)]
        if market_type in {"1x2", "three_way", "three-way", "soccer_moneyline"}:
            if not any(outcome == "draw" for outcome in outcomes):
                issues.append(
                    issue(
                        "BINARY_COLLAPSED_MULTI_OUTCOME",
                        "Three-way soccer board is missing explicit draw outcome.",
                        fix_stage="surface",
                        auto_fixable=True,
                    )
                )
    return validation_report(stage="sports_surface", issues=issues, input_packs=[str(surface_pack.get("packId", ""))])


def validate_sports_features(feature_pack: Mapping[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    if feature_pack.get("packType") != "featurePack":
        issues.append(issue("INVALID_PACK_TYPE", "Expected sports featurePack."))
    payload = feature_pack.get("payload") or {}
    if payload.get("usesFutureData") is True:
        issues.append(issue("FEATURE_LEAKAGE_RISK", "featurePack declares future-data usage."))
    if payload.get("thinSample") and not payload.get("thinSampleFlagged"):
        issues.append(
            issue(
                "THIN_SAMPLE_UNFLAGGED",
                "Thin sample is present but not surfaced in diagnostics.",
                severity="warn",
                fix_stage="feature",
            )
        )
    return validation_report(stage="sports_features", issues=issues, input_packs=[str(feature_pack.get("packId", ""))])


def validate_sports_modifiers(
    context_pack: Mapping[str, Any],
    feature_pack: Mapping[str, Any],
    recipe: Mapping[str, Any],
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    modifiers = (context_pack.get("payload") or {}).get("modelModifiers") or []
    for modifier in modifiers:
        result = validate_modifier(dict(modifier), recipe=dict(recipe), feature_pack=dict(feature_pack))
        for row in result.get("issues") or []:
            issues.append(row)
    return validation_report(stage="sports_modifiers", issues=issues, input_packs=[str(context_pack.get("packId", "")), str(feature_pack.get("packId", ""))])


def validate_sports_analysis(analysis_pack: Mapping[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    if analysis_pack.get("packType") != "analysisPack":
        issues.append(issue("INVALID_PACK_TYPE", "Expected sports analysisPack."))
    rows = _rows(analysis_pack, "rows", "markets", "candidates")
    if not rows:
        issues.append(
            issue(
                "DECISION_WITHOUT_ANALYSIS",
                "Sports analysis has no model rows.",
                severity="warn",
                fix_stage="analysis",
            )
        )
    for row in rows:
        if "posteriorLedger" not in row and _is_actionable(row):
            issues.append(
                issue(
                    "NO_POSTERIOR_LEDGER",
                    "Actionable row lacks posterior ledger.",
                    severity="warn",
                    fix_stage="decision",
                )
            )
    return validation_report(stage="sports_analysis", issues=issues, input_packs=[str(analysis_pack.get("packId", ""))])


def validate_sports_decision(
    decision_pack: Mapping[str, Any],
    surface_pack: Mapping[str, Any],
    analysis_pack: Mapping[str, Any],
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    surface_markets = _rows(surface_pack, "markets", "orderBooks", "boards")
    analysis_rows = _rows(analysis_pack, "rows", "markets", "candidates")
    if not surface_markets:
        issues.append(issue("MISSING_EXECUTABLE_PRIOR", "No executable sports surface rows."))
    if not analysis_rows:
        issues.append(issue("DECISION_WITHOUT_ANALYSIS", "No sports analysis rows."))
    for row in _rows(decision_pack, "rows", "decisions"):
        if not _is_actionable(row):
            continue
        venue = str(row.get("venue") or row.get("source") or "").lower()
        entry = row.get("entryPrice")
        prior = row.get("marketPrior")
        if venue in {"sportsbook", "book", "books"}:
            issues.append(
                issue(
                    "SPORTSBOOK_ONLY_MARKED_ACTIONABLE",
                    "Sportsbook context cannot be the executable venue.",
                    fix_stage="surface",
                    auto_fixable=True,
                )
            )
        if entry is None and prior is None:
            issues.append(
                issue(
                    "MISSING_EXECUTABLE_PRIOR",
                    "Actionable sports row lacks fresh executable entry/market prior.",
                    fix_stage="surface",
                    auto_fixable=True,
                )
            )
        market_type = str(row.get("marketType") or "").lower()
        side = str(row.get("side") or row.get("outcome") or "").lower()
        if market_type in {"1x2", "three_way", "three-way", "soccer_moneyline"} and side in {"no", "not_favorite"}:
            issues.append(
                issue(
                    "BINARY_COLLAPSED_MULTI_OUTCOME",
                    "Three-way soccer board cannot be reduced to favorite YES/NO.",
                    fix_stage="surface",
                    auto_fixable=True,
                )
            )
    return validation_report(
        stage="sports_decision",
        issues=issues,
        input_packs=[
            str(decision_pack.get("packId", "")),
            str(surface_pack.get("packId", "")),
            str(analysis_pack.get("packId", "")),
        ],
    )
