from __future__ import annotations

import math
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
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


def summarize_backtest_artifacts(
    job_id: str, *, store: JobStore | None = None, proposal_id: str | None = None
) -> dict[str, Any]:
    store = store or JobStore()
    root = _backtest_dir(store, job_id)
    prefix = (
        f"applications/{proposal_id}/candidate/results/backtest"
        if proposal_id
        else "results/backtest"
    )
    visualization = store.read_json(job_id, f"{prefix}/visualization.json")
    latest = store.read_json(job_id, f"{prefix}/latest.json", default={}) or {}
    if not isinstance(visualization, dict):
        return {"available": False}

    series = visualization.get("series") if isinstance(visualization, dict) else []
    markers = visualization.get("markers") if isinstance(visualization, dict) else []
    series_summary = [
        _series_summary(item) for item in series if isinstance(item, dict)
    ]
    marker_counts = Counter(
        str(item.get("kind") or "unknown") for item in markers if isinstance(item, dict)
    )
    return {
        "available": True,
        "run_id": latest.get("run_id"),
        "updated_at": _modified_at(root / "visualization.json"),
        "stats": latest.get("stats") if isinstance(latest.get("stats"), dict) else {},
        "symbols": visualization.get("symbols") or [],
        "series": series_summary,
        "marker_counts": dict(marker_counts),
        "marker_count": sum(marker_counts.values()),
        "validation": visualization.get("validation") or latest.get("validation") or {},
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
    if not isinstance(visualization, dict):
        return {"available": False}

    requested = {item.strip() for item in series_names or [] if item.strip()}
    bounded_max = min(max(int(max_points or 1500), 100), 10_000)
    selected_series = [
        _filter_series_points(
            series, from_ts=from_ts, to_ts=to_ts, max_points=bounded_max
        )
        for series in visualization.get("series") or []
        if isinstance(series, dict)
        and _series_matches(series, view=view, requested_names=requested)
    ]
    symbols = {
        str(series.get("symbol"))
        for series in selected_series
        if series.get("symbol") is not None
    }
    markers = [
        marker
        for marker in visualization.get("markers") or []
        if isinstance(marker, dict)
        and _marker_in_range(marker, from_ts=from_ts, to_ts=to_ts)
        and _marker_matches_view(marker, view=view, symbols=symbols)
    ]
    return {
        "available": True,
        "view": view,
        "run_id": latest.get("run_id"),
        "summary": summarize_backtest_artifacts(
            job_id, store=store, proposal_id=proposal_id
        ),
        "visualization": {
            key: value
            for key, value in visualization.items()
            if key not in {"series", "markers"}
        }
        | {"series": selected_series, "markers": markers},
        "trades": latest.get("trades")
        if isinstance(latest.get("trades"), list)
        else [],
    }


def _backtest_dir(store: JobStore, job_id: str) -> Path:
    return store.job_dir(job_id) / "results" / "backtest"


def _series_summary(series: dict[str, Any]) -> dict[str, Any]:
    points = series.get("points") if isinstance(series.get("points"), list) else []
    return {
        "name": series.get("name"),
        "kind": series.get("kind"),
        "symbol": series.get("symbol"),
        "point_count": len(points),
    }


def _filter_series_points(
    series: dict[str, Any],
    *,
    from_ts: str | None,
    to_ts: str | None,
    max_points: int,
) -> dict[str, Any]:
    points = [
        point
        for point in series.get("points") or []
        if isinstance(point, dict)
        and _point_in_range(point, from_ts=from_ts, to_ts=to_ts)
    ]
    return {**series, "points": _downsample(points, max_points)}


def _series_matches(
    series: dict[str, Any], *, view: str, requested_names: set[str]
) -> bool:
    name = str(series.get("name") or "")
    if requested_names and name not in requested_names:
        return False
    kind = str(series.get("kind") or "")
    if view in {"all", ""}:
        return True
    if view == "legs":
        return kind == "market_price"
    if view == "spread":
        return kind in DERIVED_SERIES_KINDS
    if view == "equity":
        return kind == "equity_curve"
    if view == "drawdown":
        return kind == "drawdown_curve"
    if view == "performance":
        return kind in PERFORMANCE_SERIES_KINDS
    return True


def _marker_matches_view(
    marker: dict[str, Any], *, view: str, symbols: set[str]
) -> bool:
    if view in {"all", "spread", "equity", "drawdown", "performance", ""}:
        return True
    if view == "legs":
        symbol = marker.get("symbol")
        return not symbols or symbol is None or str(symbol) in symbols
    return True


def _point_in_range(
    point: dict[str, Any], *, from_ts: str | None, to_ts: str | None
) -> bool:
    return _timestamp_in_range(
        point.get("timestamp") or point.get("time") or point.get("ts"),
        from_ts=from_ts,
        to_ts=to_ts,
    )


def _marker_in_range(
    marker: dict[str, Any], *, from_ts: str | None, to_ts: str | None
) -> bool:
    return _timestamp_in_range(marker.get("timestamp"), from_ts=from_ts, to_ts=to_ts)


def _timestamp_in_range(value: Any, *, from_ts: str | None, to_ts: str | None) -> bool:
    current = _parse_ts(value)
    if current is None:
        return True
    start = _parse_ts(from_ts)
    end = _parse_ts(to_ts)
    if start and current < start:
        return False
    if end and current > end:
        return False
    return True


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000 if value > 100_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, UTC)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _downsample(points: list[dict[str, Any]], max_points: int) -> list[dict[str, Any]]:
    if len(points) <= max_points:
        return points
    if max_points <= 2:
        return [points[0], points[-1]]
    last_index = len(points) - 1
    return [
        points[math.floor(index * last_index / (max_points - 1))]
        for index in range(max_points)
    ]


def _modified_at(path: Path) -> str | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat()
