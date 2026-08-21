"""Portfolio-level regime and incumbent-health monitor.

The ordinary risk layer answers "has an account limit been breached?".  This
monitor answers the earlier question: "is the deployed edge behaving like the
one we validated, in market conditions that still resemble its training
history?"  It combines three independent evidence families:

* recent 7/14/30-day forward PnL shape (drawdown, drawdown velocity and loss
  concentration),
* forward trade edge versus the backtest population, and
* market-state drift (volatility, cross-symbol correlation, liquidity,
  regime mix and funding).

The report is advisory by default.  An owner can select an automatic response
in protected ``governance/<job_id>/hard_constraints.yaml``::

    regime_response:
      warning: alert_only       # alert_only | clamp_leverage | pause_entries
      critical: pause_entries   # ... | flatten
      max_leverage: 1.0         # required by clamp_leverage
    regime_detector:            # optional protected threshold overrides
      drawdown_warning: 0.04
      drawdown_critical: 0.08

Agent-writable job files cannot loosen or activate that policy. Automatic
responses require a verified protected-governance epoch; legacy jobs remain
alert-only. Pause/flatten responses use the durable risk halt, and therefore
require the owner to clear.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.regime_contract import (
    MARKET_STATE_PATH,
    REGIME_HEALTH_PATH,
    REGIME_HEALTH_SCHEMA_VERSION,
    WINDOW_DAYS,
)
from wayfinder_paths.jobs.store import JobStore

RegimeStatus = Literal["insufficient", "healthy", "watch", "warning", "critical"]

_STATUS_RANK: dict[str, int] = {
    "insufficient": 0,
    "healthy": 1,
    "watch": 2,
    "warning": 3,
    "critical": 4,
}
_ALERT_STATUSES = frozenset({"warning", "critical"})
_RESPONSE_ACTIONS = frozenset(
    {"alert_only", "clamp_leverage", "pause_entries", "flatten"}
)


def regime_health_job(
    job_id: str,
    *,
    store: JobStore | None = None,
    force: bool = False,
    now: dt.datetime | None = None,
    apply_response: bool = True,
) -> dict[str, Any]:
    """Compute and persist the current portfolio-regime health verdict.

    The cache is content- and time-aware: a report is reused only within the
    same UTC hour while forward trades, forensics, risk state, market state
    and protected governance are unchanged. Monitoring failures never alter
    execution.
    """
    store = store or JobStore()
    job = store.load(job_id)
    root = store.job_dir(job_id)
    now = now or dt.datetime.now(dt.UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)
    previous = _read_json(root / REGIME_HEALTH_PATH) or {}
    fingerprint = _input_fingerprint(root)
    fingerprint["evaluation_hour"] = now.replace(
        minute=0, second=0, microsecond=0
    ).isoformat()
    hard_constraints, governance = _governance_context(root)
    fingerprint["protected_governance"] = _governance_fingerprint(
        hard_constraints, governance
    )
    if (
        not force
        and previous.get("input_fingerprint") == fingerprint
        and previous.get("schema_version") == REGIME_HEALTH_SCHEMA_VERSION
    ):
        cached = dict(previous)
        cached["cached"] = True
        cached_policy = resolve_regime_response_policy(
            hard_constraints,
            str(cached.get("status") or "insufficient"),
            governance=governance,
        )
        # The report lives in the agent-writable job tree. It is safe to reuse
        # for alerting, but never let cached bytes authorize a mutating owner
        # policy: recompute the deterministic verdict first.
        if apply_response and cached_policy["action"] != "alert_only":
            pass
        elif apply_response:
            cached["response"] = apply_regime_response(
                store,
                job,
                cached,
                hard_constraints=hard_constraints,
                governance=governance,
            )
            return cached
        else:
            return cached

    config = _detector_config(
        hard_constraints, allow_overrides=bool(governance["trusted"])
    )
    baseline_rows = _load_backtest_forensics(root)
    forward_forensics = store.read_jsonl(
        job_id, "results/forward/trade_forensics.jsonl", limit=5_000
    )
    forward_trades = store.read_jsonl(
        job_id, "results/forward/trades.jsonl", limit=10_000
    )
    market = _read_json(root / MARKET_STATE_PATH) or {
        "available": False,
        "reason": "market-state artifact not available yet",
    }
    windows = _performance_windows(
        forward_trades,
        forward_forensics,
        baseline_rows,
        now=now,
        initial_capital=_initial_capital(job),
    )
    risk_state = _read_json(root / "state" / "risk_state.json") or {}
    signals = _health_signals(
        windows,
        market,
        risk_state=risk_state,
        config=config,
        now=now,
    )
    status, score = _verdict(signals, windows=windows, market=market)
    prior_status = str(previous.get("status") or "insufficient")
    transition = _transition(prior_status, status)
    policy = resolve_regime_response_policy(
        hard_constraints, status, governance=governance
    )

    attribution = _refresh_attribution_if_needed(
        store,
        job_id,
        root,
        status=status,
        forward_count=len(forward_forensics),
    )
    report: dict[str, Any] = {
        "schema_version": REGIME_HEALTH_SCHEMA_VERSION,
        "job_id": job_id,
        "computed_at": now.isoformat(),
        "input_fingerprint": fingerprint,
        "status": status,
        "score": score,
        "signals": signals,
        "thresholds": config,
        "windows": windows,
        "market": market,
        "risk_state": {
            key: risk_state.get(key)
            for key in (
                "equity",
                "peak_equity",
                "drawdown",
                "equity_source",
                "updated_at",
            )
            if key in risk_state
        },
        "governance": governance,
        "policy": policy,
        "transition": transition,
        "attribution": attribution,
        "cached": False,
        "_basis": (
            "Portfolio-level incumbent health. warning/critical means recent "
            "performance and/or market-state drift crossed deterministic "
            "thresholds. Attribution is refreshed before treatment design. "
            "The detector never changes strategy code; execution response is "
            "owner-governed and defaults to alert_only."
        ),
    }
    if apply_response:
        report["response"] = apply_regime_response(
            store,
            job,
            report,
            hard_constraints=hard_constraints,
            governance=governance,
        )
    else:
        report["response"] = {"action": policy["action"], "applied": False}
    store.write_json(job_id, REGIME_HEALTH_PATH, report)
    _journal_transition(store, job_id, report)
    return report


def resolve_regime_response_policy(
    hard_constraints: Mapping[str, Any],
    status: str,
    *,
    governance: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve the owner-owned response for a health status.

    Invalid or absent policy is deliberately non-mutating (``alert_only``)
    and carries a validation error in the artifact for the owner to fix.
    """
    raw = hard_constraints.get("regime_response")
    if not isinstance(raw, Mapping):
        return {"action": "alert_only", "source": "default"}
    if not governance.get("trusted"):
        return {
            "action": "alert_only",
            "source": str(governance.get("source") or "untrusted"),
            "error": (
                "automatic regime response requires a verified protected "
                "governance epoch"
            ),
        }
    requested = str(raw.get(status) or "alert_only")
    if requested not in _RESPONSE_ACTIONS:
        return {
            "action": "alert_only",
            "source": "governance",
            "error": f"unsupported regime_response action {requested!r}",
        }
    policy: dict[str, Any] = {"action": requested, "source": "governance"}
    if requested == "clamp_leverage":
        try:
            cap = float(raw["max_leverage"])
        except (KeyError, TypeError, ValueError):
            return {
                "action": "alert_only",
                "source": "governance",
                "error": "clamp_leverage requires positive max_leverage",
            }
        if not math.isfinite(cap) or cap <= 0:
            return {
                "action": "alert_only",
                "source": "governance",
                "error": "clamp_leverage requires positive max_leverage",
            }
        policy["max_leverage"] = cap
    return policy


