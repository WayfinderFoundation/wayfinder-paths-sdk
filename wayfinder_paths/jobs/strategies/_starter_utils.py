"""Small shared helpers for the mixed-asset starter strategies."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.primitives import ExecutionContext
from wayfinder_paths.jobs.indicators import atr, trailing_return

# These brackets are catastrophe exits; each strategy's normal exit or rebalance
# remains responsible for routine risk. The bands below passed full-period and
# four-fold non-regression checks against the same jobs_v1 replay without stops.
MEAN_REVERSION_STOP_DEFAULTS: dict[str, Any] = {
    "stop_atr_period": 24,
    "stop_atr_multiple": 5.0,
    "stop_min_pct": 0.08,
    "stop_max_pct": 0.15,
    "stop_cooldown_seconds": 86_400,
    "native_stop_required": True,
}
RANKING_STOP_DEFAULTS: dict[str, Any] = {
    "stop_atr_multiple": 12.0,
    "stop_min_pct": 0.25,
    "stop_max_pct": 0.50,
    "stop_cooldown_seconds": 86_400,
    "native_stop_required": True,
}
PAIR_PROTECTION_DEFAULTS: dict[str, Any] = {
    # Pair-level monitoring owns the tighter 3% equity / 8% gross loss budget.
    # The per-leg bracket is intentionally wider so one leg cannot churn the pair.
    "stop_atr_period": 20,
    "stop_atr_multiple": 15.0,
    "stop_min_pct": 0.40,
    "stop_max_pct": 0.60,
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


def protection_cooldown_active(ctx: ExecutionContext, symbol: str) -> bool:
    """Return whether an engine-recorded stop cooldown still blocks entry.

    The engine persists ISO timestamps in ``strategy_state`` after a protected
    stop. Strategies with custom order construction must consult the same state
    as the shared target-weight bridge or their declared cooldown is cosmetic.
    Malformed persisted timestamps fail closed until an operator repairs them.
    """
    cooldowns = ctx.strategy_state.get("protection_cooldowns") or {}
    raw_expiry = cooldowns.get(symbol) if isinstance(cooldowns, Mapping) else None
    if not raw_expiry:
        return False
    try:
        expiry = datetime.fromisoformat(str(raw_expiry).replace("Z", "+00:00"))
        now = datetime.fromisoformat(str(ctx.timestamp).replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
    except ValueError:
        return True
    return now < expiry


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
        symbol: pd.DataFrame({column: trailing_return(frame["close"], lookback)})
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


def buffered_rank_weights(
    scores: Mapping[str, float],
    previous: Mapping[str, float],
    *,
    side_count: int,
    gross: float,
    long_only: bool = False,
) -> dict[str, float]:
    """Build a buffered rank basket with deterministic tie breaks.

    By default, half of ``gross`` is assigned to each side. ``long_only``
    assigns the full budget to the top-ranked names and does not require a
    short cross-section.
    """
    if side_count <= 0 or not math.isfinite(gross) or gross < 0:
        raise ValueError("rank side_count must be positive and gross non-negative")
    finite: dict[str, float] = {}
    for symbol, value in scores.items():
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric):
            finite[str(symbol)] = numeric
    required = side_count if long_only else 2 * side_count
    if len(finite) < required:
        return {}
    ranks = pd.Series(finite, dtype=float).rank(method="average", pct=True)
    longs = [
        symbol
        for symbol, weight in previous.items()
        if weight > 0 and symbol in ranks and ranks[symbol] >= 0.55
    ][:side_count]
    for symbol in sorted(ranks.index, key=lambda item: (-ranks[item], item)):
        if len(longs) >= side_count:
            break
        if symbol not in longs:
            longs.append(symbol)
    weights = dict.fromkeys(finite, 0.0)
    if long_only:
        weights.update(dict.fromkeys(longs, gross / side_count))
        return weights

    shorts = [
        symbol
        for symbol, weight in previous.items()
        if weight < 0
        and symbol in ranks
        and ranks[symbol] <= 0.45
        and symbol not in longs
    ][:side_count]
    for symbol in sorted(ranks.index, key=lambda item: (ranks[item], item)):
        if len(shorts) >= side_count:
            break
        if symbol not in shorts and symbol not in longs:
            shorts.append(symbol)
    if len(longs) < side_count or len(shorts) < side_count:
        return {}
    weight_per_leg = gross / (2.0 * side_count)
    weights.update(dict.fromkeys(longs, weight_per_leg))
    weights.update(dict.fromkeys(shorts, -weight_per_leg))
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
