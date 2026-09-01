"""Compact deterministic evidence for the evolution design/repair loop.

This module deliberately aggregates artifacts and simulator receipts; it does
not create a second research ontology.  Evidence citations are JSON pointers
into one immutable campaign pack, and postmortems are exact same-window diffs.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from wayfinder_paths.jobs.execution.simulator import ExecutionBacktestResult

DIAGNOSTIC_PACK_MAX_BYTES = 24_000
RESULT_STAT_KEYS = (
    "net_return",
    "trade_count",
    "sharpe",
    "sortino",
    "max_drawdown_pct",
    "win_rate",
    "profit_factor",
    "avg_trade_pnl",
    "exposure_pct",
    "peak_notional_usd",
    "total_fees",
    "total_turnover_usd",
)


def result_receipt(
    result: ExecutionBacktestResult,
    *,
    revision: str,
    objective: Mapping[str, Any] | None = None,
    behavior: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist only fields needed to reproduce a behavioral comparison."""
    return {
        "revision": revision,
        "execution_valid": bool(result.validation.get("execution_valid")),
        "stats": {
            key: result.stats.get(key)
            for key in RESULT_STAT_KEYS
            if result.stats.get(key) is not None
        },
        "objective": dict(objective or {}),
        "behavior": dict(behavior or {}),
        "trades": [_trade_view(row) for row in result.trades],
    }