def active_regime_leverage_cap(report: Mapping[str, Any]) -> float | None:
    """Cap from a freshly computed, owner-governed health report."""
    if str(report.get("status")) not in _ALERT_STATUSES:
        return None
    governance = report.get("governance") or {}
    policy = report.get("policy") or {}
    if not isinstance(governance, Mapping) or not governance.get("trusted"):
        return None
    if not isinstance(policy, Mapping):
        return None
    if policy.get("action") != "clamp_leverage":
        return None
    cap = _finite_float(policy.get("max_leverage"))
    return cap if cap is not None and cap > 0 else None


def apply_regime_response(
    store: JobStore,
    job: WayfinderJob,
    report: Mapping[str, Any],
    *,
    hard_constraints: Mapping[str, Any],
    governance: Mapping[str, Any],
) -> dict[str, Any]:
    status = str(report.get("status") or "insufficient")
    policy = resolve_regime_response_policy(
        hard_constraints, status, governance=governance
    )
    action = str(policy["action"])
    if status not in _ALERT_STATUSES or action == "alert_only":
        return {**policy, "applied": False}
    if action == "clamp_leverage":
        # The driver reads this governed cap before constructing the strategy.
        return {**policy, "applied": True, "effective_on": "next_tick"}

    from wayfinder_paths.jobs.halt import request_halt

    flatten = action == "flatten"
    halt = request_halt(
        store,
        job.id,
        reason=f"portfolio regime health {status}",
        flatten=flatten,
        source="regime_health",
    )
    return {
        **policy,
        "applied": True,
        "effective_on": "next_tick",
        "halt": {key: halt.get(key) for key in ("reason", "flatten", "source", "ts")},
    }


