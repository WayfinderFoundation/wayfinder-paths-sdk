"""Account-level risk-limit halts for the live/paper driver.

Config lives at `workspace/risk_limits.json` (legacy `RiskLimits` schema —
reused directly so limit semantics have a single source of truth). Living in
`workspace/` means edits change the workspace revision hash, exactly like a
model artifact: risk limits are part of strategy identity.

A breached limit downgrades a `valid` snapshot to `risk_halt`; the driver also
latches the existing durable halt file. Positions can still exit, new risk is
blocked, and an operator must explicitly clear the halt. No file == no checks
== byte-identical driver behavior.

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
    _float_or_none,
)
from wayfinder_paths.jobs.gating import governance_hard_constraints

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
    account_equity: float | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Returns (halt_reason | None, snapshot_used). Persists peak equity to
    state/risk_state.json so drawdown is deterministic per tick."""
    limits = _apply_governance_caps(
        RiskLimits.load_optional(Path(root) / "workspace"),
        governance_hard_constraints(root),
    )
    if limits is None:
        return None, {}
    snapshot = build_risk_snapshot(
        state=state,
        view=view,
        params=params,
        root=root,
        now=now,
        account_equity=account_equity,
    )
    reason = limits.check(snapshot)
    state_path = Path(root) / RISK_STATE_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "peak_equity": snapshot["peak_equity"],
                # The source the PEAK belongs to (not this tick's source):
                # without it, the next tick's source check discards the peak
                # and live drawdown resets to 0 every tick — a dead halt.
                "equity_source": snapshot["peak_equity_source"],
                "updated_at": now.isoformat(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return reason, snapshot


def _apply_governance_caps(
    limits: RiskLimits | None, hard_constraints: Mapping[str, Any]
) -> RiskLimits | None:
    """Owner-owned ceilings (governance hard_constraints.yaml) clamp the
    agent-writable workspace/risk_limits.json: the agent file may be STRICTER
    than governance, never looser. ``max_drawdown`` follows the RiskLimits
    convention (negative decimal; either sign is tolerated and normalized),
    ``max_gross_exposure_usd`` is a plain USD cap. A governance ceiling with
    no agent file still enforces — deleting risk_limits.json must not lift
    the owner's ceiling. No ceilings set -> limits returned unchanged."""
    gov_drawdown = _float_or_none(hard_constraints.get("max_drawdown"))
    gov_exposure = _float_or_none(
        hard_constraints.get("max_gross_exposure_usd")
        if hard_constraints.get("max_gross_exposure_usd") is not None
        else hard_constraints.get("max_gross_exposure")
    )
    if gov_drawdown is None and gov_exposure is None:
        return limits
    if limits is None:
        limits = RiskLimits()
    if gov_drawdown is not None:
        gov_drawdown = -abs(gov_drawdown)
        if limits.max_drawdown is None or limits.max_drawdown < gov_drawdown:
            limits.max_drawdown = gov_drawdown
    if gov_exposure is not None and gov_exposure > 0:
        if (
            limits.max_gross_exposure_usd is None
            or limits.max_gross_exposure_usd > gov_exposure
        ):
            limits.max_gross_exposure_usd = gov_exposure
    return limits


def build_risk_snapshot(
    *,
    state: EngineState,
    view: CompletedBarsView,
    params: Mapping[str, Any],
    root: Path,
    now: pd.Timestamp,
    account_equity: float | None = None,
) -> dict[str, Any]:
    """Maps driver-side telemetry onto the RiskLimits.check keys.

    Live equity comes from the reconciled venue account value. Paper equity is
    initial capital + forward closed-trade PnL + funding + ledger unrealized at
    the latest closes. The optional override keeps one snapshot shape across
    both modes without treating configured capital as live collateral.
    """
    initial_capital = float(params.get("initial_capital") or DEFAULT_INITIAL_CAPITAL)
    summary = _read_json(Path(root) / FORWARD_SUMMARY_PATH) or {}
    trades_summary = summary.get("trades") or {}
    net_pnl = float(trades_summary.get("net_pnl") or 0.0)
    # Funding is real PnL that never appears in trade rows — excluding it
    # fires false drawdown halts on funding-heavy shorts (and vice versa).
    funding_total = float((summary.get("funding") or {}).get("total_usd") or 0.0)

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

    # Live venue equity already includes realized, unrealized, fees, and
    # funding. Config capital remains the deterministic paper/backtest source.
    equity = (
        float(account_equity)
        if account_equity is not None
        else initial_capital + net_pnl + funding_total + unrealized
    )
    equity_source = "venue" if account_equity is not None else "modelled"
    risk_state = _read_json(Path(root) / RISK_STATE_PATH) or {}
    persisted_peak = (
        float(risk_state["peak_equity"]) if "peak_equity" in risk_state else None
    )
    # Legacy risk_state.json predates the source stamp: only modelled-era
    # semantics ever wrote it, so default the recorded source to "modelled".
    persisted_source = str(risk_state.get("equity_source") or "modelled")
    if persisted_peak is None or (
        persisted_source == "modelled" and equity_source == "venue"
    ):
        # First tick — or venue equity appearing over a modelled peak. The
        # venue is ground truth: seed a fresh venue peak rather than comparing
        # (or forever carrying) across sources. Drawdown starts at 0.
        peak_equity = equity
        peak_source = equity_source
        drawdown = 0.0
    elif persisted_source == equity_source:
        peak_equity = max(persisted_peak, equity)
        peak_source = equity_source
        drawdown = (equity / peak_equity - 1.0) if peak_equity > 0 else 0.0
    else:
        # Venue peak, modelled tick (an ambiguous venue fetch fell back to
        # modelled equity): comparing modelled equity to a venue peak is
        # meaningless math that once latched spurious halts. Carry the venue
        # peak untouched — don't reseed, don't compare — and skip the
        # drawdown check for this tick only.
        peak_equity = persisted_peak
        peak_source = persisted_source
        drawdown = 0.0

    return {
        "equity": equity,
        "equity_source": equity_source,
        "peak_equity": peak_equity,
        "peak_equity_source": peak_source,
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
