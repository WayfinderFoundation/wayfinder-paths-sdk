"""Small shared helpers for the mixed-asset starter strategies."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.primitives import ExecutionContext


def merge_params(
    defaults: Mapping[str, Any], overrides: Mapping[str, Any] | None
) -> dict[str, Any]:
    params = dict(defaults)
    params.update(dict(overrides or {}))
    params["symbols"] = [str(symbol) for symbol in params.get("symbols") or []]
    return params


def current_feature_values(
    ctx: ExecutionContext,
    symbols: Sequence[str],
    column: str,
) -> dict[str, float] | None:
    """Return one synchronized, finite feature value per symbol.

    A missing/stale leg stands the whole basket down. Ranking a mixture of
    timestamps would silently turn a data outage into a trading signal.
    """
    rows = current_rows(ctx, symbols, required_columns=(column,))
    if rows is None:
        return None
    values: dict[str, float] = {}
    for symbol, row in rows.items():
        value = pd.to_numeric(pd.Series([row[column]]), errors="coerce").iloc[0]
        if pd.isna(value):
            return None
        values[symbol] = float(value)
    return values


def current_rows(
    ctx: ExecutionContext,
    symbols: Sequence[str],
    *,
    required_columns: Sequence[str] = (),
) -> dict[str, pd.Series] | None:
    """Return synchronized latest rows or stand the whole basket down."""
    timestamps = ctx.view.timestamps
    if not timestamps:
        return None
    latest_timestamp = pd.Timestamp(timestamps[-1])
    required = set(required_columns)
    rows: dict[str, pd.Series] = {}
    for symbol in symbols:
        frame = ctx.view.symbol_frame(symbol)
        if frame.empty or not required.issubset(frame.columns):
            return None
        row = frame.iloc[-1]
        if pd.Timestamp(row["timestamp"]) != latest_timestamp:
            return None
        rows[symbol] = row
    return rows


def trailing_return_features(
    frames: Mapping[str, pd.DataFrame],
    *,
    lookback: int,
    column: str,
) -> dict[str, pd.DataFrame]:
    return {
        symbol: pd.DataFrame(
            {
                column: pd.to_numeric(frame["close"], errors="coerce").pct_change(
                    lookback
                )
            }
        )
        for symbol, frame in frames.items()
    }


def ranked_weights(
    scores: Mapping[str, float], *, weight_per_leg: float
) -> dict[str, float]:
    """Long the upper half and short the lower half, deterministically."""
    ranked = sorted(scores, key=lambda symbol: (scores[symbol], symbol))
    midpoint = len(ranked) // 2
    weights = dict.fromkeys(ranked[:midpoint], -weight_per_leg)
    weights.update(dict.fromkeys(ranked[midpoint:], weight_per_leg))
    return weights


def sleeve_weights(
    scores: Mapping[str, float],
    sleeves: Sequence[Sequence[str]],
    *,
    weight_per_leg: float,
) -> dict[str, float]:
    """Long the winner and short the loser independently in each sleeve."""
    weights = {symbol: 0.0 for sleeve in sleeves for symbol in sleeve}
    for sleeve in sleeves:
        if len(sleeve) != 2:
            raise ValueError("starter momentum sleeves must contain two symbols")
        loser, winner = sorted(sleeve, key=lambda symbol: (scores[symbol], symbol))
        weights[loser] = -weight_per_leg
        weights[winner] = weight_per_leg
    return weights