def compact_regime_health(report: Mapping[str, Any]) -> dict[str, Any]:
    """Bounded trigger/prompt view; the full report remains on disk."""
    signals = list(report.get("signals") or [])
    return {
        "status": report.get("status"),
        "score": report.get("score"),
        "computed_at": report.get("computed_at"),
        "signals": signals[:8],
        "policy": report.get("policy"),
        "transition": report.get("transition"),
        "attribution": report.get("attribution"),
        "response": report.get("response"),
    }


def _performance_windows(
    trades: Sequence[Mapping[str, Any]],
    forensics: Sequence[Mapping[str, Any]],
    baseline: Sequence[Mapping[str, Any]],
    *,
    now: dt.datetime,
    initial_capital: float,
) -> dict[str, Any]:
    baseline_bps = [
        value
        for row in baseline
        if (value := _finite_float(row.get("realized_bps"))) is not None
    ]
    baseline_mean = statistics.fmean(baseline_bps) if baseline_bps else None
    result: dict[str, Any] = {}
    for days in WINDOW_DAYS:
        cutoff = now - dt.timedelta(days=days)
        recent_trades = sorted(
            (row for row in trades if _timestamp(row) >= cutoff),
            key=_timestamp,
        )
        recent_forensics = [row for row in forensics if _timestamp(row) >= cutoff]
        pnls = [
            value for row in recent_trades if (value := _trade_pnl(row)) is not None
        ]
        bps = [
            value
            for row in recent_forensics
            if (value := _finite_float(row.get("realized_bps"))) is not None
        ]
        drawdown = _max_drawdown(pnls)
        negative = [abs(value) for value in pnls if value < 0]
        loss_by_symbol: dict[str, float] = defaultdict(float)
        for row in recent_trades:
            pnl = _trade_pnl(row)
            if pnl is not None and pnl < 0:
                loss_by_symbol[str(row.get("symbol") or "unknown")] += abs(pnl)
        loss_total = sum(negative)
        edge_percentile = _rolling_mean_percentile(baseline_bps, bps)
        observation_span_days = (
            min(
                float(days),
                max(
                    (
                        now - min(_timestamp(row) for row in recent_trades)
                    ).total_seconds()
                    / 86_400.0,
                    1.0,
                ),
            )
            if recent_trades
            else float(days)
        )
        result[str(days)] = {
            "days": days,
            "observation_span_days": round(observation_span_days, 4),
            "closed_trades": len(recent_trades),
            "forensics_trades": len(bps),
            "net_pnl": round(sum(pnls), 6),
            "max_drawdown_usd": round(drawdown, 6),
            "max_drawdown_pct": round(drawdown / initial_capital, 6),
            "drawdown_velocity_pct_per_day": round(
                drawdown / initial_capital / observation_span_days, 8
            ),
            "largest_loss_share": round(max(negative) / loss_total, 4)
            if negative
            else None,
            "largest_losing_symbol_share": round(
                max(loss_by_symbol.values()) / loss_total, 4
            )
            if loss_by_symbol and loss_total
            else None,
            "largest_losing_symbol": max(
                loss_by_symbol, key=lambda symbol: loss_by_symbol[symbol]
            )
            if loss_by_symbol
            else None,
            "forward_avg_realized_bps": round(statistics.fmean(bps), 4)
            if bps
            else None,
            "baseline_avg_realized_bps": round(baseline_mean, 4)
            if baseline_mean is not None
            else None,
            "edge_rolling_percentile": edge_percentile,
        }
    return {
        "initial_capital_basis": initial_capital,
        "baseline_forensics_trades": len(baseline_bps),
        "windows": result,
        "_basis": (
            "Closed-trade PnL drawdown is normalized by configured initial_capital; "
            "the venue-marked risk_state drawdown is evaluated separately. Edge "
            "percentile compares the recent mean realized bps with all same-length "
            "contiguous samples in the backtest forensics population."
        ),
    }


