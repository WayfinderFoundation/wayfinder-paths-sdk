from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from wayfinder_paths.core.backtesting.execution.primitives import ExecutionSpec


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
