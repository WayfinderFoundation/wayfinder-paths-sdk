from __future__ import annotations

import ast
import asyncio
import io
import json
import math
import py_compile
import re
import tokenize
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from croniter import croniter

from wayfinder_paths.jobs.execution.features import (
    apply_precompute,
    load_feature_rows,
    parse_feature_specs,
)
from wayfinder_paths.jobs.execution.primitives import (
    BAR_CLOSE_LABEL,
    REDUCE_ONLY_ACTIONS,
    CompletedBarsView,
    ExecutionSpec,
    ExecutionTrace,
    StateSnapshot,
    _load_module_from_path,
    bar_interval_seconds,
    resolve_compute_window,
)
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.runner.schedule import normalize_schedule

FORBIDDEN_ORDER_PATTERNS = (
    "hyperliquid_place_",
    "polymarket_place_",
    ".place_market_order(",
    ".place_limit_order(",
    ".place_trigger_order(",
    ".place_stop_loss(",
)
RAW_CANDLE_PATTERNS = ("ccxt", "fetch_ohlcv", "get_candles(")
MANUAL_STATE_CLEAR_PATTERNS = (
    "in_position = False",
    '"in_position": False',
    "'in_position': False",
    "position = None",
)


def validate_execution_trace(
    trace: Mapping[str, Any],
    execution_spec: ExecutionSpec | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    spec = ExecutionSpec.coerce(execution_spec or trace["execution_spec"])
    issues: list[str] = []
    warnings: list[str] = []
    critical_failures: list[str] = []

    runs = trace["runs"]
    if runs and all("visible_latest_timestamp" in item for item in runs):
        replay_times = [_trace_timestamp(item.get("timestamp")) for item in runs]
        visible_times = [
            _trace_timestamp(item.get("visible_latest_timestamp")) for item in runs
        ]
        parsed_replay_times = [value for value in replay_times if value is not None]
        parsed_visible_times = [value for value in visible_times if value is not None]
        timestamps_parse = len(parsed_replay_times) == len(runs) and len(
            parsed_visible_times
        ) == len(runs)
        replay_monotonic = timestamps_parse and parsed_replay_times == sorted(
            parsed_replay_times
        )
        visible_monotonic = timestamps_parse and parsed_visible_times == sorted(
            parsed_visible_times
        )
        visible_not_future = timestamps_parse and all(
            visible <= replay
            for visible, replay in zip(
                parsed_visible_times, parsed_replay_times, strict=True
            )
        )
        no_lookahead = bool(
            timestamps_parse
            and replay_monotonic
            and visible_monotonic
            and visible_not_future
        )
    else:
        # Backward compatibility for traces recorded before the causal
        # timestamp marker existed. Counts are valid for growing views, but
        # not sufficient for new bounded windows containing sparse symbols.
        visible_counts = [item["visible_bar_count"] for item in runs]
        no_lookahead = visible_counts == sorted(visible_counts)
    if not no_lookahead:
        critical_failures.append(
            "visible market data moved backward or leaked future bars"
        )

    bracket_events = trace["bracket_events"]
    ohlc_correct = all(item["used_ohlc"] for item in bracket_events if item["hit"])
    if not ohlc_correct:
        critical_failures.append("bracket event missing OHLC high/low evaluation")
    if spec.ohlc_rules["use_high_low_for_stops"] and not bracket_events:
        warnings.append(
            "no bracket events recorded; stop/TP behavior was not exercised"
        )

    hidden_success = [
        fill
        for fill in trace["fills"]
        if fill["status"] not in {"filled", "partial"} and not fill["error"]
    ]
    if hidden_success:
        issues.append("non-filled order statuses must not be reported as success")

    guard_events = trace.get("guard_events") or []
    stale_timestamps = {
        event["timestamp"] for event in guard_events if event["kind"] == "stale_data"
    }
    stale_entries = [
        fill
        for fill in trace["fills"]
        if fill["timestamp"] in stale_timestamps
        and fill["status"] in {"filled", "partial"}
        and not fill["reduce_only"]
    ]
    state_valid = not stale_entries
    if stale_entries:
        issues.append("position-opening fills executed against stale market data")

    rejected = [event for event in guard_events if event["kind"] == "intent_rejected"]
    capacity_valid = not rejected
    if rejected:
        warnings.append(
            f"{len(rejected)} intent(s) rejected by capability/limit guards"
        )

    execution_valid = not critical_failures and not issues
    return {
        "execution_valid": execution_valid,
        "data_valid": no_lookahead,
        "state_valid": state_valid,
        "capacity_valid": capacity_valid,
        "issues": issues,
        "warnings": warnings,
        "critical_failures": critical_failures,
        "auto_fix_suggestions": _suggestions(issues + critical_failures + warnings),
    }


def _trace_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def validate_execution_job(
    job_id: str,
    *,
    strict: bool = False,
    candidate_dir: str | Path | None = None,
    store: JobStore | None = None,
) -> dict[str, Any]:
    store = store or JobStore()
    root = Path(candidate_dir) if candidate_dir else store.job_dir(job_id)
    job_yaml_path = root / "job.yaml"
    checks: list[dict[str, Any]] = [
        {
            "name": "job_yaml_exists",
            "passed": job_yaml_path.exists(),
            "path": str(job_yaml_path),
        }
    ]
    job_data: dict[str, Any] = {}
    if job_yaml_path.exists():
        try:
            loaded = yaml.safe_load(job_yaml_path.read_text(encoding="utf-8")) or {}
            match loaded:
                case dict():
                    job_data = loaded
                    yaml_ok = True
                case _:
                    yaml_ok = False
            checks.append({"name": "job_yaml_parse", "passed": yaml_ok})
        except Exception as exc:
            checks.append(
                {"name": "job_yaml_parse", "passed": False, "error": str(exc)}
            )

    spec_data, spec_path = resolve_execution_spec(root, job_data)
    has_spec = bool(spec_data)
    checks.append(
        {
            "name": "execution_spec_present",
            "passed": has_spec,
            "path": str(spec_path) if spec_path else None,
            "blocking": bool(strict),
        }
    )
    if not has_spec:
        report = _report(checks, strict=strict)
        if not candidate_dir:
            store.write_json(job_id, "reports/validation/latest.json", report)
        return report

    spec = ExecutionSpec.from_dict(spec_data)
    checks.extend(_execution_spec_checks(spec))
    checks.extend(_timing_checks(job_data, spec))
    checks.extend(_dataset_timestamp_checks(root, job_data))
    checks.extend(_feature_checks(root, spec))
    script_path = store.resolve_script_entrypoint(
        job_id,
        job_data,
        candidate_dir=root if candidate_dir else None,
    )
    checks.append(
        {
            "name": "execution_script_exists",
            "passed": bool(script_path and script_path.exists()),
            "path": str(script_path) if script_path else None,
        }
    )
    checks.append(entrypoint_inside_workspace_check(root, script_path))
    if script_path and script_path.exists():
        checks.extend(_script_static_checks(script_path, spec))
        try:
            module = _load_module_from_path(script_path)
            checks.append({"name": "strategy_module_loads", "passed": True})
            checks.append(
                {
                    "name": "strategy_entrypoint_present",
                    "passed": callable(getattr(module, "build_strategy", None))
                    or callable(getattr(module, "decide", None)),
                }
            )
        except Exception as exc:
            checks.append(
                {"name": "strategy_module_loads", "passed": False, "error": str(exc)}
            )
        checks.extend(_execution_scenario_checks(script_path, job_data, spec))
        checks.extend(_window_invariance_checks(root, script_path, job_data, spec))

    trace_report = _latest_trace_validation(root, spec)
    if trace_report is not None:
        checks.append(
            {
                "name": "latest_trace_valid",
                "passed": bool(trace_report["execution_valid"]),
                "details": trace_report,
            }
        )

    checks.extend(_preflight_checks(root, job_data, spec))
    checks.extend(_evidence_window_check(root))
    checks.extend(_live_wallet_checks(job_data))

    report = _report(checks, strict=strict or spec.strict)
    report["revision"] = compute_workspace_revision(root)
    if not candidate_dir:
        store.write_json(job_id, "reports/validation/latest.json", report)
    return report


def _dataset_timestamp_checks(
    root: Path, job_data: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Require an auditable close-time label before a jobs_v1 dataset gates.

    Timestamp values alone cannot distinguish an opening label from a closing
    label when both land on the interval grid. The persisted provenance is the
    only reliable boundary contract, so hand-built datasets must declare it.
    Pre-contract ``live_fetch`` files are safe to grandfather because that
    source has always gone through ``MarketDataFeed.get_completed_bars``.
    """
    paths = (
        root / "results" / "backtest" / "input_bars.json",
        root / "workspace" / "config" / "backtest_bars.json",
    )
    path = next((candidate for candidate in paths if candidate.exists()), None)
    if path is None:
        return []  # fixture/scenario-driven validation contexts

    blocking = str(job_data.get("execution_contract") or "") == "jobs_v1"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [
            {
                "name": "dataset_close_time_labels",
                "passed": False,
                "blocking": blocking,
                "path": str(path),
                "error": str(exc),
            }
        ]

    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    metadata = metadata if isinstance(metadata, dict) else {}
    convention = metadata.get("label_convention")
    source = str(metadata.get("source") or "")
    legacy_live_fetch = convention is None and source == "live_fetch"
    passed = convention == BAR_CLOSE_LABEL or legacy_live_fetch
    check: dict[str, Any] = {
        "name": "dataset_close_time_labels",
        "passed": passed,
        "blocking": blocking,
        "path": str(path),
        "label_convention": convention,
    }
    if legacy_live_fetch:
        check["note"] = (
            "legacy live_fetch provenance accepted: this source has always "
            "used completed-bar venue feeds; the next refresh will stamp "
            f"label_convention={BAR_CLOSE_LABEL!r}"
        )
    elif not passed:
        check["hint"] = (
            f"wrap bars in {{'bars': [...], 'metadata': "
            f"{{'label_convention': {BAR_CLOSE_LABEL!r}}}}} or rebuild with "
            "wayfinder job fetch-dataset"
        )
    return [check]


# Evidence-window policy (owner-set 2026-07-27): a 5m strategy validated on
# a 14-day window backtested +21% at deploy and ran -41bps/trade forward —
# window-local noise survived every multiplicity gate because none of them
# question the window itself. Force long history when it exists; the 30d
# floor applies ONLY with proof of unavailability (the full target was
# requested and the source could not supply it — e.g. a new listing).
EVIDENCE_TARGET_DAYS = 120.0
EVIDENCE_FLOOR_DAYS = 30.0


def _evidence_window_check(root: Path) -> list[dict[str, Any]]:
    bars_path = root / "results" / "backtest" / "input_bars.json"
    if not bars_path.exists():
        return []  # fixture/scenario-driven validation contexts
    try:
        payload = json.loads(bars_path.read_text(encoding="utf-8"))
    except ValueError:
        payload = None
    # Legacy bars files are a bare list of rows with no metadata envelope.
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if not isinstance(metadata, dict) or metadata.get("days") is None:
        return [
            {
                "name": "evidence_window",
                "passed": True,
                "blocking": False,
                "note": (
                    "dataset provenance unknown (hand-written bars) — window "
                    "policy not evaluable; prefer fetch-dataset so the window "
                    "is auditable"
                ),
            }
        ]
    requested = float(metadata.get("days") or 0.0)
    received = float(metadata.get("days_received") or requested)
    if received >= 0.9 * EVIDENCE_TARGET_DAYS:
        return [
            {
                "name": "evidence_window",
                "passed": True,
                "tier": "long_history",
                "days_received": received,
            }
        ]
    if requested < EVIDENCE_TARGET_DAYS:
        # A short window is only excusable with PROOF the data does not
        # exist — and proof requires having asked for the full target.
        return [
            {
                "name": "evidence_window",
                "passed": False,
                "days_received": received,
                "error": (
                    f"dataset spans {received:g}d but only {requested:g}d was "
                    f"requested — request the full target first: fetch-dataset "
                    f"--days {EVIDENCE_TARGET_DAYS:g} --source ccxt. The "
                    f"{EVIDENCE_FLOOR_DAYS:g}d floor applies only when the "
                    "full target was requested and the source could not "
                    "supply it (new listing)."
                ),
            }
        ]
    source = str(metadata.get("source") or "")
    missing = metadata.get("ccxt_missing_markets")
    if source != "ccxt" and isinstance(missing, list) and missing:
        # The long-history source does not list these symbols at all (probed
        # at fetch time) — a venue dataset is the only obtainable evidence.
        if received >= EVIDENCE_FLOOR_DAYS:
            return [
                {
                    "name": "evidence_window",
                    "passed": True,
                    "tier": "short_history_proven",
                    "days_received": received,
                    "note": (
                        f"{received:g}d from the venue; symbols {missing} "
                        "have no market on the long-history exchange (probed "
                        "at fetch) — venue data is the only obtainable "
                        "evidence. 30d floor applies; short-history caveats "
                        "stand."
                    ),
                }
            ]
        return [
            {
                "name": "evidence_window",
                "passed": False,
                "days_received": received,
                "error": (
                    f"only {received:g}d of venue history and symbols "
                    f"{missing} have no long-history market — below the "
                    f"{EVIDENCE_FLOOR_DAYS:g}d floor; too new to validate."
                ),
            }
        ]
    if source != "ccxt":
        # A VENUE shortfall proves nothing — venue feeds cap at days of
        # history while the ccxt path has years. This exact hole let an
        # Aug 2 default-source refetch replace the 120d ccxt dataset with
        # 40d of venue data and still pass the gate as "proven".
        return [
            {
                "name": "evidence_window",
                "passed": False,
                "days_received": received,
                "error": (
                    f"dataset spans {received:g}d from source {source!r} — a "
                    "venue-capped shortfall is NOT proof of unavailability. "
                    f"Refetch via the long-history path: fetch-dataset --days "
                    f"{EVIDENCE_TARGET_DAYS:g} --source ccxt. Only a ccxt "
                    "shortfall counts as proven (new listing)."
                ),
            }
        ]
    if received >= EVIDENCE_FLOOR_DAYS:
        return [
            {
                "name": "evidence_window",
                "passed": True,
                "tier": "short_history_proven",
                "days_received": received,
                "note": (
                    f"{received:g}d received of {requested:g}d requested — the "
                    "long-history source could not supply the target (new "
                    "symbol); the 30d floor applies. Evidence from this window "
                    "is short-history: prefer probation sizing and re-validate "
                    "as history grows."
                ),
            }
        ]
    return [
        {
            "name": "evidence_window",
            "passed": False,
            "days_received": received,
            "error": (
                f"only {received:g}d of history exists (full target "
                "requested) — below the "
                f"{EVIDENCE_FLOOR_DAYS:g}d floor; too new to validate any "
                "deployment evidence"
            ),
        }
    ]


# Window-invariance probe: a mechanical backtest ≡ forward-input proof for
# declared compute windows. Bars beyond the declared window must be invisible
# to decide(), so replaying a bar with W and with W+extra bars from identical
# fresh state must produce identical intents.
WINDOW_PROBE_SAMPLES = 12
WINDOW_PROBE_EXTRA_BARS = 64
BOUNDED_WINDOW_HINT = (
    "live decide() sees at most the declared window of completed bars; any "
    "logic that consumes more history than warmup_bars is testing a strategy "
    "that cannot exist in production. Declare warmup_bars covering your "
    "longest indicator lookback plus buffer, compute within that window, and "
    "carry long memory as incremental state."
)


# Relative tolerance between the two replays' numeric fields. An exponential
# average (Wilder ATR, EMA) seeded 64 bars earlier moves a stop or a size by
# a few basis points with no real dependence beyond the window; a decide()
# that reads beyond its window moves entries, sides or levels by far more.
# Ten basis points sits well under one side of execution cost, so a passing
# difference cannot change a fill's economics. (1e-6 rejected two genuine
# candidates on a 2.6 bps ATR-stop gap and took a whole benchmark loop with
# them.)
WINDOW_PROBE_REL_TOL = 1e-3


def _probe_values_match(left: Any, right: Any) -> bool:
    """Structural equality with the economic float tolerance above."""
    match left, right:
        case dict(), dict():
            return left.keys() == right.keys() and all(
                _probe_values_match(left[key], right[key]) for key in left
            )
        case ((list() | tuple()), (list() | tuple())):
            return len(left) == len(right) and all(
                _probe_values_match(a, b) for a, b in zip(left, right, strict=True)
            )
        case bool(), _:
            return left == right
        case ((int() | float()), (int() | float())):
            return math.isclose(
                float(left), float(right), rel_tol=WINDOW_PROBE_REL_TOL, abs_tol=1e-9
            )
        case _:
            return left == right


def probe_mismatches(base: Any, wide: Any, path: str = "") -> list[dict[str, Any]]:
    """The fields that differ between the two replays, with their relative
    gap, so a rejection names the level or size that moved, not a slogan."""
    rows: list[dict[str, Any]] = []
    match base, wide:
        case dict(), dict():
            for key in sorted(set(base) | set(wide)):
                rows.extend(
                    probe_mismatches(base.get(key), wide.get(key), f"{path}.{key}")
                )
        case ((list() | tuple()), (list() | tuple())):
            if len(base) != len(wide):
                rows.append(
                    {
                        "path": path or "/",
                        "base": len(base),
                        "wide": len(wide),
                        "kind": "count",
                    }
                )
            for index, (a, b) in enumerate(zip(base, wide, strict=False)):
                rows.extend(probe_mismatches(a, b, f"{path}[{index}]"))
        case _:
            if not _probe_values_match(base, wide):
                row: dict[str, Any] = {
                    "path": path.lstrip("."),
                    "base": base,
                    "wide": wide,
                }
                if isinstance(base, (int, float)) and isinstance(wide, (int, float)):
                    denominator = max(abs(float(wide)), 1e-12)
                    row["rel"] = round(abs(float(base) - float(wide)) / denominator, 8)
                rows.append(row)
    return rows


def _probe_indices(timestamps: Any, *, first: int, samples: int) -> list[int]:
    span = len(timestamps) - 1 - first
    count = max(1, min(samples, span + 1))
    step = span / max(count - 1, 1)
    return sorted({first + round(index * step) for index in range(count)})


async def _probe_decided_intents(
    script_entrypoint: str | Path | Callable[..., Any],
    bars: CompletedBarsView,
    spec: ExecutionSpec,
    params: Mapping[str, Any],
    *,
    index: int,
    lookback: int,
) -> list[dict[str, Any]]:
    from wayfinder_paths.jobs.execution.engine import (  # circular import
        EngineState,
        run_tick,
    )
    from wayfinder_paths.jobs.execution.simulator import (  # circular import
        BacktestBroker,
        _load_strategy,
    )

    params_data = dict(params)
    strategy = _load_strategy(script_entrypoint, dict(params_data))
    view = apply_precompute(strategy, bars.window(index, lookback))
    trace = ExecutionTrace(execution_spec=spec.to_dict())
    await run_tick(
        strategy,
        view=view,
        brokers={"*": BacktestBroker()},
        state=EngineState(),
        spec=spec,
        params=params_data,
        timestamp=bars.timestamps[index],
        snapshot=StateSnapshot(status="valid"),
        trace=trace,
    )
    return trace.intents


_MATERIAL_INTENT_KEYS = (
    "action",
    "venue",
    "symbol",
    "side",
    "size",
    "notional",
    "reduce_only",
    "bracket",
    "limit_price",
    "time_in_force",
    "expires_after_bars",
)


def _material_intents(intents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: intent.get(key) for key in _MATERIAL_INTENT_KEYS} for intent in intents
    ]


def parameter_behavior_probe(
    script_entrypoint: str | Path | Callable[..., Any],
    bars: CompletedBarsView,
    execution_spec: ExecutionSpec | Mapping[str, Any] | None,
    params: Mapping[str, Any],
    variants: Sequence[Mapping[str, Any]],
    *,
    samples: int = 8,
) -> dict[str, Any]:
    """Check sampled material intents before paying for a full simulation."""
    from wayfinder_paths.jobs.execution.simulator import _load_strategy

    params_data = dict(params)
    spec = ExecutionSpec.coerce(execution_spec)
    window = resolve_compute_window(
        params_data, _load_strategy(script_entrypoint, dict(params_data))
    )
    if not window.declared or window.size is None:
        return {
            "status": "skipped",
            "reason": f"compute window is {window.source}, not declared",
        }
    if not variants:
        return {"status": "skipped", "reason": "no parameter variants"}
    timestamps = bars.timestamps
    window_size = window.size
    first = window_size - 1
    if len(timestamps) - 1 < first:
        return {
            "status": "skipped",
            "reason": "dataset does not cover the declared window",
        }
    indices = _probe_indices(timestamps, first=first, samples=samples)

    async def probe() -> dict[str, Any]:
        ticks = 0
        for index in indices:
            baseline = _material_intents(
                await _probe_decided_intents(
                    script_entrypoint,
                    bars,
                    spec,
                    params_data,
                    index=index,
                    lookback=window_size,
                )
            )
            ticks += 1
            for variant in variants:
                candidate = _material_intents(
                    await _probe_decided_intents(
                        script_entrypoint,
                        bars,
                        spec,
                        {**params_data, **dict(variant)},
                        index=index,
                        lookback=window_size,
                    )
                )
                ticks += 1
                if not _probe_values_match(baseline, candidate):
                    return {
                        "status": "changed",
                        "bar": timestamps[index].isoformat(),
                        "window": window_size,
                        "bars_probed": len(indices),
                        "variants_declared": len(variants),
                        "ticks_evaluated": ticks,
                        "changed_params": dict(variant),
                    }
        return {
            "status": "unchanged",
            "window": window_size,
            "bars_probed": len(indices),
            "variants_declared": len(variants),
            "ticks_evaluated": ticks,
        }

    return asyncio.run(probe())


def window_invariance_probe(
    script_entrypoint: str | Path | Callable[..., Any],
    bars: CompletedBarsView,
    execution_spec: ExecutionSpec | Mapping[str, Any] | None,
    params: Mapping[str, Any],
    *,
    samples: int = WINDOW_PROBE_SAMPLES,
) -> dict[str, Any]:
    """Replay sampled decision bars twice — declared window W versus
    min(available, W + WINDOW_PROBE_EXTRA_BARS) — from identical fresh state
    and require identical intents. A mismatch means decide() consumed history
    beyond its declared window: its backtest inputs are not its live inputs."""
    from wayfinder_paths.jobs.execution.simulator import _load_strategy

    params_data = dict(params)
    spec = ExecutionSpec.coerce(execution_spec)
    window = resolve_compute_window(
        params_data, _load_strategy(script_entrypoint, dict(params_data))
    )
    if not window.declared or window.size is None:
        return {
            "status": "skipped",
            "reason": f"compute window is {window.source}, not declared",
        }
    size = window.size
    timestamps = bars.timestamps
    first = size  # earliest index where the wide slice adds bars
    if len(timestamps) - 1 < first:
        return {
            "status": "skipped",
            "reason": "dataset does not extend beyond the declared window",
        }
    indices = _probe_indices(timestamps, first=first, samples=samples)

    async def probe() -> dict[str, Any]:
        for index in indices:
            # Fresh strategy + state per replay: view depth is the only delta.
            base = await _probe_decided_intents(
                script_entrypoint,
                bars,
                spec,
                params_data,
                index=index,
                lookback=size,
            )
            wide = await _probe_decided_intents(
                script_entrypoint,
                bars,
                spec,
                params_data,
                index=index,
                lookback=size + WINDOW_PROBE_EXTRA_BARS,
            )
            if not _probe_values_match(base, wide):
                return {
                    "status": "failed",
                    "bar": timestamps[index].isoformat(),
                    "window": size,
                    "window_source": window.source,
                    "base_intents": base,
                    "wide_intents": wide,
                    "mismatches": probe_mismatches(base, wide)[:6],
                }
        return {
            "status": "passed",
            "window": size,
            "window_source": window.source,
            "bars_probed": len(indices),
        }

    return asyncio.run(probe())


SEQUENCE_PREVIEW_BARS = 2_000
_SEQUENCE_PREVIEW_STATE_KEYS = 8


def sequence_preview(
    script_entrypoint: str | Path | Callable[..., Any],
    dataset: Any,
    execution_spec: ExecutionSpec | Mapping[str, Any] | None,
    params: Mapping[str, Any],
    *,
    bars: int = SEQUENCE_PREVIEW_BARS,
) -> dict[str, Any]:
    """Replay the last ``bars`` decision bars of a dataset through the real
    sequential simulator (one strategy, one precompute, one engine state)
    and summarize what decide() did: every intent it emitted and how its own
    strategy_state moved bar to bar. An isolated-bar probe cannot see a state
    machine; this can, and names the keys that were written but froze."""
    from wayfinder_paths.jobs.execution.simulator import (  # circular import
        PreparedExecutionDataset,
        _load_strategy,
        simulate_execution,
    )
    from wayfinder_paths.jobs.execution.walk_forward import _slice  # circular

    params_data = dict(params)
    spec = ExecutionSpec.coerce(execution_spec)
    window = resolve_compute_window(
        params_data, _load_strategy(script_entrypoint, dict(params_data))
    )
    if not window.declared or window.size is None:
        return {
            "status": "skipped",
            "reason": f"compute window is {window.source}, not declared",
        }
    timestamps = dataset.bars.timestamps
    if len(timestamps) <= window.size:
        return {
            "status": "skipped",
            "reason": "dataset does not extend beyond the declared window",
        }
    start = max(0, len(timestamps) - (window.size + int(bars)))
    replay: PreparedExecutionDataset = _slice(
        dataset, timestamps, start, len(timestamps)
    )
    result = simulate_execution(
        script_entrypoint, replay, spec, params_data, record_strategy_state=True
    )
    warm = window.size - 1
    runs = list(result.trace.get("runs") or [])[warm:]
    decision_stamps = {str(row.get("timestamp")) for row in runs}
    intents = [
        row
        for row in result.trace.get("intents") or []
        if str(row.get("timestamp")) in decision_stamps
    ]
    by_action: dict[str, int] = {}
    entries = 0
    first_entry_bar: str | None = None
    for intent in intents:
        action = str(intent.get("action") or "").upper()
        by_action[action] = by_action.get(action, 0) + 1
        if action not in REDUCE_ONLY_ACTIONS and not intent.get("reduce_only"):
            entries += 1
            first_entry_bar = first_entry_bar or str(intent.get("timestamp"))
    seen: dict[str, str] = {}
    keys: dict[str, dict[str, Any]] = {}
    frozen_after: str | None = None
    for row in runs:
        stamp = str(row.get("timestamp"))
        for key, digest in (row.get("strategy_state_digest") or {}).items():
            if key not in seen:
                keys[key] = {
                    "first_set_bar": stamp,
                    "last_changed_bar": stamp,
                    "changes": 1,
                }
                frozen_after = stamp
            elif seen[key] != digest:
                keys[key]["last_changed_bar"] = stamp
                keys[key]["changes"] += 1
                frozen_after = stamp
            seen[key] = digest
    ranked = sorted(keys.items(), key=lambda item: (-item[1]["changes"], item[0]))
    status = "entries" if entries else ("armed_no_entry" if keys else "silent")
    return {
        "status": status,
        "bars_replayed": len(runs),
        "window": window.size,
        "intents_total": len(intents),
        "by_action": by_action,
        "entries": entries,
        "first_entry_bar": first_entry_bar,
        "state_keys": dict(ranked[:_SEQUENCE_PREVIEW_STATE_KEYS]),
        "state_keys_total": len(keys),
        "frozen_after": frozen_after,
    }


def _window_invariance_checks(
    root: Path, script_path: Path, job_data: Mapping[str, Any], spec: ExecutionSpec
) -> list[dict[str, Any]]:
    """Bounded-window parity gate: for jobs that DECLARE a compute window,
    prove backtest inputs ≡ forward inputs on the canonical dataset. Jobs
    without a declared window are exempt — the simulator surfaces the
    profiler hint as a backtest validation warning instead."""
    from wayfinder_paths.jobs.execution.simulator import (  # circular import
        _load_strategy,
    )

    params = dict(job_data.get("execution_params") or {})
    try:
        window = resolve_compute_window(params, _load_strategy(script_path, params))
    except Exception:  # noqa: BLE001 — strategy_module_loads reports this already
        return []
    if not window.declared:
        return []
    from wayfinder_paths.jobs.execution.job import _load_dataset  # circular import

    try:
        dataset = _load_dataset(root, spec, dict(job_data))
        probe = window_invariance_probe(script_path, dataset.bars, spec, params)
    except FileNotFoundError:
        return []  # fixture/scenario-driven validation contexts have no dataset
    except Exception as exc:  # noqa: BLE001 — a broken probe must not block jobs
        return [
            {
                "name": "window_invariance",
                "passed": False,
                "blocking": False,
                "error": str(exc)[:300],
            }
        ]
    if probe["status"] == "skipped":
        return [
            {
                "name": "window_invariance",
                "passed": True,
                "blocking": False,
                "note": probe["reason"],
            }
        ]
    check: dict[str, Any] = {
        "name": "window_invariance",
        "passed": probe["status"] == "passed",
        "details": {
            key: value for key, value in probe.items() if not key.endswith("_intents")
        },
    }
    if probe["status"] != "passed":
        check["error"] = (
            f"decide() at bar {probe['bar']} changed its intents when handed "
            f"{WINDOW_PROBE_EXTRA_BARS} bars beyond the declared window of "
            f"{probe['window']} ({probe['window_source']}) — {BOUNDED_WINDOW_HINT}"
        )
    return [check]


def _feature_checks(root: Path, spec: ExecutionSpec) -> list[dict[str, Any]]:
    try:
        specs = parse_feature_specs(spec)
    except ValueError as exc:
        # A malformed feature schema is a spec error: blocking.
        return [{"name": "declared_features_valid", "passed": False, "error": str(exc)}]
    if not specs:
        return []
    frames = load_feature_rows([root], specs)
    missing = [
        item.name
        for item in specs
        if frames.get(item.name) is None or frames[item.name].empty
    ]
    return [
        {"name": "declared_features_valid", "passed": True, "count": len(specs)},
        {
            # Non-blocking: pre-live jobs may declare features before the
            # agent has published any rows.
            "name": "declared_features_available",
            "passed": not missing,
            "missing": missing,
            "blocking": False,
        },
    ]


def _live_wallet_checks(job_data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Live execution signs with `execution_params.wallet_label`; the engine
    default is 'main', which rarely exists on hosted instances — a live job
    without an explicit label starts cleanly and then fails EVERY tick with
    'Wallet not found: main' (observed live: three config guesses across two
    sessions before the nested key was found). Blocking when mode is live."""
    if str(job_data.get("execution_contract") or "legacy") != "jobs_v1":
        return []
    script_loop = job_data.get("script_loop") or {}
    if str(script_loop.get("mode") or "paper") != "live":
        return []
    params = job_data.get("execution_params") or {}
    label = str(params.get("wallet_label") or "").strip()
    return [
        {
            "name": "wallet_label_declared",
            "passed": bool(label),
            "blocking": True,
            "hint": (
                "live mode signs with execution_params.wallet_label (the "
                "engine default 'main' rarely exists on this instance) — set "
                "it to a label from core_get_wallets() before going live; it "
                "is NOT a job-root key, an env var, or an adapter config file"
            ),
            "details": {"wallet_label": label or None},
        }
    ]


def _preflight_checks(
    root: Path, job_data: Mapping[str, Any], spec: ExecutionSpec
) -> list[dict[str, Any]]:
    """A passing preflight (the behavioral gate that drives the real driver
    over replayed data + fault scenarios) is mandatory before live mode."""
    is_jobs_v1 = str(job_data.get("execution_contract") or "legacy") == "jobs_v1"
    if not is_jobs_v1:
        return []
    script_loop = job_data.get("script_loop") or {}
    live = str(script_loop.get("mode") or "paper") == "live"
    blocking = live or spec.strict
    path = root / "reports" / "preflight" / "latest.json"
    if not path.exists():
        return [
            {
                "name": "preflight_report_present",
                "passed": False,
                "blocking": blocking,
                "hint": "run `wayfinder job preflight`",
            }
        ]
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        report = {}
    return [
        {"name": "preflight_report_present", "passed": True, "blocking": blocking},
        {
            "name": "preflight_passed",
            "passed": report.get("status") == "passed",
            "blocking": blocking,
            "details": {
                "status": report.get("status"),
                "revision": report.get("revision"),
            },
        },
    ]


def resolve_execution_spec(
    root: Path, job_data: Mapping[str, Any]
) -> tuple[dict[str, Any], Path | None]:
    match job_data.get("execution_spec"):
        case Mapping() as embedded if embedded:
            return dict(embedded), None
    path = root / "execution_spec.json"
    if not path.exists():
        return {}, None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}, path
    match loaded:
        case dict():
            return loaded, path
        case _:
            return {}, path


STALE_POLICIES = frozenset({"skip", "flat", "decide_anyway"})


def _timing_checks(
    job_data: Mapping[str, Any], spec: ExecutionSpec
) -> list[dict[str, Any]]:
    """Structural timing checks: schedule vs bar interval, timeout, staleness.

    A strategy that consumes 1h bars but wakes every 4h silently skips bars; a
    timeout longer than the schedule period means the runner (which skips
    in-flight ticks and SIGKILLs on timeout) can starve the schedule. Both are
    unobservable in a fixture backtest, so they are validated structurally here.
    """
    is_jobs_v1 = str(job_data.get("execution_contract") or "legacy") == "jobs_v1"
    controller = job_data.get("controller")
    is_starter = isinstance(controller, Mapping) and bool(controller.get("starter"))
    bar_seconds = bar_interval_seconds(spec.data_contract.get("bar_interval"))
    params = job_data.get("execution_params") or {}
    checks: list[dict[str, Any]] = [
        {
            "name": "bar_interval_declared",
            "passed": bar_seconds is not None or not is_jobs_v1,
            "value": spec.data_contract.get("bar_interval"),
            "blocking": is_jobs_v1,
        },
        {
            "name": "staleness_policy_valid",
            "passed": spec.data_contract.get("stale_policy") in STALE_POLICIES,
            "value": spec.data_contract.get("stale_policy"),
            "blocking": False,
        },
        {
            # Without an explicit base, equity/return stats and compound
            # sizing silently use the engine default — declare it.
            "name": "initial_capital_declared",
            "passed": bool(params.get("initial_capital")) or not is_jobs_v1,
            "value": params.get("initial_capital"),
            "blocking": False,
        },
        {
            # The live driver and the backtest simulator hand decide() the
            # SAME declared window (warmup_bars canonical, lookback_bars
            # back-compat); undeclared jobs ride the default cap without ever
            # stating what history the strategy needs. Blocking for starter
            # jobs: most catalog starters have warmup_bars above the old live
            # default, so an undeclared window caps ctx.bar_index below
            # warmup and the job silently never trades.
            "name": "lookback_bars_declared",
            "passed": bool(params.get("warmup_bars") or params.get("lookback_bars"))
            or not is_jobs_v1,
            "value": params.get("warmup_bars") or params.get("lookback_bars"),
            "blocking": is_jobs_v1 and is_starter,
        },
    ]

    script_loop = job_data.get("script_loop") or {}
    if not script_loop.get("enabled"):
        return checks

    try:
        schedule = normalize_schedule(
            interval_seconds=script_loop.get("interval_seconds"),
            cron_expr=script_loop.get("cron_expr"),
            timezone=script_loop.get("timezone"),
        )
        checks.append(
            {"name": "schedule_declared_valid", "passed": True, "kind": schedule.kind}
        )
    except (ValueError, TypeError) as exc:
        checks.append(
            {"name": "schedule_declared_valid", "passed": False, "error": str(exc)}
        )
        return checks

    if schedule.kind == "interval":
        period = schedule.interval_seconds
    elif schedule.cron_expr:
        iterator = croniter(schedule.cron_expr, 0)
        fires = [iterator.get_next(float) for _ in range(4)]
        gaps = sorted(b - a for a, b in zip(fires, fires[1:], strict=False))
        period = int(gaps[len(gaps) // 2])  # median gap
    else:
        period = None
    if bar_seconds is not None and period is not None:
        checks.append(
            {
                "name": "schedule_matches_bar_interval",
                "passed": period <= bar_seconds,
                "schedule_period_seconds": period,
                "bar_interval_seconds": bar_seconds,
            }
        )
    if period is not None:
        timeout = int(script_loop.get("timeout_seconds") or 120)
        checks.append(
            {
                "name": "timeout_vs_interval",
                "passed": timeout < period,
                "timeout_seconds": timeout,
                "schedule_period_seconds": period,
            }
        )
    return checks


def _execution_spec_checks(spec: ExecutionSpec) -> list[dict[str, Any]]:
    return [
        {
            "name": "bar_model_completed_only",
            "passed": spec.bar_model == "completed_only",
        },
        {
            "name": "fill_model_supported",
            "passed": spec.fill_model in {"next_bar_open", "replay"},
        },
        {
            "name": "ohlc_stops_use_high_low",
            "passed": bool(spec.ohlc_rules["use_high_low_for_stops"]),
        },
        {
            "name": "token_state_not_execution_venue",
            "passed": spec.view_type != "token_state",
        },
    ]


def entrypoint_inside_workspace_check(
    root: Path, script_path: Path | None
) -> dict[str, Any]:
    """Blocking: strategy code has exactly one durable, versionable home.

    Revisions hash only workspace/* + job.yaml and proposals stage only
    workspace/, so an entrypoint anywhere else can never be versioned,
    staged, or promoted (and may not even survive an image update).
    """
    workspace = (root / "workspace").resolve()
    passed = bool(
        script_path is not None and script_path.resolve().is_relative_to(workspace)
    )
    check: dict[str, Any] = {
        "name": "entrypoint_inside_workspace",
        "passed": passed,
        "blocking": True,
        "path": str(script_path) if script_path else None,
        "expected_dir": str(root / "workspace" / "src"),
    }
    if not passed:
        check["hint"] = (
            "move the strategy into <job>/workspace/src/ and set "
            "script_loop.entrypoint to 'workspace/src/<file>.py' — revisions "
            "hash only workspace/* + job.yaml and proposals stage only "
            "workspace/, so code elsewhere cannot be versioned or promoted"
        )
    return check


_CLOCK_PERMITTED_TOKENS = ("warmup", "params", "lookback")
_CLOCK_HIT_LIMIT = 4


def _bounded_index_clock_hits(text: str) -> list[str]:
    """Source lines that persist `ctx.bar_index` or do elapsed-time arithmetic
    with it. Permitted: comparisons and arithmetic against constants or
    warmup/params/lookback expressions (`ctx.bar_index < self.warmup_bars`,
    `ctx.bar_index - 1`)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    def unwrap(node: ast.AST) -> ast.AST:
        while (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"int", "float"}
            and len(node.args) == 1
        ):
            node = node.args[0]
        return node

    # Names bound straight from the index (`now = ctx.bar_index`) carry it.
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], unwrap(node.value)
            if (
                isinstance(target, ast.Name)
                and isinstance(value, ast.Attribute)
                and value.attr == "bar_index"
            ):
                aliases.add(target.id)

    def is_bar_index(node: ast.AST) -> bool:
        inner = unwrap(node)
        if isinstance(inner, ast.Name):
            return inner.id in aliases
        return isinstance(inner, ast.Attribute) and inner.attr == "bar_index"

    def permitted(node: ast.AST) -> bool:
        inner = unwrap(node)
        if isinstance(inner, ast.Constant):
            return True
        words = " ".join(
            str(getattr(item, "id", "") or getattr(item, "attr", "") or "")
            + " "
            + (str(item.value) if isinstance(item, ast.Constant) else "")
            for item in ast.walk(inner)
        ).lower()
        return any(token in words for token in _CLOCK_PERMITTED_TOKENS)

    def stored_read(node: ast.AST) -> bool:
        inner = unwrap(node)
        if isinstance(inner, ast.Subscript):
            return not permitted(inner)
        return (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "get"
            and not permitted(inner)
        )

    hits: list[tuple[int, str]] = []

    def record(node: ast.AST) -> None:
        segment = ast.get_source_segment(text, node) or ast.dump(node)
        hits.append((int(getattr(node, "lineno", 0)), " ".join(segment.split())[:120]))

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if (
                node.value is not None
                and is_bar_index(node.value)
                and any(isinstance(t, (ast.Subscript, ast.Attribute)) for t in targets)
            ):
                record(node)
        elif isinstance(node, ast.Dict):
            if any(value is not None and is_bar_index(value) for value in node.values):
                record(node)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and (
                (
                    node.func.attr == "setdefault"
                    and len(node.args) >= 2
                    and is_bar_index(node.args[1])
                )
                or (
                    node.func.attr in {"append", "insert", "add", "appendleft"}
                    and any(is_bar_index(arg) for arg in node.args)
                )
            )
        ):
            record(node)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
            left, right = is_bar_index(node.left), is_bar_index(node.right)
            if left != right:
                other = node.right if left else node.left
                if not permitted(other):
                    record(node)
        elif isinstance(node, ast.Compare) and len(node.comparators) == 1:
            sides = (node.left, node.comparators[0])
            flags = tuple(is_bar_index(side) for side in sides)
            if flags[0] != flags[1]:
                other = sides[1] if flags[0] else sides[0]
                if stored_read(other):
                    record(node)
    unique = sorted(set(hits))[:_CLOCK_HIT_LIMIT]
    return [f"line {line}: {segment}" for line, segment in unique]


def _code_only_text(text: str) -> str:
    """Strip comments and docstrings so static greps see only real code.

    Falls back to the raw text on tokenize failures — those scripts already
    fail execution_script_py_compile with the real error.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except Exception:
        return text
    keep: list[str] = []
    prev_significant = tokenize.INDENT
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and prev_significant in (
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.NEWLINE,
        ):
            # Expression-statement string == docstring; drop it.
            prev_significant = tok.type
            continue
        if tok.type not in (tokenize.NL, tokenize.NEWLINE):
            prev_significant = tok.type
        elif tok.type == tokenize.NEWLINE:
            prev_significant = tokenize.NEWLINE
        keep.append(tok.string)
    return " ".join(keep)


def _script_static_checks(
    script_path: Path, spec: ExecutionSpec
) -> list[dict[str, Any]]:
    text = script_path.read_text(encoding="utf-8", errors="replace")
    checks: list[dict[str, Any]] = []
    try:
        py_compile.compile(str(script_path), doraise=True)
        checks.append({"name": "execution_script_py_compile", "passed": True})
    except Exception as exc:
        checks.append(
            {"name": "execution_script_py_compile", "passed": False, "error": str(exc)}
        )
    checks.append(
        {
            "name": "no_direct_order_placement",
            "passed": not any(pattern in text for pattern in FORBIDDEN_ORDER_PATTERNS),
        }
    )
    raw_hits = [pattern for pattern in RAW_CANDLE_PATTERNS if pattern in text]
    checks.append(
        {
            "name": "no_forbidden_external_candles",
            "passed": not (spec.data_contract["no_external_ccxt"] and raw_hits),
            "details": raw_hits,
        }
    )
    checks.append(
        {
            "name": "no_manual_position_clear",
            "passed": not any(
                pattern in text for pattern in MANUAL_STATE_CLEAR_PATTERNS
            ),
        }
    )
    # Comments and docstrings must neither trip this check ("# time stop:
    # close if held > N days") nor rescue it (a comment saying BracketEngine).
    code_text = _code_only_text(text)
    # Boot-relative warmup/cadence counters go dark for a full warmup period
    # after every state reset and never fire correctly in live's sliding
    # window — the live funding-carry job sat 27 days from one of these.
    counter_gate = re.search(
        r"strategy_state\s*(?:\.\s*get\s*\(|\[)\s*[\"'](?:bar|tick)_?count",
        code_text,
    )
    checks.append(
        {
            "name": "no_boot_relative_warmup",
            "passed": counter_gate is None,
            "blocking": False,
            "hint": (
                "gate warmup on ctx.bar_index (data available in the view) and "
                "cadence on ctx.every_n_bars(n) — tick counters in "
                "strategy_state re-warm from zero on every state reset"
            )
            if counter_gate is not None
            else None,
        }
    )
    # ctx.bar_index is the bounded view length and is constant once warm, so
    # a stored copy never ages: five of eight designs in one campaign armed a
    # state machine on it and never traded. Warmup comparisons stay legal.
    clock_hits = _bounded_index_clock_hits(text)
    checks.append(
        {
            "name": "no_bounded_index_clock",
            "passed": not clock_hits,
            "blocking": True,
            "details": clock_hits,
            "hint": (
                "ctx.bar_index is the bounded view length and is constant once "
                "warm, so it cannot measure elapsed bars: an age, cooldown or "
                "expiry computed from it reads 0 forever and the state machine "
                f"never fires ({'; '.join(clock_hits)}). Stamp ctx.bar_ordinal "
                "into strategy_state and measure with ctx.bars_since(stamp), or "
                "store ctx.timestamp."
            )
            if clock_hits
            else None,
        }
    )
    close_stop_pattern = re.search(
        r"(stop|take_profit|tp).*close", code_text, re.IGNORECASE
    )
    # Intent bracket dicts ({"bracket": {"stop_loss": ...}}) delegate stop
    # evaluation to the engine, which honors ohlc_rules (intrabar highs/lows)
    # — pricing the level off a close is then correct, not a close-only stop.
    # Without this escape hatch `"stop_loss": current_close * 0.98` inside a
    # bracket trips the regex and agents contort strategy code to appease it.
    bracket_delegation = re.search(r"[\"']bracket[\"']\s*:", code_text)
    checks.append(
        {
            "name": "no_close_only_stop_tp",
            "passed": close_stop_pattern is None
            or "BracketEngine" in code_text
            or "ohlc_" in code_text
            or bracket_delegation is not None,
        }
    )
    return checks


def _execution_scenario_checks(
    script_path: Path, job_data: Mapping[str, Any], spec: ExecutionSpec
) -> list[dict[str, Any]]:
    scenario_plan = job_data.get("execution_scenario_plan") or spec.validation.get(
        "execution_scenario_plan"
    )
    if not scenario_plan:
        return [
            {
                "name": "execution_scenario_plan_present",
                "passed": not spec.validation["require_scenarios"],
                "blocking": bool(spec.validation["require_scenarios"]),
            }
        ]
    match scenario_plan:
        case Mapping():
            scenarios = scenario_plan.get("scenarios")
        case _:
            scenarios = None
    match scenarios:
        case list() if scenarios:
            pass
        case _:
            return [{"name": "execution_scenario_plan_present", "passed": False}]
    from wayfinder_paths.jobs.execution.simulator import (  # circular import
        PreparedExecutionDataset,
        simulate_execution,
    )

    checks: list[dict[str, Any]] = [
        {"name": "execution_scenario_plan_present", "passed": True}
    ]
    for index, scenario in enumerate(scenarios):
        name = str(scenario.get("name") or f"scenario_{index + 1}")
        try:
            dataset = PreparedExecutionDataset.from_rows(scenario.get("bars") or [])
            result = simulate_execution(
                script_path,
                dataset,
                spec,
                params=scenario.get("params") or {},
            )
            expected = scenario.get("expect") or {}
            trades = result.trades
            passed = (
                (
                    "min_trades" not in expected
                    or len(trades) >= int(expected["min_trades"])
                )
                and (
                    "max_trades" not in expected
                    or len(trades) <= int(expected["max_trades"])
                )
                and (
                    "execution_valid" not in expected
                    or bool(result.validation["execution_valid"])
                    is bool(expected["execution_valid"])
                )
            )
            checks.append(
                {
                    "name": f"execution_scenario_{name}",
                    "passed": passed,
                    "expected": expected,
                    "stats": result.stats,
                    "validation": result.validation,
                }
            )
        except Exception as exc:
            checks.append(
                {
                    "name": f"execution_scenario_{name}",
                    "passed": False,
                    "error": str(exc),
                }
            )
    return checks


def _latest_trace_validation(root: Path, spec: ExecutionSpec) -> dict[str, Any] | None:
    latest = root / "results" / "backtest" / "latest.json"
    if not latest.exists():
        return None
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "execution_valid": False,
            "critical_failures": ["latest backtest JSON is invalid"],
        }
    match data:
        case {"trace": Mapping() as trace}:
            return validate_execution_trace(trace, spec)
        case _:
            return {
                "execution_valid": False,
                "critical_failures": ["latest backtest trace missing"],
            }


def _report(checks: list[dict[str, Any]], *, strict: bool) -> dict[str, Any]:
    failed = [check for check in checks if not check["passed"]]
    blocking = [
        check for check in failed if strict or check.get("blocking") is not False
    ]
    return {
        "status": "passed" if not blocking else "failed",
        "checks": checks,
        "strict": strict,
        "warnings": [check for check in failed if check not in blocking],
    }


def _suggestions(messages: list[str]) -> list[str]:
    suggestions: list[str] = []
    joined = " ".join(messages).lower()
    if "ohlc" in joined or "bracket" in joined:
        suggestions.append(
            "use BracketEngine / OHLC high-low helpers for stops and take profits"
        )
    if "lookahead" in joined or "future" in joined:
        suggestions.append(
            "feed strategies CompletedBarsView truncated to the current tick"
        )
    if "success" in joined or "status" in joined:
        suggestions.append(
            "treat resting/rejected/ambiguous order responses as non-success"
        )
    return suggestions
