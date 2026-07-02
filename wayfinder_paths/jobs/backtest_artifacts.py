from __future__ import annotations

import math
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from wayfinder_paths.jobs.store import JobStore

DERIVED_SERIES_KINDS = {
    "spread",
    "ratio",
    "z_score",
    "zscore",
    "indicator",
    "signal",
    "basket",
}
PERFORMANCE_SERIES_KINDS = {
    "equity_curve",
    "drawdown_curve",
    "realized_pnl",
    "unrealized_pnl",
    "pnl",
}
VIEW_KINDS = {
    "legs": {"market_price"},
    "spread": DERIVED_SERIES_KINDS,
    "equity": {"equity_curve"},
    "drawdown": {"drawdown_curve"},
    "performance": PERFORMANCE_SERIES_KINDS,
}


def summarize_backtest_artifacts(
    job_id: str, *, store: JobStore | None = None, proposal_id: str | None = None
) -> dict[str, Any]:
    store = store or JobStore()
    prefix = (
        f"applications/{proposal_id}/candidate/results/backtest"
        if proposal_id
        else "results/backtest"
    )
    visualization = store.read_json(job_id, f"{prefix}/visualization.json")
    latest = store.read_json(job_id, f"{prefix}/latest.json", default={}) or {}
    if not visualization:
        return {"available": False}

    viz_path = store.job_dir(job_id) / "results" / "backtest" / "visualization.json"
    marker_counts = Counter(marker["kind"] for marker in visualization["markers"])
    return {
        "available": True,
        "run_id": latest["run_id"] if latest else None,
        "updated_at": (
            datetime.fromtimestamp(viz_path.stat().st_mtime).astimezone().isoformat()
            if viz_path.exists()
            else None
        ),
        "stats": latest["stats"] if latest else {},
        "symbols": visualization["symbols"],
        "series": [
            {
                "name": series["name"],
                "kind": series["kind"],
                "symbol": series.get("symbol"),
                "point_count": len(series["points"]),
            }
            for series in visualization["series"]
        ],
        "marker_counts": dict(marker_counts),
        "marker_count": sum(marker_counts.values()),
        "validation": visualization["validation"]
        or (latest["validation"] if latest else {}),
    }


def load_backtest_view(
    job_id: str,
    *,
    store: JobStore | None = None,
    view: str = "all",
    series_names: list[str] | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    max_points: int = 1500,
    proposal_id: str | None = None,
) -> dict[str, Any]:
    store = store or JobStore()
    # Proposal-scoped view (contract C2): read the CANDIDATE run written by
    # candidate validation so the FE can overlay it against the active run.
    prefix = (
        f"applications/{proposal_id}/candidate/results/backtest"
        if proposal_id
        else "results/backtest"
    )
    visualization = store.read_json(job_id, f"{prefix}/visualization.json")
    latest = store.read_json(job_id, f"{prefix}/latest.json", default={}) or {}
    if not visualization:
        return {"available": False}

    requested = {item.strip() for item in series_names or [] if item.strip()}
    bounded_max = min(max(max_points, 100), 10_000)
    start = _parse_ts(from_ts)
    end = _parse_ts(to_ts)
    kinds = VIEW_KINDS.get(view)
    selected_series = []
    for series in visualization["series"]:
        if requested and series["name"] not in requested:
            continue
        if kinds is not None and series["kind"] not in kinds:
            continue
        points = [
            point
            for point in series["points"]
            if _in_range(_parse_ts(point["timestamp"]), start, end)
        ]
        if len(points) > bounded_max:
            # Even-stride downsample keeping first and last points;
            # bounded_max >= 100, so no degenerate two-point case.
            last_index = len(points) - 1
            points = [
                points[math.floor(index * last_index / (bounded_max - 1))]
                for index in range(bounded_max)
            ]
        selected_series.append({**series, "points": points})
    symbols = {
        str(series["symbol"])
        for series in selected_series
        if series.get("symbol") is not None
    }
    markers = [
        marker
        for marker in visualization["markers"]
        if _in_range(_parse_ts(marker["timestamp"]), start, end)
        and (view != "legs" or not symbols or str(marker["symbol"]) in symbols)
    ]
    return {
        "available": True,
        "view": view,
        "run_id": latest["run_id"] if latest else None,
        "summary": summarize_backtest_artifacts(
            job_id, store=store, proposal_id=proposal_id
        ),
        "visualization": {
            key: value
            for key, value in visualization.items()
            if key not in {"series", "markers"}
        }
        | {"series": selected_series, "markers": markers},
        "trades": latest["trades"] if latest else [],
    }


def _in_range(
    current: datetime | None, start: datetime | None, end: datetime | None
) -> bool:
    if current is None:
        return True
    if start and current < start:
        return False
    return not (end and current > end)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # from_ts/to_ts arrive as free-form CLI/agent input; a bad bound
        # disables filtering rather than failing the whole view.
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
