"""Feature row writer: how agents publish signals for decide() to consume.

The agent loop (or any offline script) researches whatever it wants — briefs,
tweets, weather APIs — and distills the result into structured rows here.
The driver/dataset loader merges them into views as-of each bar (see
execution/features.py), so the strategy reads them purely via
`ctx.view.feature(name)` with identical backtest/live semantics.

Rows are append-only and expected monotonic per feature name; back-dated
rows change historical replays and will surface as reconciler drift.
"""

from __future__ import annotations

import json
from typing import Any

from wayfinder_paths.jobs.execution.features import DEFAULT_FEATURES_PATH
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore


def append_feature(
    store: JobStore,
    job_id: str,
    *,
    name: str,
    value: Any,
    timestamp: str | None = None,
    symbol: str | None = None,
    path: str = DEFAULT_FEATURES_PATH,
) -> dict[str, Any]:
    if not str(name).strip():
        raise ValueError("feature name is required")
    row = {
        "timestamp": timestamp or utc_now_iso(),
        "name": str(name),
        "value": value,
        "symbol": symbol,
        "written_at": utc_now_iso(),
    }
    target = store.job_dir(job_id) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    return row


def list_features(
    store: JobStore,
    job_id: str,
    *,
    name: str | None = None,
    limit: int = 50,
    path: str = DEFAULT_FEATURES_PATH,
) -> list[dict[str, Any]]:
    target = store.job_dir(job_id) / path
    if not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        match row:
            case dict() if name is None or row.get("name") == name:
                rows.append(row)
    return rows[-max(int(limit), 1) :]
