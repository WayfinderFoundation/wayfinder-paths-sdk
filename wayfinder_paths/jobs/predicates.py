"""Typed lifecycle predicates: machine-evaluable graduate/kill rules.

Prose criteria ("kill if WR<20% after 10 trades") depend on an agent reading
and honoring them; a rule that fires only if someone re-reads it is not a
rule. Predicates are data — {metric}{__op} thresholds evaluated by the
deterministic lifecycle controller against measured forward metrics.

Ops: __gte, __lte, __gt, __lt, __eq. Prerequisite keys (min_closed_trades,
min_days) gate evaluation: until they are met the predicate set is PENDING,
not failed — pre-registered sample-size gates, mechanically enforced.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_OPS = {
    "__gte": lambda value, limit: value >= limit,
    "__lte": lambda value, limit: value <= limit,
    "__gt": lambda value, limit: value > limit,
    "__lt": lambda value, limit: value < limit,
    "__eq": lambda value, limit: value == limit,
}
_PREREQUISITES = {"min_closed_trades": "closed_trades", "min_days": "days"}


def evaluate_predicates(
    rules: Mapping[str, Any] | None,
    metrics: Mapping[str, Any],
) -> dict[str, Any]:
    """Return {status: pending|met|not_met, checks: [...], missing: [...]}.

    `met` requires EVERY non-prerequisite rule to hold with all prerequisites
    satisfied. A metric absent from `metrics` records as missing and the set
    cannot be `met` — silence must never satisfy a rule.
    """
    if not rules:
        return {"status": "pending", "checks": [], "missing": ["no rules registered"]}

    checks: list[dict[str, Any]] = []
    missing: list[str] = []
    for key, limit in rules.items():
        if key in _PREREQUISITES:
            metric_name = _PREREQUISITES[key]
            value = metrics.get(metric_name)
            if value is None or float(value) < float(limit):
                return {
                    "status": "pending",
                    "checks": checks,
                    "missing": missing,
                    "waiting_on": f"{metric_name} {value} < {limit}",
                }
            continue
        metric_name, op = _split(key)
        if op is None:
            missing.append(f"unknown op in rule {key!r}")
            continue
        value = metrics.get(metric_name)
        if value is None:
            missing.append(f"metric {metric_name!r} unavailable")
            continue
        passed = _OPS[op](float(value), float(limit))
        checks.append(
            {"rule": key, "value": float(value), "limit": float(limit), "passed": passed}
        )

    if missing:
        return {"status": "pending", "checks": checks, "missing": missing}
    if checks and all(check["passed"] for check in checks):
        return {"status": "met", "checks": checks, "missing": []}
    return {"status": "not_met", "checks": checks, "missing": []}


def _split(key: str) -> tuple[str, str | None]:
    for op in _OPS:
        if key.endswith(op):
            return key[: -len(op)], op
    return key, None


def _row_ts(row: Mapping[str, Any]) -> str:
    # Forward trade rows stamp `ts`/`closed_at` (execution schema), not
    # `timestamp` — checking only the latter silently excluded every row and
    # froze adjudication at pending (found on live xyz data, 24 real closes
    # measured as 0).
    return str(row.get("timestamp") or row.get("closed_at") or row.get("ts") or "")


def forward_metrics(
    trades: list[Mapping[str, Any]],
    *,
    symbol: str | None = None,
    since: str | None = None,
    now_iso: str,
) -> dict[str, Any]:
    """Measured forward metrics for one leg/incumbent from closed-trade rows.

    Filters by symbol and deployment time so a leg is judged only on its own
    trades. Drawdown is computed on the cumulative net-pnl path (per-symbol
    forward equity does not exist as an artifact)."""
    import datetime as dt

    rows = [
        row
        for row in trades
        if (symbol is None or str(row.get("symbol")) == symbol)
        and (since is None or _row_ts(row) >= since)
    ]
    pnls = [float(row.get("net_pnl") or row.get("pnl") or 0.0) for row in rows]
    wins = sum(1 for value in pnls if value > 0)
    peak = 0.0
    trough_dd = 0.0
    running = 0.0
    for value in pnls:
        running += value
        peak = max(peak, running)
        trough_dd = max(trough_dd, peak - running)
    streak = 0
    for value in reversed(pnls):
        if value < 0:
            streak += 1
        else:
            break
    days = None
    if since:
        try:
            start = dt.datetime.fromisoformat(since)
            end = dt.datetime.fromisoformat(now_iso)
            days = round((end - start).total_seconds() / 86_400.0, 2)
        except ValueError:
            days = None
    return {
        "closed_trades": len(rows),
        "win_rate": (wins / len(rows)) if rows else None,
        "net_pnl": round(sum(pnls), 4),
        "drawdown_usd": round(trough_dd, 4),
        "loss_streak": streak,
        "days": days,
    }
