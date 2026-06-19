"""Compact prediction-market surface helpers.

The full hydrated PM/HL payload belongs on disk in a WorkPack. Agents should
usually receive only a compact surfaceLite: rows, profile, refs, and warnings.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from wayfinder_paths.quant.hyperliquid_prediction_surface import classify_hl_profile

PM_PROFILES = {
    "pm_simple_binary",
    "pm_event_independent_binaries",
    "pm_exclusive_multi",
    "pm_neg_risk",
    "pm_aug_neg_risk",
    "pm_partial_50_50",
    "pm_custom_resolution",
    "pm_sports_board",
}
HL_PROFILES = {
    "hl_mid_only",
    "hl_l2_derivative",
    "hl_event_perp",
    "hl_bounded_event",
    "hl_oracle_settled",
    "hl_unknown_spec",
}
GENERIC_PROFILES = {
    "simple_binary",
    "exclusive_multi",
    "partial_50_50",
    "augmented_other",
    "custom_resolution",
    "derivative_perp",
}


def _first_present(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mid(bid: float | None, ask: float | None, fallback: Any = None) -> float | None:
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return _float_or_none(fallback)


def _depth_flag(row: Mapping[str, Any], bid: float | None, ask: float | None) -> str:
    flag = row.get("depthFlag") or row.get("liquidityFlag")
    if flag:
        return str(flag)
    if bid is not None and ask is not None:
        return "ok"
    if row.get("mid") is not None or row.get("price") is not None:
        return "mid_only"
    return "missing"


def _is_yes_no(labels: Sequence[str]) -> bool:
    normalized = {label.strip().lower() for label in labels}
    return normalized == {"yes", "no"}


def _market_outcome_rows(market: Mapping[str, Any]) -> list[list[Any]]:
    outcomes = market.get("outcomes") or []
    if not isinstance(outcomes, Sequence) or isinstance(outcomes, (str, bytes)):
        outcomes = []

    rows: list[list[Any]] = []
    if outcomes:
        for outcome in outcomes:
            if isinstance(outcome, Mapping):
                label = str(outcome.get("label") or outcome.get("name") or "")
                bid = _float_or_none(_first_present(outcome, "bid", "bestBid"))
                ask = _float_or_none(_first_present(outcome, "ask", "bestAsk"))
                mid = _mid(bid, ask, _first_present(outcome, "mid", "price"))
                rows.append([label, bid, ask, mid, _depth_flag(outcome, bid, ask)])
            else:
                rows.append([str(outcome), None, None, None, "missing"])
        return rows

    label = str(
        _first_present(market, "label", "outcome", "question", "symbol", "coin")
        or "market"
    )
    bid = _float_or_none(_first_present(market, "bid", "bestBid", "yesBid"))
    ask = _float_or_none(_first_present(market, "ask", "bestAsk", "yesAsk"))
    mid = _mid(bid, ask, _first_present(market, "mid", "price"))
    return [[label, bid, ask, mid, _depth_flag(market, bid, ask)]]


def _all_markets(full_pack: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    payload = full_pack.get("payload") or {}
    markets = payload.get("markets") or full_pack.get("markets") or []
    return [row for row in markets if isinstance(row, Mapping)]


def classify_resolution_profile(full_pack: Mapping[str, Any]) -> dict[str, Any]:
    """Return a compact profile enum and warnings for a hydrated surface pack."""

    payload = full_pack.get("payload") or {}
    resolution = payload.get("resolution") or {}
    explicit = (
        resolution.get("profile")
        or payload.get("profile")
        or full_pack.get("profile")
        or full_pack.get("surfaceKind")
    )
    warnings: list[str] = []
    if explicit:
        profile = str(explicit)
        if profile in GENERIC_PROFILES:
            mapping = {
                "simple_binary": "pm_simple_binary",
                "exclusive_multi": "pm_exclusive_multi",
                "partial_50_50": "pm_partial_50_50",
                "augmented_other": "pm_aug_neg_risk",
                "custom_resolution": "pm_custom_resolution",
                "derivative_perp": "hl_event_perp",
            }
            profile = mapping[profile]
        return {"profile": profile, "warnings": warnings}

    venue = str(payload.get("venue") or full_pack.get("venue") or "").lower()
    markets = _all_markets(full_pack)
    if venue == "hyperliquid" or payload.get("assetCtx") or payload.get("mid"):
        return {"profile": classify_hl_profile(payload), "warnings": warnings}

    if not markets:
        return {
            "profile": "pm_custom_resolution",
            "warnings": ["no_markets_to_classify"],
        }

    if any(bool(row.get("negRisk") or row.get("neg_risk")) for row in markets):
        labels = [
            str(out.get("label") or out.get("name") or "")
            for market in markets
            for out in (market.get("outcomes") or [])
            if isinstance(out, Mapping)
        ]
        if any(label.lower() in {"other", "placeholder"} for label in labels):
            return {
                "profile": "pm_aug_neg_risk",
                "warnings": ["augmented_other_requires_rules"],
            }
        return {"profile": "pm_neg_risk", "warnings": warnings}

    outcome_counts = []
    binary_count = 0
    for market in markets:
        labels = [
            str(out.get("label") or out.get("name") or out)
            for out in (market.get("outcomes") or [])
        ]
        if not labels and (market.get("yesTokenId") or market.get("noTokenId")):
            labels = ["Yes", "No"]
        outcome_counts.append(len(labels))
        if len(labels) == 2 and _is_yes_no(labels):
            binary_count += 1

    text = str(resolution.get("text") or payload.get("description") or "").lower()
    if "50-50" in text or "50/50" in text or "unknown" in text:
        return {"profile": "pm_partial_50_50", "warnings": warnings}
    if len(markets) > 1 and binary_count == len(markets):
        return {"profile": "pm_event_independent_binaries", "warnings": warnings}
    if any(count > 2 for count in outcome_counts):
        return {"profile": "pm_exclusive_multi", "warnings": warnings}
    return {"profile": "pm_simple_binary", "warnings": warnings}


def compact_surface_lite(full_pack: Mapping[str, Any]) -> dict[str, Any]:
    """Return a token-efficient surfaceLite from a hydrated surfaceFull pack."""

    payload = full_pack.get("payload") or {}
    profile_info = classify_resolution_profile(full_pack)
    pack_id = str(full_pack.get("packId") or payload.get("packId") or "")
    full_ref = str(full_pack.get("path") or payload.get("fullRef") or "")
    rows: list[list[Any]] = []
    for market in _all_markets(full_pack):
        rows.extend(_market_outcome_rows(market))
    if not rows and isinstance(payload.get("rows"), list):
        rows = list(payload["rows"])

    ids = {
        "eventSlug": payload.get("eventSlug")
        or (payload.get("event") or {}).get("slug"),
        "conditionId": payload.get("conditionId"),
        "coin": payload.get("coin"),
        "dex": payload.get("dex"),
    }
    return {
        "packType": "surfacePack",
        "domain": "prediction_markets",
        "venue": str(payload.get("venue") or full_pack.get("venue") or "").lower()
        or None,
        "surfaceKind": payload.get("surfaceKind") or profile_info["profile"],
        "observedAt": full_pack.get("observedAt"),
        "validUntil": full_pack.get("validUntil"),
        "event": payload.get("eventSlug") or (payload.get("event") or {}).get("slug"),
        "market": payload.get("marketSlug")
        or payload.get("coin")
        or payload.get("market"),
        "profile": profile_info["profile"],
        "rows": rows,
        "ids": {key: value for key, value in ids.items() if value},
        "resolutionRef": f"{pack_id}#resolution" if pack_id else None,
        "orderbookRef": f"{pack_id}#books" if pack_id else None,
        "fullRef": full_ref,
        "warnings": profile_info["warnings"],
    }


def require_executable_fields(
    surface_lite: Mapping[str, Any], *, for_action: bool
) -> list[str]:
    """Return missing fields for shortlist/action gates."""

    missing: list[str] = []
    rows = surface_lite.get("rows") or []
    if not rows:
        missing.append("rows")
    if for_action and not surface_lite.get("validUntil"):
        missing.append("validUntil")
    if for_action and not surface_lite.get("profile"):
        missing.append("profile")
    if for_action and not surface_lite.get("fullRef"):
        missing.append("fullRef")
    if for_action:
        has_executable = False
        for row in rows:
            if (
                isinstance(row, Sequence)
                and not isinstance(row, (str, bytes))
                and len(row) >= 3
            ):
                if row[1] is not None or row[2] is not None:
                    has_executable = True
                    break
        if not has_executable:
            missing.append("bid_or_ask")
    return missing
