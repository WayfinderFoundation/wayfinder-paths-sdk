"""Small shared helpers for the mixed-asset starter strategies."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.primitives import ExecutionContext
from wayfinder_paths.jobs.indicators import atr

MEAN_REVERSION_STOP_DEFAULTS: dict[str, Any] = {
    "stop_atr_period": 24,
    "stop_atr_multiple": 3.0,
    "stop_min_pct": 0.03,
    "stop_max_pct": 0.06,
    "stop_cooldown_seconds": 86_400,
    "native_stop_required": True,
}
RANKING_STOP_DEFAULTS: dict[str, Any] = {
    "stop_atr_multiple": 4.0,
    "stop_min_pct": 0.06,
    "stop_max_pct": 0.12,
    "stop_cooldown_seconds": 86_400,
    "native_stop_required": True,
}
PAIR_PROTECTION_DEFAULTS: dict[str, Any] = {
    "stop_atr_period": 20,
    "stop_atr_multiple": 6.0,
    "stop_min_pct": 0.12,
    "stop_max_pct": 0.20,
    "native_stop_required": True,
    "protection_monitor_interval_seconds": 300,
    "pair_max_entry_equity_loss_pct": 0.03,
    "pair_max_entry_gross_loss_pct": 0.08,
}


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


def add_stop_atr(
    derived: dict[str, pd.DataFrame],
    frames: Mapping[str, pd.DataFrame],
    *,
    period: int,
) -> dict[str, pd.DataFrame]:
    """Add the shared starter ATR column without replacing other features."""
    for symbol, frame in frames.items():
        features = derived.setdefault(symbol, pd.DataFrame(index=frame.index))
        features["starter_stop_atr"] = atr(frame, period)
    return derived


def stop_brackets(
    ctx: ExecutionContext,
    symbols: Sequence[str],
    params: Mapping[str, Any],
    *,
    protection_group: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build fill-relative, volatility-scaled stop policies for OPEN intents."""
    rows = current_rows(ctx, symbols, required_columns=("starter_stop_atr",))
    if rows is None:
        return {}
    multiple = float(params["stop_atr_multiple"])
    minimum = float(params["stop_min_pct"])
    maximum = float(params["stop_max_pct"])
    cooldown_seconds = int(params.get("stop_cooldown_seconds") or 0)
    policies: dict[str, dict[str, Any]] = {}
    for symbol, row in rows.items():
        close = float(row["close"])
        atr_value = float(row["starter_stop_atr"])
        if pd.isna(atr_value) or close <= 0 or atr_value <= 0:
            continue
        stop_pct = min(maximum, max(minimum, multiple * atr_value / close))
        policy: dict[str, Any] = {
            "stop_loss_pct": stop_pct,
            "policy": "conservative",
            "native_required": bool(params.get("native_stop_required", True)),
        }
        if cooldown_seconds:
            policy["cooldown_seconds"] = cooldown_seconds
        if protection_group:
            group = dict(protection_group)
            account_value = (ctx.state_snapshot.data or {}).get("account_value")
            if account_value is not None:
                group["entry_account_equity"] = float(account_value)
            policy["protection_group"] = group
        policies[symbol] = policy
    return policies


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