def _health_signals(
    performance: Mapping[str, Any],
    market: Mapping[str, Any],
    *,
    risk_state: Mapping[str, Any],
    config: Mapping[str, float],
    now: dt.datetime,
) -> list[dict[str, Any]]:
    signals = _performance_signals(
        performance, risk_state=risk_state, config=config
    ) + _market_drift_signals(market, now=now)
    return sorted(signals, key=lambda item: (-int(item["severity"]), item["kind"]))


def _performance_signals(
    performance: Mapping[str, Any],
    *,
    risk_state: Mapping[str, Any],
    config: Mapping[str, float],
) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    windows = performance.get("windows") or {}

    exact_value = _finite_float(risk_state.get("drawdown"))
    exact_drawdown = abs(exact_value) if exact_value is not None else 0.0
    trade_drawdown = max(
        (float(row.get("max_drawdown_pct") or 0.0) for row in windows.values()),
        default=0.0,
    )
    drawdown = max(exact_drawdown, trade_drawdown)
    if drawdown >= config["drawdown_critical"]:
        signals.append(_signal("drawdown", 2, drawdown, config["drawdown_critical"]))
    elif drawdown >= config["drawdown_warning"]:
        signals.append(_signal("drawdown", 1, drawdown, config["drawdown_warning"]))

    velocity = max(
        (
            float(row.get("drawdown_velocity_pct_per_day") or 0.0)
            for row in windows.values()
        ),
        default=0.0,
    )
    if velocity >= config["velocity_critical"]:
        signals.append(
            _signal("drawdown_velocity", 2, velocity, config["velocity_critical"])
        )
    elif velocity >= config["velocity_warning"]:
        signals.append(
            _signal("drawdown_velocity", 1, velocity, config["velocity_warning"])
        )

    eligible_edges = [
        row
        for row in windows.values()
        if int(row.get("forensics_trades") or 0) >= int(config["min_recent_trades"])
        and row.get("edge_rolling_percentile") is not None
        and float(row.get("forward_avg_realized_bps") or 0.0) < 0
        and float(row.get("baseline_avg_realized_bps") or 0.0) > 0
    ]
    if eligible_edges:
        weakest = min(eligible_edges, key=lambda row: row["edge_rolling_percentile"])
        percentile = float(weakest["edge_rolling_percentile"])
        if percentile <= config["edge_critical_percentile"]:
            signals.append(
                _signal(
                    "edge_decay",
                    2,
                    percentile,
                    config["edge_critical_percentile"],
                    window_days=weakest["days"],
                )
            )
        elif percentile <= config["edge_warning_percentile"]:
            signals.append(
                _signal(
                    "edge_decay",
                    1,
                    percentile,
                    config["edge_warning_percentile"],
                    window_days=weakest["days"],
                )
            )

    loss_rows = [
        row
        for row in windows.values()
        if int(row.get("closed_trades") or 0) >= int(config["min_recent_trades"])
        and float(row.get("net_pnl") or 0.0) < 0
    ]
    if loss_rows:
        concentrated = max(
            loss_rows,
            key=lambda row: max(
                float(row.get("largest_loss_share") or 0.0),
                float(row.get("largest_losing_symbol_share") or 0.0),
            ),
        )
        concentration = max(
            float(concentrated.get("largest_loss_share") or 0.0),
            float(concentrated.get("largest_losing_symbol_share") or 0.0),
        )
        if concentration >= config["loss_concentration_warning"]:
            signals.append(
                _signal(
                    "loss_concentration",
                    1,
                    concentration,
                    config["loss_concentration_warning"],
                    window_days=concentrated["days"],
                    symbol=concentrated.get("largest_losing_symbol"),
                )
            )
    return signals


