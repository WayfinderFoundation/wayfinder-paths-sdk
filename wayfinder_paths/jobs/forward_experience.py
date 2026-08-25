"""Owner-scoped forward evidence for research and execution calibration.

The :class:`JobStore` root is the tenant boundary.  This module never queries a
global service: it derives a compact profile only from jobs visible through the
caller's store, keeps actual-live and paper evidence separate, and stamps a
cutoff so later campaign audits cannot accidentally learn from their future.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

from wayfinder_paths.jobs.store import JobStore

CALIBRATION_PATH = "results/research/live_calibration.json"
LOOKBACK_DAYS = 90
EMPIRICAL_MIN_SAMPLES = 50
BLEND_MIN_SAMPLES = 10
AUDIT_COVERAGE_TARGET = 0.85
CONSERVATIVE_PRIOR_BPS = 7.0


def build_forward_experience(
    store: JobStore,
    target_job_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build and persist a leak-resistant owner-only experience profile."""
    current = _aware(now or datetime.now(UTC))
    cutoff = current.isoformat()
    earliest = current - timedelta(days=LOOKBACK_DAYS)
    live_samples: list[dict[str, Any]] = []
    paper: list[dict[str, Any]] = []
    seen: set[str] = set()

    for job in store.list_jobs():
        default_mode = str(job.script_loop.mode or "paper")
        for row in store.read_jsonl(job.id, "results/forward/fills.jsonl"):
            mode = str(row.get("mode") or default_mode)
            stamp = _row_time(row)
            if stamp is None or stamp < earliest or stamp > current:
                continue
            key = _event_key(job.id, "fill", row)
            if key in seen:
                continue
            seen.add(key)
            cost = _execution_cost_bps(row)
            if mode == "live" and cost is not None:
                live_samples.append(
                    {
                        "ts": stamp.isoformat(),
                        "job_id": job.id,
                        "symbol": str(row.get("symbol") or "unknown"),
                        "venue": str(row.get("venue") or "unknown"),
                        "order_type": _order_type(row),
                        "cost_bps": round(cost, 6),
                    }
                )
        trades = store.read_jsonl(job.id, "results/forward/trades.jsonl")
        eligible = [
            row
            for row in trades
            if str(row.get("mode") or default_mode) == "paper"
            and (stamp := _row_time(row)) is not None
            and earliest <= stamp <= current
            and str(row.get("status") or "closed") == "closed"
            and _dedupe_event(seen, _event_key(job.id, "trade", row))
        ]
        if eligible:
            paper.append(
                {
                    "job_id": job.id,
                    "closed_trades": len(eligible),
                    "net_pnl": round(
                        sum(float(row.get("net_pnl") or 0.0) for row in eligible), 6
                    ),
                    "symbols": sorted(
                        {
                            str(row.get("symbol"))
                            for row in eligible
                            if row.get("symbol")
                        }
                    ),
                }
            )

    live_samples.sort(key=lambda row: (row["ts"], row["job_id"], row["symbol"]))
    cells: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in live_samples:
        cells[_cell_key(sample)].append(sample)
    # Venue/order priors are learned from each cell's chronological training
    # partition only; no held-out row can leak into a shrunk cell's prior.
    prior_samples: dict[str, list[float]] = defaultdict(list)
    for rows in cells.values():
        for sample in _training_rows(rows):
            prior_samples[_prior_key(sample)].append(float(sample["cost_bps"]))
    priors = {
        key: {
            "p50_bps": max(CONSERVATIVE_PRIOR_BPS, _quantile(values, 0.50)),
            "p90_bps": max(CONSERVATIVE_PRIOR_BPS, _quantile(values, 0.90)),
        }
        for key, values in prior_samples.items()
    }
    default_prior = {
        "p50_bps": CONSERVATIVE_PRIOR_BPS,
        "p90_bps": CONSERVATIVE_PRIOR_BPS,
    }
    calibrated = {
        key: _calibrate_cell(rows, priors.get(_prior_key(rows[0]), default_prior))
        for key, rows in sorted(cells.items())
    }
    recommended = execution_cost_assumptions({"live_execution": {"cells": calibrated}})
    held_out = sum(int(cell["held_out_samples"]) for cell in calibrated.values())
    covered = sum(int(cell["audit_covered"]) for cell in calibrated.values())
    report = {
        "schema_version": "1.0",
        "owner_scope": "job_store",
        "target_job_id": target_job_id,
        "generated_at": cutoff,
        "forward_context_cutoff": cutoff,
        "lookback_days": LOOKBACK_DAYS,
        "live_execution": {
            "samples": len(live_samples),
            "cells": calibrated,
            "held_out_samples": held_out,
            "audit_coverage": (round(covered / held_out, 4) if held_out else None),
            "audit_target": AUDIT_COVERAGE_TARGET,
            "recommended": recommended,
        },
        "paper_strategy_priors": paper,
        "separation": (
            "Actual-live fills calibrate execution costs; paper trades inform "
            "strategy/drift priors only. Evidence is never pooled across owners."
        ),
    }
    store.write_json(target_job_id, CALIBRATION_PATH, report)
    return report


