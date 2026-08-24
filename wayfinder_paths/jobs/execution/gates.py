"""Observable strategy-gate snapshots and descriptive attribution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.primitives import CompletedBarsView

GATE_PREFIX = "gate_"


def latest_gate_state(view: CompletedBarsView) -> dict[str, dict[str, Any]]:
    """Read synchronized causal ``gate_*`` columns from the latest bar."""
    if not view.timestamps:
        return {}
    latest = view.timestamps[-1]
    by_gate: dict[str, dict[str, bool]] = {}
    for symbol in view.symbols:
        frame = view.symbol_frame(symbol)
        if frame.empty or pd.Timestamp(frame.iloc[-1]["timestamp"]) != latest:
            continue
        row = frame.iloc[-1]
        for column in frame.columns:
            if not str(column).startswith(GATE_PREFIX) or pd.isna(row[column]):
                continue
            by_gate.setdefault(str(column), {})[symbol] = bool(row[column])

    snapshot: dict[str, dict[str, Any]] = {}
    expected = set(view.symbols)
    for name, values in sorted(by_gate.items()):
        uniform = set(values) == expected and len(set(values.values())) == 1
        snapshot[name] = {
            "scope": "portfolio" if uniform else "symbol",
            "active": next(iter(values.values())) if uniform else None,
            "by_symbol": values,
        }
    return snapshot


def summarize_gate_diagnostics(
    runs: Sequence[Mapping[str, Any]],
    equity_curve: Sequence[Mapping[str, Any]],
    positions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Summarize observed gate coverage without claiming causal lift."""
    gate_names = sorted(
        {str(name) for run in runs for name in (run.get("gates") or {})}
    )
    if not gate_names:
        return {}
    equity = {str(row["timestamp"]): float(row["equity"]) for row in equity_curve}
    position_rows = {str(row["timestamp"]): row for row in positions}
    initial_equity = next(iter(equity.values()), 0.0)
    output: dict[str, Any] = {}
    for name in gate_names:
        observations: list[tuple[str, Mapping[str, Any]]] = []
        for run in runs:
            value = (run.get("gates") or {}).get(name)
            if isinstance(value, Mapping):
                observations.append((str(run["timestamp"]), value))
        scopes = {str(value.get("scope")) for _, value in observations}
        if scopes == {"portfolio"}:
            output[name] = _portfolio_summary(
                observations, equity, position_rows, initial_equity
            )
        else:
            output[name] = _symbol_summary(observations, position_rows)
    return output


def _portfolio_summary(
    observations: Sequence[tuple[str, Mapping[str, Any]]],
    equity: Mapping[str, float],
    positions: Mapping[str, Mapping[str, Any]],
    initial_equity: float,
) -> dict[str, Any]:
    active = [bool(value.get("active")) for _, value in observations]
    transitions = sum(
        left != right for left, right in zip(active, active[1:], strict=False)
    )
    activations = sum(
        value and (index == 0 or not active[index - 1])
        for index, value in enumerate(active)
    )
    result: dict[str, Any] = {
        "scope": "portfolio",
        "observed_bars": len(active),
        "active_bars": sum(active),
        "inactive_bars": len(active) - sum(active),
        "active_fraction": round(sum(active) / len(active), 6) if active else None,
        "activation_count": activations,
        "transition_count": transitions,
        "activation_transitions": transitions,
        "states": {},
        "interpretation": "descriptive conditional attribution, not causal lift",
    }
    previous_equity: float | None = None
    previous_state: bool | None = None
    pnl_by_state: dict[bool, list[float]] = {True: [], False: []}
    exposure_by_state: dict[bool, list[tuple[float, float]]] = {
        True: [],
        False: [],
    }
    for timestamp, value in observations:
        state = bool(value.get("active"))
        current_equity = equity.get(timestamp)
        if (
            current_equity is not None
            and previous_equity is not None
            and previous_state is not None
        ):
            pnl_by_state[previous_state].append(current_equity - previous_equity)
        if current_equity is not None:
            previous_equity = current_equity
            previous_state = state
        exposure_by_state[state].append(_entry_price_exposure(positions.get(timestamp)))
    for state, label in ((True, "active"), (False, "inactive")):
        pnls = pnl_by_state[state]
        exposures = exposure_by_state[state]
        gross = [item[0] for item in exposures]
        net = [item[1] for item in exposures]
        result["states"][label] = {
            "pnl_usd": round(sum(pnls), 6),
            "max_drawdown_pct": _conditional_drawdown(pnls, initial_equity),
            "mean_gross_notional_usd": _mean(gross),
            "max_gross_notional_usd": round(max(gross), 6) if gross else None,
            "mean_net_notional_usd": _mean(net),
            "max_abs_net_notional_usd": (
                round(max((abs(value) for value in net), default=0.0), 6)
                if net
                else None
            ),
        }
    return result


def _symbol_summary(
    observations: Sequence[tuple[str, Mapping[str, Any]]],
    positions: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    counts: dict[str, dict[str, Any]] = {}
    previous: dict[str, bool] = {}
    gross: list[float] = []
    net: list[float] = []
    for timestamp, value in observations:
        for symbol, is_active in (value.get("by_symbol") or {}).items():
            bucket = counts.setdefault(
                str(symbol),
                {
                    "observed_bars": 0,
                    "active_bars": 0,
                    "activation_count": 0,
                    "transition_count": 0,
                },
            )
            state = bool(is_active)
            bucket["observed_bars"] += 1
            bucket["active_bars"] += int(state)
            if state and not previous.get(str(symbol), False):
                bucket["activation_count"] += 1
            if str(symbol) in previous and state != previous[str(symbol)]:
                bucket["transition_count"] += 1
            previous[str(symbol)] = state
        gross_value, net_value = _entry_price_exposure(positions.get(timestamp))
        gross.append(gross_value)
        net.append(net_value)
    for bucket in counts.values():
        bucket["active_fraction"] = round(
            bucket["active_bars"] / bucket["observed_bars"], 6
        )
    return {
        "scope": "symbol",
        "symbols": counts,
        "mean_gross_notional_usd": _mean(gross),
        "mean_net_notional_usd": _mean(net),
        "pnl_attribution": None,
        "interpretation": "per-symbol gates do not support portfolio PnL attribution",
    }


def _entry_price_exposure(row: Mapping[str, Any] | None) -> tuple[float, float]:
    records = (row or {}).get("positions") or {}
    notionals = [
        (1.0 if record.get("side") == "long" else -1.0)
        * float(record.get("size") or 0.0)
        * float(record.get("avg_price") or 0.0)
        for record in records.values()
    ]
    return sum(abs(value) for value in notionals), sum(notionals)


def _conditional_drawdown(pnls: Sequence[float], initial_equity: float) -> float | None:
    if not pnls or initial_equity <= 0:
        return None
    equity = initial_equity
    peak = initial_equity
    worst = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1.0)
    return round(worst, 6)


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None