def _market_drift_signals(
    market: Mapping[str, Any], *, now: dt.datetime
) -> list[dict[str, Any]]:
    if market.get("available"):
        as_of = _timestamp({"timestamp": market.get("as_of")})
        age_hours = max((now - as_of).total_seconds() / 3_600.0, 0.0)
        if age_hours > 6.0:
            severity = 2 if age_hours > 24.0 else 1
            return [
                _signal(
                    "market_data_stale",
                    severity,
                    age_hours,
                    24.0 if severity == 2 else 6.0,
                )
            ]

    signals: list[dict[str, Any]] = []
    market_windows = market.get("windows") if isinstance(market, Mapping) else {}
    for name, key, warning, critical, direction in (
        ("volatility_shift", "volatility_ratio", 1.67, 2.5, "outside"),
        ("correlation_shift", "correlation_delta", 0.20, 0.35, "absolute"),
        ("liquidity_deterioration", "liquidity_ratio", 0.65, 0.40, "below"),
        ("regime_mix_shift", "regime_js_divergence", 0.10, 0.20, "above"),
    ):
        candidates: list[tuple[int, float, int]] = []
        for days, row in (market_windows or {}).items():
            if not isinstance(row, Mapping) or not row.get("available"):
                continue
            raw = row.get(key)
            value = _finite_float(raw)
            if value is None:
                continue
            severity = _market_severity(value, warning, critical, direction)
            if severity:
                candidates.append((severity, value, int(days)))
        if candidates:
            severity, value, days = max(
                candidates, key=lambda item: (item[0], abs(item[1]))
            )
            threshold = critical if severity == 2 else warning
            signals.append(_signal(name, severity, value, threshold, window_days=days))

    funding_candidates: list[tuple[int, float, int, str | None]] = []
    for days, row in (market_windows or {}).items():
        if not isinstance(row, Mapping):
            continue
        funding = row.get("funding_shift")
        if not isinstance(funding, Mapping) or not _finite(funding.get("z_score")):
            continue
        value = abs(float(funding["z_score"]))
        severity = 2 if value >= 3.0 else 1 if value >= 2.0 else 0
        if severity:
            funding_candidates.append(
                (severity, value, int(days), funding.get("symbol"))
            )
    if funding_candidates:
        severity, value, days, symbol = max(funding_candidates)
        signals.append(
            _signal(
                "funding_shift",
                severity,
                value,
                3.0 if severity == 2 else 2.0,
                window_days=days,
                symbol=symbol,
            )
        )
    return signals


def _verdict(
    signals: Sequence[Mapping[str, Any]],
    *,
    windows: Mapping[str, Any],
    market: Mapping[str, Any],
) -> tuple[RegimeStatus, int]:
    if not signals:
        has_forward = any(
            int(row.get("closed_trades") or 0) > 0
            for row in (windows.get("windows") or {}).values()
        )
        return (
            "healthy" if has_forward or market.get("available") else "insufficient"
        ), 0
    performance_kinds = {
        "drawdown",
        "drawdown_velocity",
        "edge_decay",
        "loss_concentration",
    }
    performance = [s for s in signals if s["kind"] in performance_kinds]
    market_signals = [s for s in signals if s["kind"] not in performance_kinds]
    performance_score = sum(int(s["severity"]) for s in performance)
    market_score = sum(int(s["severity"]) for s in market_signals)
    score = performance_score + market_score
    critical_drawdown = any(
        s["kind"] == "drawdown" and int(s["severity"]) == 2 for s in performance
    )
    critical_performance = any(int(s["severity"]) == 2 for s in performance)
    if critical_drawdown or (critical_performance and market_score >= 1):
        return "critical", score
    if performance_score >= 2 or market_score >= 4:
        return "warning", score
    return "watch", score


