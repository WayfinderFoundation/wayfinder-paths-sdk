from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_DOWN, Decimal
from typing import Any


def spot_index_from_asset_id(spot_asset_id: int) -> int:
    if spot_asset_id < 10000:
        raise ValueError(f"Expected spot asset_id >= 10000, got {spot_asset_id}")
    return int(spot_asset_id) - 10000


def normalize_l2_book(
    raw: dict[str, Any],
    *,
    fallback_mid: float | None = None,
) -> dict[str, Any]:
    def coerce_levels(levels: Any) -> list[tuple[float, float]]:
        normalized: list[tuple[float, float]] = []
        if not isinstance(levels, list):
            return normalized
        for level in levels:
            try:
                if isinstance(level, dict):
                    px = float(level.get("px"))
                    sz = float(level.get("sz"))
                elif isinstance(level, (list, tuple)) and len(level) >= 2:
                    px = float(level[0])
                    sz = float(level[1])
                else:
                    continue
            except (TypeError, ValueError):
                continue
            if px > 0 and sz > 0:
                normalized.append((px, sz))
        return normalized

    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []

    levels = raw.get("levels")
    if isinstance(levels, list) and levels:
        bids = coerce_levels(levels[0])
        if len(levels) > 1:
            asks = coerce_levels(levels[1])
    else:
        bids = coerce_levels(raw.get("bids"))
        asks = coerce_levels(raw.get("asks"))

    mid_px = None
    try:
        mid_raw = raw.get("midPx")
        if mid_raw is not None:
            mid_px = float(mid_raw)
    except (TypeError, ValueError):
        mid_px = None

    if (mid_px is None or mid_px <= 0) and bids and asks:
        mid_px = (bids[0][0] + asks[0][0]) / 2.0
    if (mid_px is None or mid_px <= 0) and fallback_mid:
        mid_px = float(fallback_mid)

    return {
        "bids": bids,
        "asks": asks,
        "midPx": float(mid_px or 0.0),
    }


def usd_depth_in_band(
    book: dict[str, Any], band_bps: int, side: str
) -> tuple[float, float]:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    mid = float(book.get("midPx") or 0.0)

    if mid <= 0.0:
        return 0.0, mid

    lo = mid * (1.0 - band_bps / 1e4)
    hi = mid * (1.0 + band_bps / 1e4)

    def usd_sum(levels: list[tuple[float, float]], predicate) -> float:
        total = 0.0
        for px, sz in levels:
            if predicate(px):
                total += float(px) * float(sz)
        return total

    bids_usd = usd_sum(bids, lambda px: px >= lo)
    asks_usd = usd_sum(asks, lambda px: px <= hi)

    depth_side = asks_usd if side.lower() == "buy" else bids_usd
    return float(depth_side), mid


def sz_decimals_for_asset(
    asset_to_sz_decimals: Mapping[int, int], asset_id: int
) -> int:
    if asset_id not in asset_to_sz_decimals:
        raise ValueError(f"Unknown asset_id {asset_id}: missing szDecimals")
    return int(asset_to_sz_decimals[asset_id])


def size_step(asset_to_sz_decimals: Mapping[int, int], asset_id: int) -> Decimal:
    return Decimal(10) ** (-sz_decimals_for_asset(asset_to_sz_decimals, asset_id))


def round_size_for_asset(
    asset_to_sz_decimals: Mapping[int, int],
    asset_id: int,
    size: float | Decimal,
    *,
    ensure_min_step: bool = False,
) -> float:
    size_d = size if isinstance(size, Decimal) else Decimal(str(size))
    if size_d <= 0:
        return 0.0

    step = size_step(asset_to_sz_decimals, asset_id)
    q = (size_d / step).to_integral_value(rounding=ROUND_DOWN) * step
    if ensure_min_step and q == 0:
        q = step

    decimals = sz_decimals_for_asset(asset_to_sz_decimals, asset_id)
    return float(f"{q:.{decimals}f}")
