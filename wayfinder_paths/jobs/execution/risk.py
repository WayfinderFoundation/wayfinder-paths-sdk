"""Account-level risk-limit halts for the live/paper driver.

Config lives at `workspace/risk_limits.json` (legacy `RiskLimits` schema —
reused directly so limit semantics have a single source of truth). Living in
`workspace/` means edits change the workspace revision hash, exactly like a
model artifact: risk limits are part of strategy identity.

A breached limit downgrades a `valid` snapshot to `risk_halt`, which the
engine already routes to reduce-only mode — positions can still exit, new
risk cannot be added. No file == no checks == byte-identical driver behavior.

Division of labor vs `auto_limits`: auto_limits are per-intent caps enforced
inside the engine at decide time; risk limits are account-level circuit
breakers evaluated before the tick from forward telemetry + the ledger.

`min_rolling_30d_sharpe` is deferred: it needs a persisted forward equity
history that does not exist yet. `RiskLimits.check` skips missing snapshot
keys, so configs that set it load fine and simply never trip it here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pandas as pd

from wayfinder_paths.core.strategies.risk_limits import RiskLimits
from wayfinder_paths.jobs.execution.engine import EngineState
from wayfinder_paths.jobs.execution.primitives import (
    DEFAULT_INITIAL_CAPITAL,
    CompletedBarsView,
)

RISK_STATE_PATH = "state/risk_state.json"
FORWARD_SUMMARY_PATH = "results/forward/summary.json"
FORWARD_TRADES_PATH = "results/forward/trades.jsonl"


def check_risk_halt(
    root: Path,
    *,
    state: EngineState,
    view: CompletedBarsView,
    params: Mapping[str, Any],
    now: pd.Timestamp,
) -> tuple[str | None, dict[str, Any]]:
    """Returns (halt_reason | None, snapshot_used). Persists peak equity to
    state/risk_state.json so drawdown is deterministic per tick."""
    limits = RiskLimits.load_optional(Path(root) / "workspace")
    if limits is None:
        return None, {}
    snapshot = build_risk_snapshot(
        state=state, view=view, params=params, root=root, now=now
    )
    reason = limits.check(snapshot)
    state_path = Path(root) / RISK_STATE_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {"peak_equity": snapshot["peak_equity"], "updated_at": now.isoformat()},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return reason, snapshot


def build_risk_snapshot(
    *,
    state: EngineState,
    view: CompletedBarsView,
    params: Mapping[str, Any],
    root: Path,
    now: pd.Timestamp,
) -> dict[str, Any]:
    """Maps driver-side telemetry onto the RiskLimits.check keys.

    Equity = initial_capital + closed-trade net_pnl (forward summary — the
    driver-side realized source of truth, which survives engine-state
    adoption) + ledger unrealized marked at the latest closes. Conservative
    vs a true venue-equity feed (fees/funding on open positions not marked).
    """
    initial_capital = float(params.get("initial_capital") or DEFAULT_INITIAL_CAPITAL)
    summary = _read_json(Path(root) / FORWARD_SUMMARY_PATH) or {}
    trades_summary = summary.get("trades") or {}
    net_pnl = float(trades_summary.get("net_pnl") or 0.0)

    unrealized = 0.0
    gross_exposure = 0.0
    positions_usd: dict[str, float] = {}
    for symbol, position in state.ledger.positions.items():
        close = (
            float(view.latest(symbol)["close"])
            if symbol in view.symbols
            else position.avg_price
        )
        direction = 1 if position.side == "long" else -1
        unrealized += direction * (close - position.avg_price) * position.size
        notional = position.size * close
        gross_exposure += abs(notional)
        positions_usd[symbol] = direction * notional

    equity = initial_capital + net_pnl + unrealized
    risk_state = _read_json(Path(root) / RISK_STATE_PATH)
    peak_equity = float(risk_state["peak_equity"]) if risk_state else None
    if peak_equity is None or equity > peak_equity:
        peak_equity = equity  # first tick seeds peak == equity -> drawdown 0
    drawdown = (equity / peak_equity - 1.0) if peak_equity > 0 else 0.0

    return {
        "equity": equity,
        "peak_equity": peak_equity,
        "drawdown": drawdown,
        "gross_exposure_usd": gross_exposure,
        "positions_usd": positions_usd,
        "daily_pnl_usd": _daily_pnl_usd(root, now),
        "consecutive_losses": int(trades_summary.get("current_loss_streak") or 0),
    }


def _daily_pnl_usd(root: Path, now: pd.Timestamp) -> float:
    today = now.tz_convert("UTC").strftime("%Y-%m-%d")
    total = 0.0
    path = Path(root) / FORWARD_TRADES_PATH
    if not path.exists():
        return total
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        # Parse guard, not a cast: tolerate a torn final line from a crash
        # mid-append — one bad line must not brick every future risk check.
        try:
            row = json.loads(line)
        except ValueError:
            continue
        stamp = str(row.get("closed_at") or row.get("ts") or "")
        if stamp[:10] != today:
            continue
        value = row.get("net_pnl")
        if value is not None:
            total += float(value)
    return total


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    match loaded:
        case dict():
            return loaded
        case _:
            return None