def _detector_config(
    hard_constraints: Mapping[str, Any], *, allow_overrides: bool
) -> dict[str, float]:
    """Defaults plus optional OWNER overrides from protected governance.

    Alarm thresholds must not live in agent-writable job/performance config:
    the strategy being judged cannot move its own goalposts.
    """
    hard_drawdown = hard_constraints.get("max_drawdown")
    if hard_drawdown is None:
        hard_drawdown = hard_constraints.get("max_drawdown_pct")
    hard_value = _finite_float(hard_drawdown)
    hard = abs(hard_value) if hard_value is not None else 0.0
    defaults = {
        "min_recent_trades": 3.0,
        "drawdown_warning": min(0.05, hard / 3.0) if hard else 0.05,
        "drawdown_critical": min(0.10, hard * 2.0 / 3.0) if hard else 0.10,
        "velocity_warning": 0.02,
        "velocity_critical": 0.05,
        "edge_warning_percentile": 0.10,
        "edge_critical_percentile": 0.05,
        "loss_concentration_warning": 0.60,
    }
    raw = hard_constraints.get("regime_detector") if allow_overrides else None
    supplied = raw if isinstance(raw, Mapping) else {}
    for key in defaults:
        if key not in supplied:
            continue
        try:
            value = float(supplied[key])
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            defaults[key] = value
    defaults["drawdown_critical"] = max(
        defaults["drawdown_critical"], defaults["drawdown_warning"]
    )
    defaults["velocity_critical"] = max(
        defaults["velocity_critical"], defaults["velocity_warning"]
    )
    defaults["edge_critical_percentile"] = min(
        defaults["edge_critical_percentile"],
        defaults["edge_warning_percentile"],
    )
    return defaults


def _refresh_attribution_if_needed(
    store: JobStore,
    job_id: str,
    root: Path,
    *,
    status: str,
    forward_count: int,
) -> dict[str, Any]:
    if status not in _ALERT_STATUSES:
        return {"required": False, "refreshed": False}
    path = root / "results" / "research" / "attribution.json"
    existing = _read_json(path) or {}
    if int(existing.get("forward_trades") or -1) == forward_count:
        return {
            "required": True,
            "refreshed": False,
            "forward_trades": forward_count,
            "path": "results/research/attribution.json",
        }
    try:
        from wayfinder_paths.jobs.attribution import attribution_job

        result = attribution_job(job_id, store=store)
    except (ValueError, OSError) as exc:
        return {"required": True, "refreshed": False, "error": str(exc)[:240]}
    return {
        "required": True,
        "refreshed": True,
        "forward_trades": result.get("forward_trades"),
        "path": "results/research/attribution.json",
    }


def _journal_transition(
    store: JobStore, job_id: str, report: Mapping[str, Any]
) -> None:
    transition = report.get("transition") or {}
    if not transition.get("changed"):
        return
    status = str(report.get("status"))
    event_type = (
        "portfolio_regime_shift_detected"
        if status in _ALERT_STATUSES
        else "portfolio_regime_health_changed"
    )
    store.append_journal(
        job_id,
        {
            "type": event_type,
            "from_status": transition.get("from"),
            "to_status": status,
            "score": report.get("score"),
            "signals": [
                {key: signal.get(key) for key in ("kind", "severity", "value")}
                for signal in list(report.get("signals") or [])[:8]
            ],
        },
    )


def _transition(previous: str, current: str) -> dict[str, Any]:
    changed = previous != current
    return {
        "from": previous,
        "to": current,
        "changed": changed,
        "worsened": _STATUS_RANK.get(current, 0) > _STATUS_RANK.get(previous, 0),
        "alert": changed and current in _ALERT_STATUSES,
    }


def _input_fingerprint(root: Path) -> dict[str, Any]:
    paths = (
        root / "results" / "forward" / "trades.jsonl",
        root / "results" / "forward" / "trade_forensics.jsonl",
        root / "results" / "backtest" / "trade_forensics.json",
        root / MARKET_STATE_PATH,
        root / "state" / "risk_state.json",
    )
    return {
        str(path.relative_to(root)): {
            "size": path.stat().st_size,
            "mtime_ns": path.stat().st_mtime_ns,
        }
        for path in paths
        if path.exists()
    }


