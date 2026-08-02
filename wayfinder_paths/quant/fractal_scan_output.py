"""Result shaping for deterministic Fractal Scan evidence."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import numpy as np


def label_match_scopes(
    matches: list[dict[str, Any]],
    *,
    selected_symbol: str | None,
    exact_symbol: str,
    exact_source: str,
    canonical_symbol: Callable[[str], str | None],
) -> list[dict[str, Any]]:
    result = []
    for match in matches:
        if match["source"] == exact_source and match["symbol"] == exact_symbol:
            match_scope = "same_market"
        elif (
            selected_symbol is not None
            and canonical_symbol(str(match["symbol"])) == selected_symbol
        ):
            match_scope = "same_asset_proxy"
        else:
            match_scope = "cross_market"
        result.append({**match, "match_scope": match_scope})
    return result


def build_view_data(
    analysis: dict[str, Any], matches: list[dict[str, Any]]
) -> dict[str, Any]:
    forward_paths = [match["forward_path_bps"] for match in matches]
    fan = None
    if forward_paths:
        width = min(len(path) for path in forward_paths)
        values = np.asarray([path[:width] for path in forward_paths], dtype=float)
        fan = {
            "step_bars": list(range(width)),
            "q25_bps": [
                round(float(value), 1) for value in np.quantile(values, 0.25, axis=0)
            ],
            "median_bps": [
                round(float(value), 1) for value in np.median(values, axis=0)
            ],
            "q75_bps": [
                round(float(value), 1) for value in np.quantile(values, 0.75, axis=0)
            ],
        }
    analogue_paths = [
        {
            "symbol": match["symbol"],
            "source": match["source"],
            "match_scope": match["match_scope"],
            "start_ms": match["start_ms"],
            "shape_path_bps": match["shape_path_bps"],
        }
        for match in matches
        if "shape_path_bps" in match
    ]
    return {
        "pattern_shape_bps": analysis["pattern"]["shape_path_bps"],
        "analogue_paths": analogue_paths,
        "forward_fan": fan,
    }


def pattern_metrics(
    rows: list[dict[str, float | int | None]],
) -> dict[str, float | None]:
    closes = [float(row["c"]) for row in rows]
    highs = [float(row["h"]) for row in rows if row.get("h") is not None]
    lows = [float(row["l"]) for row in rows if row.get("l") is not None]
    volumes = [float(row["v"]) for row in rows if row.get("v") is not None]
    last = closes[-1]
    return {
        "return_bps": round((last / closes[0] - 1) * 10_000, 1),
        "range_bps": (
            round(((max(highs) - min(lows)) / last) * 10_000, 1)
            if highs and lows
            else None
        ),
        "low": min(lows) if lows else min(closes),
        "high": max(highs) if highs else max(closes),
        "last": last,
        "volume": round(sum(volumes), 2) if volumes else None,
    }


def regime_stats(
    rows: list[dict[str, float | int | None]], interval_ms: int
) -> dict[str, float | None]:
    closes = np.asarray([float(row["c"]) for row in rows], dtype=float)

    def trailing_return(bars: int) -> float | None:
        if len(closes) <= bars:
            return None
        return round(float((closes[-1] / closes[-1 - bars] - 1) * 10_000), 1)

    realized_vol = None
    if len(closes) >= 21:
        periods_per_year = 365 * 86_400_000 / interval_ms
        realized_vol = round(
            float(
                np.std(np.diff(np.log(closes[-21:])))
                * math.sqrt(periods_per_year)
                * 100
            ),
            2,
        )
    return {
        "return_20_bar_bps": trailing_return(20),
        "return_50_bar_bps": trailing_return(50),
        "realized_vol_20_pct": realized_vol,
    }


def confidence_label(exact_matches: int, coverage_ratio: float) -> str:
    if exact_matches >= 15 and coverage_ratio >= 0.95:
        return "high"
    if exact_matches >= 8 and coverage_ratio >= 0.9:
        return "medium"
    return "low"
