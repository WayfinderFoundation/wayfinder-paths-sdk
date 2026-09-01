"""Deterministic execution defenses shared by backtest, paper, and live.

The explicitly enabled overlay is deliberately small and engine-owned. It
neither promotes a strategy nor asks an agent for judgment: a same-direction
stop-loss streak temporarily blocks new entries for that symbol, while a
synchronized cross-sectional shock scales newly proposed exposure until breadth
normalizes. Protective exits are never scaled or blocked.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from wayfinder_paths.jobs.execution.primitives import (
        CompletedBarsView,
        OrderIntent,
    )

OOD_ENTRY_SCALE_COLUMN = "__wf_ood_entry_scale"
OOD_ACTIVE_COLUMN = "__wf_ood_breadth_thrust"
DEFENSE_FEATURE_WARMUP_BARS = 450
DEFENSE_VOL_BASELINE_BARS = 400


def defense_feature_warmup_bars(interval_seconds: int | float | None) -> int:
    """History needed for a lagged 400-bar baseline plus a 24h return."""
    if interval_seconds is None or interval_seconds <= 0:
        return DEFENSE_FEATURE_WARMUP_BARS
    horizon = max(2, math.ceil(86_400 / float(interval_seconds)))
    return max(DEFENSE_FEATURE_WARMUP_BARS, horizon + DEFENSE_VOL_BASELINE_BARS + 2)


def defense_policy(params: Mapping[str, Any]) -> dict[str, Any]:
    raw = params.get("defense_overlay")
    if raw is None or raw is False:
        return {"enabled": False}
    config = dict(raw) if isinstance(raw, Mapping) else {}
    return {
        "enabled": bool(config.get("enabled", True)),
        "stop_loss_streak": max(1, int(config.get("stop_loss_streak", 3))),
        "stand_down_hours": max(1.0, float(config.get("stand_down_hours", 12))),
        "ood_sigma": max(1.0, float(config.get("ood_sigma", 3.0))),
        "ood_min_fraction": min(
            1.0, max(0.5, float(config.get("ood_min_fraction", 0.75)))
        ),
        "ood_entry_scale": min(
            1.0, max(0.0, float(config.get("ood_entry_scale", 0.25)))
        ),
    }


def add_defense_features(
    view: CompletedBarsView, params: Mapping[str, Any]
) -> CompletedBarsView:
    from wayfinder_paths.jobs.execution.primitives import CompletedBarsView

    policy = defense_policy(params)
    if not policy["enabled"]:
        return view
    frame = view.to_frame()
    if OOD_ENTRY_SCALE_COLUMN in frame.columns:
        return view
    stamps = sorted(pd.to_datetime(frame["timestamp"], utc=True).unique())
    frame[OOD_ACTIVE_COLUMN] = False
    frame[OOD_ENTRY_SCALE_COLUMN] = 1.0
    if len(stamps) < 120:
        return CompletedBarsView(frame)
    deltas = pd.Series(stamps).diff().dropna().dt.total_seconds()
    if deltas.empty or float(deltas.median()) <= 0:
        return CompletedBarsView(frame)
    horizon = max(2, int(round(86_400 / float(deltas.median()))))
    baseline = DEFENSE_VOL_BASELINE_BARS
    if len(stamps) <= horizon + baseline:
        return CompletedBarsView(frame)

    close = frame.pivot(
        index="timestamp", columns="symbol", values="close"
    ).sort_index()
    close = close.apply(pd.to_numeric, errors="coerce")
    one_bar = close.pct_change(fill_method=None)
    horizon_return = close.pct_change(horizon, fill_method=None)
    expected_sigma = one_bar.rolling(baseline, min_periods=baseline).std().shift(
        1
    ) * math.sqrt(horizon)
    zscore = horizon_return / expected_sigma.replace(0.0, np.nan)
    eligible = zscore.notna().sum(axis=1)
    required = max(3, math.ceil(len(close.columns) * policy["ood_min_fraction"]))
    synchronized = (eligible >= required) & (
        (zscore.ge(policy["ood_sigma"]).sum(axis=1) >= required)
        | (zscore.le(-policy["ood_sigma"]).sum(axis=1) >= required)
    )
    active_by_stamp = synchronized.to_dict()
    frame[OOD_ACTIVE_COLUMN] = frame["timestamp"].map(active_by_stamp).fillna(False)
    frame[OOD_ENTRY_SCALE_COLUMN] = np.where(
        frame[OOD_ACTIVE_COLUMN], policy["ood_entry_scale"], 1.0
    )
    return CompletedBarsView(frame)


def current_ood_entry_scale(view: CompletedBarsView) -> float:
    try:
        return float(view.feature(OOD_ENTRY_SCALE_COLUMN))
    except (TypeError, ValueError):
        return 1.0


def active_stand_down_symbols(
    defense_state: dict[str, Any], *, now: pd.Timestamp
) -> set[str]:
    blocked: set[str] = set()
    stand_downs = defense_state.setdefault("stand_downs", {})
    for symbol, raw_until in list(stand_downs.items()):
        try:
            until = _utc_timestamp(raw_until)
        except (TypeError, ValueError):
            stand_downs.pop(symbol, None)
            continue
        if now < until:
            blocked.add(str(symbol))
        else:
            stand_downs.pop(symbol, None)
    return blocked


def record_stop_loss_result(
    defense_state: dict[str, Any],
    *,
    symbol: str,
    direction: str | None,
    realized_pnl: float,
    timestamp: Any,
    stopped_out: bool = True,
) -> dict[str, Any] | None:
    policy = defense_state.get("policy") or {}
    if not policy.get("enabled") or not direction:
        return None
    streaks = defense_state.setdefault("stop_loss_streaks", {})
    key = f"{symbol}:{direction}"
    # Consecutive means consecutive closed outcomes in this direction. A
    # profitable close or an ordinary (non-stop) exit breaks the streak.
    if not stopped_out or realized_pnl >= 0:
        streaks[key] = 0
        return None
    streak = int(streaks.get(key) or 0) + 1
    streaks[key] = streak
    threshold = int(policy.get("stop_loss_streak") or 3)
    if streak < threshold:
        return None
    until = _utc_timestamp(timestamp) + timedelta(
        hours=float(policy.get("stand_down_hours") or 12)
    )
    defense_state.setdefault("stand_downs", {})[symbol] = until.isoformat()
    return {
        "kind": "loss_streak_symbol_stand_down",
        "symbol": symbol,
        "direction": direction,
        "streak": streak,
        "blocked_until": until.isoformat(),
    }


def is_stop_loss_fill(row: Mapping[str, Any]) -> bool:
    raw = row.get("raw") or {}
    metadata = raw.get("intent_metadata") or {}
    action = str(raw.get("intent_action") or metadata.get("action") or "").upper()
    reason = str(metadata.get("exit_reason") or "")
    return action == "STOP_LOSS" or reason in {"bracket_stop", "stop_loss"}


def scale_entry_intents(intents: Sequence[OrderIntent], scale: float) -> int:
    from wayfinder_paths.jobs.execution.primitives import REDUCE_ONLY_ACTIONS

    if not 0 <= scale < 1:
        return 0
    changed = 0
    for intent in intents:
        if intent.reduce_only or str(intent.action).upper() in REDUCE_ONLY_ACTIONS:
            continue
        if intent.notional is not None:
            intent.notional = float(intent.notional) * scale
        if intent.size is not None:
            intent.size = float(intent.size) * scale
        metadata = dict(intent.metadata or {})
        metadata["defense_ood_entry_scale"] = scale
        intent.metadata = metadata
        changed += 1
    return changed


def _utc_timestamp(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        return stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC")