def _governance_context(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    from wayfinder_paths.jobs.constitution import load_constitution

    constitution = load_constitution(Path(root))
    raw_hard = constitution.get("hard_constraints")
    hard = dict(raw_hard) if isinstance(raw_hard, Mapping) else {}
    metadata = constitution.get("governance")
    chain_status = (
        str(metadata.get("chain_status")) if isinstance(metadata, Mapping) else None
    )
    source = str(constitution.get("source") or "unknown")
    return hard, {
        "source": source,
        "chain_status": chain_status,
        "revision": constitution.get("revision"),
        "trusted": source == "governance" and chain_status == "verified",
    }


def _governance_fingerprint(
    hard_constraints: Mapping[str, Any], governance: Mapping[str, Any]
) -> str:
    relevant = {
        key: hard_constraints.get(key)
        for key in (
            "max_drawdown",
            "max_drawdown_pct",
            "regime_detector",
            "regime_response",
        )
        if key in hard_constraints
    }
    return hashlib.sha256(
        json.dumps(
            {"hard_constraints": relevant, "governance": dict(governance)},
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()[:16]


def _load_backtest_forensics(root: Path) -> list[dict[str, Any]]:
    doc = _read_json(root / "results" / "backtest" / "trade_forensics.json") or {}
    rows = doc.get("trades")
    return (
        [dict(row) for row in rows if isinstance(row, Mapping)]
        if isinstance(rows, list)
        else []
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _timestamp(row: Mapping[str, Any]) -> dt.datetime:
    raw = (
        row.get("exit_ts")
        or row.get("closed_at")
        or row.get("timestamp")
        or row.get("ts")
    )
    try:
        stamp = dt.datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return dt.datetime.min.replace(tzinfo=dt.UTC)
    return (
        stamp.replace(tzinfo=dt.UTC)
        if stamp.tzinfo is None
        else stamp.astimezone(dt.UTC)
    )


def _trade_pnl(row: Mapping[str, Any]) -> float | None:
    raw = row.get("net_pnl")
    if raw is None:
        match row.get("pnl"):
            case Mapping() as payload:
                raw = (
                    payload.get("net_usd")
                    if payload.get("net_usd") is not None
                    else payload.get("net")
                )
            case other:
                raw = other
    return _finite_float(raw)


def _initial_capital(job: WayfinderJob) -> float:
    from wayfinder_paths.jobs.execution.primitives import DEFAULT_INITIAL_CAPITAL

    try:
        value = float(
            job.execution_params.get("initial_capital") or DEFAULT_INITIAL_CAPITAL
        )
    except (TypeError, ValueError):
        value = DEFAULT_INITIAL_CAPITAL
    return value if math.isfinite(value) and value > 0 else DEFAULT_INITIAL_CAPITAL


def _max_drawdown(values: Sequence[float]) -> float:
    peak = 0.0
    running = 0.0
    worst = 0.0
    for value in values:
        running += value
        peak = max(peak, running)
        worst = max(worst, peak - running)
    return worst


def _rolling_mean_percentile(
    baseline: Sequence[float], recent: Sequence[float]
) -> float | None:
    width = len(recent)
    if width < 2 or len(baseline) < width:
        return None
    observed = statistics.fmean(recent)
    samples = [
        statistics.fmean(baseline[index : index + width])
        for index in range(len(baseline) - width + 1)
    ]
    rank = sum(value <= observed for value in samples)
    return round((rank + 1) / (len(samples) + 1), 4)


def _market_severity(
    value: float, warning: float, critical: float, direction: str
) -> int:
    if direction == "absolute":
        magnitude = abs(value)
        return 2 if magnitude >= critical else 1 if magnitude >= warning else 0
    if direction == "below":
        return 2 if value <= critical else 1 if value <= warning else 0
    if direction == "outside":
        reciprocal_critical = 1.0 / critical
        reciprocal_warning = 1.0 / warning
        return (
            2
            if value >= critical or value <= reciprocal_critical
            else 1
            if value >= warning or value <= reciprocal_warning
            else 0
        )
    return 2 if value >= critical else 1 if value >= warning else 0


def _signal(
    kind: str,
    severity: int,
    value: Any,
    threshold: Any,
    **context: Any,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "severity": severity,
        "value": round(float(value), 6) if _finite(value) else value,
        "threshold": threshold,
        **{key: item for key, item in context.items() if item is not None},
    }


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _finite_float(value: Any) -> float | None:
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    return converted if math.isfinite(converted) else None
