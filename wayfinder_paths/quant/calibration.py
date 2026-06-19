"""Calibration metrics for model outputs and betting forecasts."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any


def brier_score(p: float, outcome: int) -> float:
    return (float(p) - int(outcome)) ** 2


def log_loss(p: float, outcome: int, eps: float = 1e-6) -> float:
    prob = min(max(float(p), eps), 1.0 - eps)
    return -math.log(prob if int(outcome) else 1.0 - prob)


def calibration_buckets(
    rows: list[dict[str, Any]],
    bucket_size: float = 0.1,
) -> list[dict[str, Any]]:
    buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("p") is None or row.get("outcome") is None:
            continue
        idx = min(int(float(row["p"]) / float(bucket_size)), int(1 / bucket_size) - 1)
        buckets[idx].append(row)
    result = []
    for idx in sorted(buckets):
        bucket = buckets[idx]
        probs = [float(row["p"]) for row in bucket]
        outcomes = [int(row["outcome"]) for row in bucket]
        result.append(
            {
                "bucketStart": idx * bucket_size,
                "bucketEnd": (idx + 1) * bucket_size,
                "count": len(bucket),
                "avgPrediction": sum(probs) / len(probs),
                "realizedRate": sum(outcomes) / len(outcomes),
            }
        )
    return result


def reliability_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [
        row
        for row in rows
        if row.get("p") is not None and row.get("outcome") is not None
    ]
    if not scored:
        return {"count": 0, "brier": None, "logLoss": None, "buckets": []}
    return {
        "count": len(scored),
        "brier": sum(
            brier_score(float(row["p"]), int(row["outcome"])) for row in scored
        )
        / len(scored),
        "logLoss": sum(log_loss(float(row["p"]), int(row["outcome"])) for row in scored)
        / len(scored),
        "buckets": calibration_buckets(scored),
    }


def closing_line_value(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = []
    for row in rows:
        entry = row.get("entryPrice")
        close = row.get("closePrice")
        side = str(row.get("side") or "YES").upper()
        if entry is None or close is None:
            continue
        delta = float(close) - float(entry)
        values.append(delta if side in {"YES", "OVER", "HOME", "BUY"} else -delta)
    if not values:
        return {"count": 0, "avgClv": None, "positiveClvRate": None}
    return {
        "count": len(values),
        "avgClv": sum(values) / len(values),
        "positiveClvRate": sum(1 for value in values if value > 0) / len(values),
    }


def update_model_trust(
    *,
    model_id: str,
    calibration_report: dict[str, Any],
) -> dict[str, Any]:
    brier = calibration_report.get("brier")
    count = int(calibration_report.get("count") or 0)
    if brier is None or count < 50:
        multiplier = 0.75
        reason = "thin_or_missing_calibration"
    elif float(brier) <= 0.20:
        multiplier = 1.10
        reason = "well_calibrated"
    elif float(brier) <= 0.24:
        multiplier = 1.0
        reason = "acceptable_calibration"
    else:
        multiplier = 0.70
        reason = "poor_calibration"
    return {
        "modelId": model_id,
        "trustMultiplier": multiplier,
        "reason": reason,
        "calibrationCount": count,
    }
