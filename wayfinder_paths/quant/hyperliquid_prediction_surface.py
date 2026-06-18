"""Hyperliquid prediction/derivative surface normalization.

Hyperliquid surfaces are normally derivative-style exposure, not CTF payout
tokens. These helpers classify how much executable/spec detail is present.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_hl_market(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize search/mid/L2/context rows into a compact HL surface payload."""

    asset_ctx = raw.get("assetCtx") or raw.get("asset_context") or raw.get("market") or {}
    l2 = raw.get("l2") or raw.get("l2Book") or raw.get("orderBook") or raw.get("book")
    mid = _float_or_none(raw.get("mid") or raw.get("midPx") or asset_ctx.get("midPx"))
    normalized = {
        "venue": "hyperliquid",
        "coin": raw.get("coin") or raw.get("name") or raw.get("asset") or raw.get("symbol"),
        "dex": raw.get("dex") or raw.get("builder") or raw.get("exchange"),
        "mid": mid,
        "l2": l2,
        "assetCtx": dict(asset_ctx) if isinstance(asset_ctx, Mapping) else {},
        "funding": _float_or_none(raw.get("funding") or asset_ctx.get("funding")),
        "openInterest": _float_or_none(
            raw.get("openInterest") or raw.get("open_interest") or asset_ctx.get("openInterest")
        ),
        "settlement": raw.get("settlement") or raw.get("resolution") or raw.get("oracleSpec"),
        "boundedPayoff": raw.get("boundedPayoff") or raw.get("bounded_event"),
        "oraclePx": _float_or_none(raw.get("oraclePx") or asset_ctx.get("oraclePx")),
        "raw": dict(raw),
    }
    normalized["profile"] = classify_hl_profile(normalized)
    return normalized


def _has_l2_depth(value: Any) -> bool:
    if not value:
        return False
    if isinstance(value, Mapping):
        levels = value.get("levels") or value.get("bids") or value.get("asks")
        if isinstance(levels, list) and levels:
            return True
        if isinstance(value.get("book"), Mapping):
            return _has_l2_depth(value["book"])
    return False


def classify_hl_profile(normalized: Mapping[str, Any]) -> str:
    """Classify HL market as mid-only, derivative, bounded event, or unknown."""

    explicit = normalized.get("profile")
    if explicit and str(explicit).startswith("hl_"):
        return str(explicit)
    if normalized.get("boundedPayoff"):
        return "hl_bounded_event"
    if normalized.get("settlement") and (
        normalized.get("oraclePx") is not None or normalized.get("assetCtx")
    ):
        return "hl_oracle_settled"
    if _has_l2_depth(normalized.get("l2")):
        return "hl_l2_derivative"
    if normalized.get("mid") is not None:
        return "hl_mid_only"
    return "hl_unknown_spec"


def hl_exit_ev(
    *,
    side: str,
    entry: float,
    expected_exit: float,
    funding_cost: float = 0.0,
    fees: float = 0.0,
    slippage: float = 0.0,
) -> dict[str, float | str]:
    """Return per-unit derivative exit EV for long/short HL exposure."""

    side_normalized = str(side).lower()
    if side_normalized not in {"long", "short"}:
        raise ValueError("side must be 'long' or 'short'")
    gross = float(expected_exit) - float(entry)
    if side_normalized == "short":
        gross = -gross
    ev = gross - float(funding_cost) - float(fees) - float(slippage)
    return {
        "side": side_normalized,
        "entry": float(entry),
        "expectedExit": float(expected_exit),
        "grossEv": gross,
        "fundingCost": float(funding_cost),
        "fees": float(fees),
        "slippage": float(slippage),
        "ev": ev,
    }
