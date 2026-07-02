"""Exogenous feature feeds: driver-owned auxiliary data for decide().

The flexibility contract: unstructured research (briefs, tweets, weather
APIs, anything) flows through the AGENT loop — which has unconstrained I/O —
and is distilled into structured feature rows. Those rows reach the pure
`decide(ctx)` as extra view columns, merged by the DRIVER (live) and by the
dataset loader (backtest) with identical as-of semantics, so backtest/live
parity holds by construction and the purity sandbox stays intact.

Placement decisions (revision-hash aware):
- Feature DATA lives in `state/features.jsonl` — outside the workspace
  revision hash, so continuous appends never invalidate the live gate.
- Feature SCHEMA lives in `execution_spec.data_contract.features` — inside
  job.yaml, revision-bound: changing what a strategy consumes is a strategy
  change and re-gates promotion like any code edit.

Row shape (append-only, timestamps expected monotonic per name):
    {"timestamp": iso8601, "name": str, "value": num|str,
     "symbol": str|null, "written_at": iso8601}

Merge semantics: `merge_asof(..., direction="backward")` — a bar sees the
latest feature row at or before its own timestamp, never a future one.
Late back-dated rows change historical replays and will (correctly) surface
as drift in the reconciler.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    ExecutionSpec,
)

DEFAULT_FEATURES_PATH = "state/features.jsonl"


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    source: str = "file"
    path: str = DEFAULT_FEATURES_PATH
    max_age_seconds: int | None = None
    stale_policy: str = "decide_anyway"  # "skip" | "decide_anyway"
    column: str | None = None

    @property
    def column_name(self) -> str:
        return self.column or self.name

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FeatureSpec:
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("feature spec requires a name")
        raw_age = data.get("max_age_seconds")
        policy = str(data.get("stale_policy") or "decide_anyway")
        if policy not in {"skip", "decide_anyway"}:
            raise ValueError(
                f"feature {name!r}: stale_policy must be skip or decide_anyway"
            )
        return cls(
            name=name,
            source=str(data.get("source") or "file"),
            path=str(data.get("path") or DEFAULT_FEATURES_PATH),
            max_age_seconds=int(raw_age) if raw_age is not None else None,
            stale_policy=policy,
            column=str(data["column"]) if data.get("column") else None,
        )


def parse_feature_specs(spec: ExecutionSpec) -> list[FeatureSpec]:
    raw = spec.data_contract.get("features") or []
    if not isinstance(raw, list):
        raise ValueError("execution_spec.data_contract.features must be a list")
    specs = [FeatureSpec.from_dict(item) for item in raw if isinstance(item, Mapping)]
    for item in specs:
        if item.source not in FEATURE_SOURCES:
            raise ValueError(
                f"feature {item.name!r}: unknown source {item.source!r} "
                f"(registered: {sorted(FEATURE_SOURCES)})"
            )
    return specs


def _load_file_rows(roots: list[Path], spec: FeatureSpec) -> list[dict[str, Any]]:
    """First root that has the file wins (candidate dir before job dir —
    mirrors the candidate dataset fallback)."""
    for root in roots:
        path = Path(root) / spec.path
        if not path.exists():
            continue
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and str(row.get("name")) == spec.name:
                rows.append(row)
        return rows
    return []


FEATURE_SOURCES: dict[
    str, Callable[[list[Path], FeatureSpec], list[dict[str, Any]]]
] = {
    "file": _load_file_rows,
}


def load_feature_rows(
    roots: list[Path], specs: list[FeatureSpec]
) -> dict[str, pd.DataFrame]:
    """Per-feature frames sorted by timestamp: columns [timestamp, value,
    symbol]. Empty frame when a feature has no rows yet."""
    frames: dict[str, pd.DataFrame] = {}
    for spec in specs:
        rows = FEATURE_SOURCES[spec.source](roots, spec)
        if not rows:
            frames[spec.name] = pd.DataFrame(
                columns=["timestamp", "value", "symbol"]
            )
            continue
        frame = pd.DataFrame(
            [
                {
                    "timestamp": row.get("timestamp"),
                    "value": row.get("value"),
                    "symbol": row.get("symbol"),
                }
                for row in rows
            ]
        )
        frame["timestamp"] = pd.to_datetime(
            frame["timestamp"], utc=True, errors="coerce"
        )
        frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp")
        frames[spec.name] = frame.reset_index(drop=True)
    return frames


def merge_features(
    view: CompletedBarsView,
    frames: Mapping[str, pd.DataFrame],
    specs: list[FeatureSpec],
) -> CompletedBarsView:
    """As-of (backward) merge of feature values onto the bar frame as extra
    columns. A feature is either global (all rows symbol-null → broadcast to
    every symbol) or per-symbol (rows joined by symbol; null-symbol rows in a
    per-symbol feature are ignored). No-op when no specs match."""
    if not specs:
        return view
    bars = view.to_frame().sort_values(["timestamp", "symbol"]).reset_index(drop=True)
    for spec in specs:
        feature = frames.get(spec.name)
        column = spec.column_name
        if feature is None or feature.empty:
            bars[column] = None
            continue
        per_symbol = feature["symbol"].notna().any()
        if per_symbol:
            sub = feature.dropna(subset=["symbol"]).rename(
                columns={"value": column}
            )
            sub["symbol"] = sub["symbol"].astype(str)
            merged = pd.merge_asof(
                bars.sort_values("timestamp"),
                sub[["timestamp", "symbol", column]].sort_values("timestamp"),
                on="timestamp",
                by="symbol",
                direction="backward",
            )
        else:
            sub = feature.rename(columns={"value": column})
            merged = pd.merge_asof(
                bars.sort_values("timestamp"),
                sub[["timestamp", column]].sort_values("timestamp"),
                on="timestamp",
                direction="backward",
            )
        bars = merged
    for spec in specs:
        column = spec.column_name
        if column in bars.columns:
            bars[column] = bars[column].astype(object).where(
                bars[column].notna(), None
            )
    return CompletedBarsView(bars)


def feature_staleness(
    specs: list[FeatureSpec],
    frames: Mapping[str, pd.DataFrame],
    now: pd.Timestamp,
) -> tuple[list[dict[str, Any]], bool]:
    """Per-feature max_age check mirroring bar staleness: guard events for
    every stale feature; skip=True iff any stale feature's policy is skip."""
    guard_events: list[dict[str, Any]] = []
    skip = False
    for spec in specs:
        if spec.max_age_seconds is None:
            continue
        frame = frames.get(spec.name)
        if frame is None or frame.empty:
            age = None
        else:
            age = float((now - frame["timestamp"].iloc[-1]).total_seconds())
        if age is None or age > spec.max_age_seconds:
            guard_events.append(
                {
                    "kind": "stale_feature",
                    "feature": spec.name,
                    "age_seconds": age,
                    "max_age_seconds": spec.max_age_seconds,
                    "policy": spec.stale_policy,
                    "timestamp": now.isoformat(),
                }
            )
            if spec.stale_policy == "skip":
                skip = True
    return guard_events, skip


def summarize_features(
    root: Path, spec: ExecutionSpec, *, now: pd.Timestamp | None = None
) -> list[dict[str, Any]] | None:
    """Bounded per-feature status for the sync snapshot: latest value + age."""
    try:
        specs = parse_feature_specs(spec)
    except ValueError:
        return None
    if not specs:
        return None
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    frames = load_feature_rows([Path(root)], specs)
    summary: list[dict[str, Any]] = []
    for item in specs:
        frame = frames.get(item.name)
        if frame is None or frame.empty:
            summary.append({"name": item.name, "available": False})
            continue
        latest = frame.iloc[-1]
        summary.append(
            {
                "name": item.name,
                "available": True,
                "latest_value": latest["value"],
                "latest_timestamp": latest["timestamp"].isoformat(),
                "age_seconds": float(
                    (now - latest["timestamp"]).total_seconds()
                ),
                "row_count": int(len(frame)),
            }
        )
    return summary