def execution_cost_assumptions(
    profile: dict[str, Any] | None, *, symbols: set[str] | None = None
) -> dict[str, Any]:
    """Collapse relevant calibration cells into base/stress simulator inputs."""
    cells = ((profile or {}).get("live_execution") or {}).get("cells") or {}
    selected: list[dict[str, Any]] = []
    for key, cell in cells.items():
        parts = str(key).split("|")
        if symbols and len(parts) >= 2 and parts[1] not in symbols:
            continue
        if isinstance(cell, dict):
            selected.append(cell)
    if not selected:
        return {
            "p50_bps": CONSERVATIVE_PRIOR_BPS,
            "p90_bps": CONSERVATIVE_PRIOR_BPS,
            "audit_passed": True,
            "source": "conservative_prior",
            "cells": 0,
        }
    p50 = median(float(cell["p50_bps"]) for cell in selected)
    p90 = max(float(cell["p90_bps"]) for cell in selected)
    audited = [cell for cell in selected if int(cell.get("held_out_samples") or 0) > 0]
    return {
        "p50_bps": round(p50, 4),
        "p90_bps": round(max(p50, p90), 4),
        "audit_passed": all(bool(cell.get("audit_passed")) for cell in audited),
        "source": "owner_live_fills",
        "cells": len(selected),
    }


def _calibrate_cell(
    rows: list[dict[str, Any]], prior: dict[str, float]
) -> dict[str, Any]:
    training_rows = _training_rows(rows)
    train = [float(row["cost_bps"]) for row in training_rows]
    audit = [float(row["cost_bps"]) for row in rows[len(training_rows) :]]
    n = len(train)
    if n >= EMPIRICAL_MIN_SAMPLES:
        method = "empirical"
        weight = 1.0
        p50 = _quantile(train, 0.50)
        p90 = _quantile(train, 0.90)
    elif n >= BLEND_MIN_SAMPLES:
        method = "shrunk"
        weight = n / EMPIRICAL_MIN_SAMPLES
        p50 = weight * _quantile(train, 0.50) + (1.0 - weight) * prior["p50_bps"]
        p90 = weight * _quantile(train, 0.90) + (1.0 - weight) * prior["p90_bps"]
    else:
        method = "conservative_prior"
        weight = 0.0
        p50 = prior["p50_bps"]
        p90 = prior["p90_bps"]
    audit_covered = sum(value <= p90 for value in audit)
    coverage = audit_covered / len(audit) if audit else None
    return {
        "samples": len(rows),
        "training_samples": n,
        "held_out_samples": len(audit),
        "audit_covered": audit_covered,
        "method": method,
        "empirical_weight": round(weight, 4),
        "p50_bps": round(p50, 4),
        "p90_bps": round(max(p50, p90), 4),
        "audit_coverage": round(coverage, 4) if coverage is not None else None,
        "audit_passed": coverage is None or coverage >= AUDIT_COVERAGE_TARGET,
    }


def _training_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    split = max(1, min(len(rows), math.ceil(len(rows) * 0.70)))
    return rows[:split]


def _execution_cost_bps(row: dict[str, Any]) -> float | None:
    raw_value = row.get("raw")
    raw: dict[str, Any] = raw_value if isinstance(raw_value, dict) else {}
    for value in (
        row.get("slippage_bps"),
        row.get("stop_slippage_bps"),
        raw.get("slippage_bps_applied"),
    ):
        if value is None:
            continue
        try:
            cost = abs(float(value))
        except (TypeError, ValueError):
            continue
        if math.isfinite(cost):
            return cost
    reference = raw.get("reference_price") or row.get("reference_price")
    fill = row.get("avg_price") or row.get("price")
    if reference is None or fill is None:
        return None
    try:
        reference_value, fill_value = float(reference), float(fill)
    except (TypeError, ValueError):
        return None
    if not reference_value:
        return None
    cost = abs(fill_value - reference_value) / reference_value * 10_000
    return cost if math.isfinite(cost) else None


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return CONSERVATIVE_PRIOR_BPS
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    low, high = math.floor(index), math.ceil(index)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def _row_time(row: dict[str, Any]) -> datetime | None:
    for key in ("ts", "timestamp", "closed_at", "time"):
        value = row.get(key)
        if value is None:
            continue
        try:
            if isinstance(value, (int, float)):
                scale = 1000 if value > 10_000_000_000 else 1
                return datetime.fromtimestamp(float(value) / scale, tz=UTC)
            return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
        except (OSError, TypeError, ValueError):
            continue
    return None


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _order_type(row: dict[str, Any]) -> str:
    raw_value = row.get("raw")
    raw: dict[str, Any] = raw_value if isinstance(raw_value, dict) else {}
    return str(row.get("order_type") or raw.get("order_type") or "market").lower()


def _cell_key(row: dict[str, Any]) -> str:
    return f"{row['venue']}|{row['symbol']}|{row['order_type']}"


def _prior_key(row: dict[str, Any]) -> str:
    return f"{row['venue']}|{row['order_type']}"


def _event_key(job_id: str, kind: str, row: dict[str, Any]) -> str:
    identity = row.get("fill_id") or row.get("order_id") or row.get("client_order_id")
    return f"{job_id}|{kind}|{identity}|{row.get('ts')}|{row.get('symbol')}|{row.get('avg_price')}"


def _dedupe_event(seen: set[str], key: str) -> bool:
    if key in seen:
        return False
    seen.add(key)
    return True
