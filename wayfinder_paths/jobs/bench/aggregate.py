"""Pre-registered aggregation for repeated campaign A/B runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.bench.env import atomic_json
from wayfinder_paths.jobs.economics import block_bootstrap_lcb


def aggregate_experiment(
    rows: list[dict[str, Any]],
    *,
    arm_order: list[str],
    output_dir: Path | None = None,
    confidence: float = 0.90,
    max_cost_ratio: float = 1.25,
    pilot: bool = False,
) -> dict[str, Any]:
    if len(arm_order) != 2:
        raise ValueError("campaign A/B aggregation requires exactly two arms")
    invalid_rows = [row for row in rows if row.get("invalid_reason")]
    by_key = {
        (str(row["world_id"]), int(row["seed"]), str(row["arm"])): row
        for row in rows
        if not row.get("invalid_reason")
    }
    paired: list[dict[str, Any]] = []
    for world_id, seed in sorted({(key[0], key[1]) for key in by_key}):
        left = by_key.get((world_id, seed, arm_order[0]))
        right = by_key.get((world_id, seed, arm_order[1]))
        if not left or not right:
            continue
        left_delta = _holdout_estimate(left)
        right_delta = _holdout_estimate(right)
        paired.append(
            {
                "world_id": world_id,
                "seed": seed,
                "a": left_delta,
                "b": right_delta,
                "delta": left_delta - right_delta,
            }
        )
    values = [row["delta"] for row in paired]
    estimate = sum(values) / len(values) if values else 0.0
    lcb = block_bootstrap_lcb(
        values, block_len=1, iterations=1_000, confidence=confidence
    )
    reverse = block_bootstrap_lcb(
        [-value for value in values],
        block_len=1,
        iterations=1_000,
        confidence=confidence,
    )
    ucb = -reverse if reverse is not None else None
    arm_rows = {
        arm: [row for row in rows if row.get("arm") == arm] for arm in arm_order
    }
    arm_stats = {arm: _arm_stats(items) for arm, items in arm_rows.items()}
    a_cost = arm_stats[arm_order[0]]["tokens_total"]
    b_cost = arm_stats[arm_order[1]]["tokens_total"]
    cost_ratio = (
        max(a_cost, b_cost) / min(a_cost, b_cost) if min(a_cost, b_cost) > 0 else None
    )
    cost_matched = cost_ratio is not None and cost_ratio <= max_cost_ratio
    if invalid_rows:
        decision = "invalid_arm_runs"
    elif not paired:
        decision = "invalid_unpaired"
    elif not cost_matched:
        decision = "invalid_cost_mismatch"
    elif lcb is not None and lcb > 0:
        decision = "a_wins"
    elif ucb is not None and ucb < 0:
        decision = "b_wins"
    else:
        decision = "no_significant_difference"
    if pilot and not decision.startswith("invalid_"):
        decision = "pilot_directional_only"
    report = {
        "schema_version": "1.0",
        "arms": arm_order,
        "pre_registered_rule": {
            "primary": "paired LCB of holdout utility delta across world/seed",
            "confidence": confidence,
            "cost_ratio_max": max_cost_ratio,
            "decision": (
                (
                    "pilot reports direction and process metrics only; it cannot "
                    "declare a winner"
                )
                if pilot
                else (
                    "winner only when the paired interval excludes zero and token "
                    "cost ratio is within the pre-registered bound"
                )
            ),
        },
        "pilot": pilot,
        "primary": {
            "pairs": len(paired),
            "estimate": round(estimate, 8),
            "lcb": lcb,
            "ucb": ucb,
            "rows": paired,
        },
        "by_arm": arm_stats,
        "invalid_runs": [
            {
                "arm": row.get("arm"),
                "world_id": row.get("world_id"),
                "seed": row.get("seed"),
                "reason": row.get("invalid_reason"),
            }
            for row in invalid_rows
        ],
        "cost_parity": {"ratio": cost_ratio, "matched": cost_matched},
        "decision": decision,
    }
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        atomic_json(output_dir / "aggregate.json", report)
        (output_dir / "report.txt").write_text(_format_report(report), encoding="utf-8")
    return report


def _holdout_estimate(row: dict[str, Any]) -> float:
    holdout = row.get("holdout") or {}
    paired = holdout.get("paired_daily_utility_delta") or {}
    return float(paired.get("estimate") or 0.0)


def _arm_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    funnel_keys = (
        "candidates_generated",
        "attempts",
        "screen_positive",
        "full_dev",
        "gate_passed",
        "staged",
    )
    funnel = {
        key: sum(int((row.get("funnel") or {}).get(key) or 0) for row in rows)
        for key in funnel_keys
    }
    verdicts = {
        status: sum(
            1 for row in rows if str((row.get("forward") or {}).get("status")) == status
        )
        for status in ("burn_in", "active", "graduated", "killed", "inconclusive")
    }
    tokens = sum(
        int((row.get("cost") or {}).get(key) or 0)
        for row in rows
        for key in ("tokens_in", "tokens_out", "tokens_reasoning")
    )
    staged = funnel["staged"]
    elite_counts = [
        float((row.get("diversity") or {}).get("mean_elite_trade_count") or 0.0)
        for row in rows
    ]
    return {
        "runs": len(rows),
        "invalid_runs": sum(bool(row.get("invalid_reason")) for row in rows),
        "funnel": funnel,
        "verdicts": verdicts,
        "diversity": {
            "qd_cells_occupied": sum(
                int((row.get("diversity") or {}).get("qd_cells_occupied") or 0)
                for row in rows
            ),
            "mean_elite_trade_count": (
                round(sum(elite_counts) / len(elite_counts), 3) if elite_counts else 0.0
            ),
        },
        "process": {
            key: sum(int((row.get("process") or {}).get(key) or 0) for row in rows)
            for key in (
                "fact_citations",
                "wildcards_used",
                "postmortems_consumed",
                "behavior_changed_attempts",
                "behavior_unchanged_attempts",
                "quick_simulations",
                "behavior_preview_rejections",
                "behavior_preview_ticks",
            )
        },
        "tokens_total": tokens,
        "tokens_per_staged_candidate": round(tokens / staged, 2) if staged else None,
        "tool_calls": sum(
            int((row.get("cost") or {}).get("tool_calls") or 0) for row in rows
        ),
        "tool_output_bytes": sum(
            int((row.get("cost") or {}).get("tool_output_bytes") or 0) for row in rows
        ),
        "wall_seconds": round(
            sum(
                float((row.get("cost") or {}).get("wall_seconds") or 0) for row in rows
            ),
            3,
        ),
        "model_seconds": round(
            sum(
                float((row.get("cost") or {}).get("model_seconds") or 0) for row in rows
            ),
            3,
        ),
        "tool_seconds": round(
            sum(
                float((row.get("cost") or {}).get("tool_seconds") or 0) for row in rows
            ),
            3,
        ),
        "other_seconds": round(
            sum(
                float((row.get("cost") or {}).get("other_seconds") or 0) for row in rows
            ),
            3,
        ),
    }


def _format_report(report: dict[str, Any]) -> str:
    primary = report["primary"]
    return (
        f"Campaign A/B decision: {report['decision']}\n"
        f"Arms: {report['arms'][0]} vs {report['arms'][1]}\n"
        f"Paired runs: {primary['pairs']}\n"
        f"Mean paired utility delta: {primary['estimate']}\n"
        f"90% interval: [{primary['lcb']}, {primary['ucb']}]\n"
        f"Cost parity: {report['cost_parity']}\n"
    )