def build_diagnostic_pack(
    root: Path,
    *,
    campaign_id: str,
    created_at: str,
    baseline: Mapping[str, Any],
    historical_lessons: Mapping[str, Any],
    research_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze compact existing diagnostics with explicit plane/provenance."""
    artifacts: dict[str, Any] = {}
    attribution = _read_json(root / "results/research/attribution.json")
    if attribution:
        deltas = [
            row
            for row in attribution.get("expectation_deltas") or []
            if isinstance(row, dict) and not row.get("small_n")
        ]
        artifacts["attribution"] = {
            "plane": "historical_and_forward",
            "forward_trades": attribution.get("forward_trades"),
            "archetypes_forward": (attribution.get("forward") or {}).get("archetype")
            or {},
            "top_expectation_deltas": deltas[:6],
            "source": _provenance(root, "results/research/attribution.json"),
        }
    forensics = _read_json(root / "results/backtest/trade_forensics.json")
    if forensics.get("aggregate"):
        artifacts["trade_forensics"] = {
            "plane": "historical",
            "aggregate": forensics["aggregate"],
            "source": _provenance(root, "results/backtest/trade_forensics.json"),
        }
    regime = _read_json(root / "results/research/regime_health.json")
    if regime:
        from wayfinder_paths.jobs.regime_health import compact_regime_health

        artifacts["regime_health"] = {
            "plane": "live_and_paper",
            "report": compact_regime_health(regime),
            "source": _provenance(root, "results/research/regime_health.json"),
        }
    counterfactual = _read_json(root / "results/forward/counterfactual.json")
    if counterfactual:
        keep = (
            "available",
            "proposal_id",
            "window",
            "delta_net_pnl",
            "effects",
            "by_symbol",
            "entries_skipped_by_change",
            "entries_added_by_change",
        )
        artifacts["counterfactual"] = {
            "plane": "forward_paper_or_live",
            "report": {
                key: counterfactual[key] for key in keep if key in counterfactual
            },
            "source": _provenance(root, "results/forward/counterfactual.json"),
        }
    forward_summary = _read_json(root / "results/forward/summary.json")
    if forward_summary:
        artifacts["forward_summary"] = {
            "plane": "forward_paper_or_live",
            "report": _compact_forward_summary(forward_summary),
            "source": _provenance(root, "results/forward/summary.json"),
        }
    pack = {
        "schema_version": "1.0",
        "campaign_id": campaign_id,
        "created_at": created_at,
        "citation_contract": (
            "Grounded hypotheses cite JSON pointers into this document. "
            "Missing sections are unavailable, never inferred."
        ),
        "baseline": {"plane": "historical", **dict(baseline)},
        **artifacts,
        "prior_campaign_lessons": dict(historical_lessons),
        "research_context": dict(research_context),
    }
    return _fit_pack(pack)


def resolve_json_pointer(document: Mapping[str, Any], pointer: str) -> Any:
    if not pointer.startswith("/"):
        raise ValueError("evidence reference must be a JSON pointer")
    current: Any = document
    for raw in pointer[1:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            try:
                current = current[int(key)]
            except (IndexError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"evidence reference does not exist: {pointer}"
                ) from exc
        elif isinstance(current, Mapping) and key in current:
            current = current[key]
        else:
            raise ValueError(f"evidence reference does not exist: {pointer}")
    if isinstance(current, (dict, list)) and not current:
        raise ValueError(f"evidence reference is empty: {pointer}")
    if current is None or current == "":
        raise ValueError(f"evidence reference is empty: {pointer}")
    return current


def build_postmortem(
    candidate: Mapping[str, Any],
    reference: Mapping[str, Any],
    *,
    previous: Mapping[str, Any] | None = None,
    min_trades: int = 8,
) -> dict[str, Any]:
    """Explain what changed and why an attempt did or did not qualify."""
    candidate_trades = list(candidate.get("trades") or [])
    reference_trades = list(reference.get("trades") or [])
    candidate_signatures = {_trade_signature(row) for row in candidate_trades}
    reference_signatures = {_trade_signature(row) for row in reference_trades}
    added = candidate_signatures - reference_signatures
    removed = reference_signatures - candidate_signatures
    stats = candidate.get("stats") or {}
    ref_stats = reference.get("stats") or {}
    trade_count = _integer(stats.get("trade_count"))
    net_return = _number(stats.get("net_return"))
    ref_count = _integer(ref_stats.get("trade_count"))
    ref_net = _number(ref_stats.get("net_return"))
    fee_delta = _number(stats.get("total_fees")) - _number(ref_stats.get("total_fees"))
    turnover_delta = _number(stats.get("total_turnover_usd")) - _number(
        ref_stats.get("total_turnover_usd")
    )
    exposure_delta = _number(stats.get("exposure_pct")) - _number(
        ref_stats.get("exposure_pct")
    )
    material_change = bool(added or removed) or any(
        abs(value) > 1e-12 for value in (fee_delta, turnover_delta, exposure_delta)
    )
    failure_codes: list[str] = []
    if not candidate.get("execution_valid"):
        failure_codes.append("invalid_execution")
    if not material_change:
        failure_codes.append("no_behavior_change")
    if trade_count <= 0:
        failure_codes.append("no_trades")
    elif trade_count < min_trades:
        failure_codes.append("activity_below_floor")
    if net_return <= 0:
        failure_codes.append("negative_after_costs")
    if ref_count and trade_count < max(1, int(ref_count * 0.5)):
        failure_codes.append("activity_collapse")
    realized = sum(_number(row.get("realized_pnl_delta")) for row in candidate_trades)
    fees = _number(stats.get("total_fees"))
    if realized > 0 and net_return <= 0 and fees > 0:
        failure_codes.append("fees_erased_edge")
    viable = bool(
        candidate.get("execution_valid")
        and material_change
        and trade_count >= min_trades
        and net_return > 0
    )
    postmortem: dict[str, Any] = {
        "viable": viable,
        "primary_failure": failure_codes[0] if failure_codes else None,
        "failure_codes": failure_codes,
        "behavior_diff": {
            "decisions_added": len(added),
            "decisions_removed": len(removed),
            "decisions_unchanged": len(candidate_signatures & reference_signatures),
            "material_change": material_change,
            "trade_count_delta": trade_count - ref_count,
            "exposure_pct_delta": round(exposure_delta, 8),
            "turnover_usd_delta": round(turnover_delta, 6),
            "fee_delta": round(fee_delta, 6),
            "net_return_delta": round(net_return - ref_net, 8),
            "by_symbol": _bucket_delta(candidate_trades, reference_trades, "symbol"),
            "by_side": _bucket_delta(candidate_trades, reference_trades, "side"),
        },
    }
    if previous:
        postmortem["progress_from_previous"] = _progress(candidate, previous)
    return postmortem


def attempt_made_progress(postmortem: Mapping[str, Any]) -> bool:
    progress = postmortem.get("progress_from_previous") or {}
    return bool(
        progress.get("became_valid")
        or progress.get("became_viable")
        or _number(progress.get("trade_count_delta")) > 0
        or _number(progress.get("net_return_delta")) > 1e-9
        or _number(progress.get("objective_delta")) > 1e-9
    )


def compact_postmortem(postmortem: Mapping[str, Any]) -> dict[str, Any]:
    behavior = postmortem.get("behavior_diff") or {}
    return {
        "viable": bool(postmortem.get("viable")),
        "primary_failure": postmortem.get("primary_failure"),
        "failure_codes": list(postmortem.get("failure_codes") or [])[:6],
        "behavior_diff": {
            key: behavior.get(key)
            for key in (
                "decisions_added",
                "decisions_removed",
                "material_change",
                "trade_count_delta",
                "exposure_pct_delta",
                "turnover_usd_delta",
                "fee_delta",
                "net_return_delta",
            )
            if behavior.get(key) is not None
        },
    }


def participation_adjusted_score(
    raw_score: float, *, trade_count: int, target_trades: int = 12
) -> float:
    if target_trades <= 0:
        return raw_score
    participation = min(1.0, max(0.0, trade_count / target_trades))
    if participation <= 0:
        return float("-inf")
    return raw_score * participation if raw_score >= 0 else raw_score / participation


def _progress(
    candidate: Mapping[str, Any], previous: Mapping[str, Any]
) -> dict[str, Any]:
    stats = candidate.get("stats") or {}
    prior = previous.get("stats") or {}
    objective = candidate.get("objective") or {}
    prior_objective = previous.get("objective") or {}
    return {
        "became_valid": bool(candidate.get("execution_valid"))
        and not bool(previous.get("execution_valid")),
        "became_viable": _receipt_viable(candidate) and not _receipt_viable(previous),
        "trade_count_delta": _integer(stats.get("trade_count"))
        - _integer(prior.get("trade_count")),
        "net_return_delta": round(
            _number(stats.get("net_return")) - _number(prior.get("net_return")), 8
        ),
        "objective_delta": round(
            _objective_score(objective) - _objective_score(prior_objective), 8
        ),
    }


def _receipt_viable(receipt: Mapping[str, Any]) -> bool:
    stats = receipt.get("stats") or {}
    return bool(
        receipt.get("execution_valid")
        and _integer(stats.get("trade_count")) >= 1
        and _number(stats.get("net_return")) > 0
    )


def _objective_score(objective: Mapping[str, Any]) -> float:
    return (
        _number(objective.get("net_log_growth"))
        - _number(objective.get("downside_deviation"))
        - _number(objective.get("tail_loss"))
        - _number(objective.get("max_drawdown_pct"))
    )


def _trade_view(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("raw") or {}
    metadata = raw.get("intent_metadata") or raw.get("metadata") or raw
    return {
        "timestamp": row.get("timestamp"),
        "symbol": row.get("symbol"),
        "side": row.get("side"),
        "filled_size": row.get("filled_size"),
        "avg_price": row.get("avg_price"),
        "fee": row.get("fee"),
        "realized_pnl_delta": row.get("realized_pnl_delta"),
        "reduce_only": bool(row.get("reduce_only")),
        "action": raw.get("intent_action") or metadata.get("action"),
    }


def _trade_signature(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(row.get("timestamp") or ""),
        str(row.get("symbol") or ""),
        str(row.get("side") or ""),
        bool(row.get("reduce_only")),
        str(row.get("action") or ""),
        round(_number(row.get("filled_size")), 10),
    )


def _bucket_delta(
    candidate: Sequence[Mapping[str, Any]],
    reference: Sequence[Mapping[str, Any]],
    key: str,
) -> dict[str, Any]:
    def aggregate(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
        buckets: defaultdict[str, dict[str, float]] = defaultdict(
            lambda: {"fills": 0.0, "pnl": 0.0, "fees": 0.0}
        )
        for row in rows:
            bucket = buckets[str(row.get(key) or "?")]
            bucket["fills"] += 1
            bucket["pnl"] += _number(row.get("realized_pnl_delta"))
            bucket["fees"] += _number(row.get("fee"))
        return dict(buckets)

    left, right = aggregate(candidate), aggregate(reference)
    output: dict[str, Any] = {}
    for name in sorted(set(left) | set(right)):
        output[name] = {
            field: round(
                left.get(name, {}).get(field, 0.0)
                - right.get(name, {}).get(field, 0.0),
                6,
            )
            for field in ("fills", "pnl", "fees")
        }
    return output


def _compact_forward_summary(doc: Mapping[str, Any]) -> dict[str, Any]:
    keep = (
        "mode",
        "status",
        "started_at",
        "last_tick_at",
        "trade_count",
        "net_pnl",
        "strategy_net_pnl",
        "operational_net_pnl",
        "total_fees",
        "by_symbol",
    )
    return {key: doc[key] for key in keep if key in doc}


def _provenance(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    try:
        payload = path.read_bytes()
        return {
            "path": relative,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        }
    except OSError:
        return {"path": relative, "unavailable": True}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _fit_pack(pack: dict[str, Any]) -> dict[str, Any]:
    def size() -> int:
        return len(json.dumps(pack, default=str).encode())

    if size() <= DIAGNOSTIC_PACK_MAX_BYTES:
        return pack
    # Preserve the current baseline and load-bearing research lists first.
    for key in (
        "trade_forensics",
        "forward_summary",
        "counterfactual",
        "regime_health",
    ):
        pack.pop(key, None)
        if size() <= DIAGNOSTIC_PACK_MAX_BYTES:
            break
    attribution = pack.get("attribution")
    if isinstance(attribution, dict) and size() > DIAGNOSTIC_PACK_MAX_BYTES:
        attribution["top_expectation_deltas"] = list(
            attribution.get("top_expectation_deltas") or []
        )[:3]
        archetypes = attribution.get("archetypes_forward")
        if isinstance(archetypes, dict):
            attribution["archetypes_forward"] = {
                key: archetypes[key] for key in sorted(archetypes)[:8]
            }
    for key in ("attribution", "prior_campaign_lessons", "research_context"):
        if size() <= DIAGNOSTIC_PACK_MAX_BYTES:
            break
        value = pack.pop(key, None)
        pack[f"{key}_truncated"] = {
            "reason": "diagnostic_pack_byte_budget",
            "sha256": hashlib.sha256(
                json.dumps(value, default=str, sort_keys=True).encode()
            ).hexdigest(),
        }
    if size() > DIAGNOSTIC_PACK_MAX_BYTES:
        baseline = pack.get("baseline") or {}
        pack["baseline"] = {
            key: baseline[key]
            for key in (
                "plane",
                "available",
                "reason",
                "stats",
                "objective",
                "behavior",
                "window_bars",
            )
            if key in baseline
        }
    if size() > DIAGNOSTIC_PACK_MAX_BYTES:
        # Controlled receipts above are normally far below the cap. Keep a
        # final fail-closed shape so a malformed oversized artifact can never
        # bloat the one design turn or the opencode heap.
        pack = {
            "schema_version": pack.get("schema_version"),
            "campaign_id": pack.get("campaign_id"),
            "created_at": pack.get("created_at"),
            "citation_contract": pack.get("citation_contract"),
            "baseline": pack.get("baseline"),
            "pack_truncated": True,
        }
    pack["truncated_to_bytes"] = DIAGNOSTIC_PACK_MAX_BYTES
    return pack


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
