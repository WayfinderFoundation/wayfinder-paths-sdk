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
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    ExecutionSpec,
)

DEFAULT_FEATURES_PATH = "state/features.jsonl"


WORKSPACE_FEATURE_PREFIX = "workspace/"


def _contained_feature_path(name: str, raw: Any) -> str:
    """The two homes a declared feature file may have: the job store
    (job-owned, refreshed by the derive op) or a file under workspace/
    (candidate-owned: copied by copy_job_bundle and hashed into the
    revision). Anything else — an absolute path, `..`, another job's store,
    a loose file under state/ — is refused here so live, backtest,
    probation and the bench all fail closed on the same rule."""
    path = str(raw or DEFAULT_FEATURES_PATH).strip()
    parts = PurePosixPath(path).parts
    if (
        not parts
        or PurePosixPath(path).is_absolute()
        or path.startswith(("/", "\\"))
        or ".." in parts
        or any(not part or part == "." for part in parts)
    ):
        raise ValueError(
            f"feature {name!r}: path must be relative, without '..': {path!r}"
        )
    if path != DEFAULT_FEATURES_PATH and not path.startswith(WORKSPACE_FEATURE_PREFIX):
        raise ValueError(
            f"feature {name!r}: path must be the job store {DEFAULT_FEATURES_PATH!r} "
            f"or a file under {WORKSPACE_FEATURE_PREFIX!r}: {path!r}"
        )
    return path


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
            path=_contained_feature_path(name, data.get("path")),
            max_age_seconds=int(raw_age) if raw_age is not None else None,
            stale_policy=policy,
            column=str(data["column"]) if data.get("column") else None,
        )


def parse_feature_specs(spec: ExecutionSpec) -> list[FeatureSpec]:
    raw = spec.data_contract.get("features") or []
    match raw:
        case list():
            pass
        case _:
            raise ValueError("execution_spec.data_contract.features must be a list")
    specs = []
    for item in raw:
        match item:
            case Mapping():
                specs.append(FeatureSpec.from_dict(item))
    for item in specs:
        if item.source != "file":
            raise ValueError(
                f"feature {item.name!r}: unknown source {item.source!r} "
                "(only 'file' is supported)"
            )
    return specs


# Single-entry parse cache keyed on (path, mtime_ns, size, names). The live
# driver calls load_feature_rows every tick; without this each 5-minute tick
# re-parsed the full store (95MB / 600k+ lines observed live) once per
# declared feature. Any append invalidates via mtime/size.
_FEATURE_FILE_CACHE: dict[str, Any] = {}


def _parse_feature_file(path: Path, names: set[str]) -> dict[str, dict[str, list]]:
    """ONE streaming pass over the jsonl store collecting column lists for
    every requested name — the store is parsed once, not once per spec."""
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size, tuple(sorted(names)))
    if _FEATURE_FILE_CACHE.get("key") == key:
        return _FEATURE_FILE_CACHE["columns"]
    columns: dict[str, dict[str, list]] = {
        name: {"timestamp": [], "value": [], "symbol": []} for name in names
    }
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            bucket = columns.get(str(row.get("name")))
            if bucket is None:
                continue
            bucket["timestamp"].append(row.get("timestamp"))
            bucket["value"].append(row.get("value"))
            bucket["symbol"].append(row.get("symbol"))
    _FEATURE_FILE_CACHE["key"] = key
    _FEATURE_FILE_CACHE["columns"] = columns
    return columns


def _trim_to_window(
    frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp
) -> pd.DataFrame:
    """Rows within [start, end] plus, per symbol series (including the
    null-symbol series), the latest row strictly before start — exactly what
    merge_asof(direction=backward) can observe inside the window."""
    before = frame[frame["timestamp"] < start]
    if not before.empty:
        anchor_idx = (
            before.groupby(before["symbol"].astype(object), dropna=False)["timestamp"]
            .idxmax()
            .tolist()
        )
        anchors = frame.loc[anchor_idx]
    else:
        anchors = frame.iloc[0:0]
    inside = frame[(frame["timestamp"] >= start) & (frame["timestamp"] <= end)]
    return pd.concat([anchors, inside]).sort_values("timestamp").reset_index(drop=True)


def load_feature_rows(
    roots: list[Path],
    specs: list[FeatureSpec],
    *,
    window: tuple[pd.Timestamp, pd.Timestamp] | None = None,
) -> dict[str, pd.DataFrame]:
    """Per-feature frames sorted by timestamp: columns [timestamp, value,
    symbol]. Empty frame when a feature has no rows yet. First root that has
    the file wins (candidate dir before job dir — mirrors the candidate
    dataset fallback).

    `window=(start, end)` trims each frame to the bar range plus the as-of
    anchor row per series — merge_asof(backward) sees identical values, but
    a multi-month store no longer rides through a 120-day backtest in full."""
    by_path: dict[Path, set[str]] = {}
    spec_path: dict[str, Path | None] = {}
    for spec in specs:
        chosen: Path | None = None
        # Ownership by path class, not first-existing-wins. ``roots`` is
        # ``(bundle, protected_root)``: the job store is job-owned and comes
        # from the protected root (the campaign snapshot during a campaign,
        # the job root live), never from a bundle; a workspace/ file is
        # candidate-owned and comes from the bundle, never from the job's own
        # workspace. One root means both are the same place.
        owner = Path(roots[-1] if spec.path == DEFAULT_FEATURES_PATH else roots[0])
        candidate = owner / spec.path
        if candidate.exists():
            # from_dict refuses `..` and absolute paths; a symlink under
            # workspace/ could still point out of the root.
            if not candidate.resolve().is_relative_to(owner.resolve()):
                raise ValueError(f"feature {spec.name!r}: path escapes its root")
            chosen = candidate
        spec_path[spec.name] = chosen
        if chosen is not None:
            by_path.setdefault(chosen, set()).add(spec.name)

    parsed = {path: _parse_feature_file(path, names) for path, names in by_path.items()}

    frames: dict[str, pd.DataFrame] = {}
    for spec in specs:
        path = spec_path[spec.name]
        columns = parsed.get(path, {}).get(spec.name) if path is not None else None
        if not columns or not columns["timestamp"]:
            frames[spec.name] = pd.DataFrame(columns=["timestamp", "value", "symbol"])
            continue
        frame = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    columns["timestamp"], utc=True, errors="coerce"
                ),
                "value": columns["value"],
                "symbol": columns["symbol"],
            }
        )
        frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp")
        if window is not None:
            frame = _trim_to_window(frame, window[0], window[1])
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
            sub = feature.dropna(subset=["symbol"]).rename(columns={"value": column})
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
            bars[column] = bars[column].astype(object).where(bars[column].notna(), None)
    return CompletedBarsView(bars)


# Bar-contract columns a strategy may never overwrite from precompute().
BAR_COLUMNS = frozenset(
    {"timestamp", "symbol", "open", "high", "low", "close", "volume"}
)


def apply_precompute(strategy: Any, view: CompletedBarsView) -> CompletedBarsView:
    """Merge strategy-precomputed indicator columns onto the bars.

    The optional strategy hook ``precompute(frames: dict[symbol, DataFrame])
    -> dict[symbol, DataFrame]`` runs ONE vectorized pass instead of
    re-deriving indicators inside decide() every bar — per-bar pandas carries
    ~5ms of fixed overhead per rolling/ewm/concat call, which is what turns
    replays into minute-long crawls (measured live: ~30 bars/s for a 15-op
    decide()). Backtest calls this once over full history; the live driver
    calls it per tick over the bounded fetched window — rolling/shift columns
    are identical in both wherever the lookback fits inside ``warmup_bars``,
    so backtest/live parity holds. Transforms must be CAUSAL (rolling / shift
    / expanding — nothing that reads future rows); the returned frames align
    row-for-row with the input frames. Cross-symbol features (spreads,
    ratios) read several input frames and attach to the traded symbol's rows.
    Runs after the exogenous feature merge, so precompute() can consume those
    columns (e.g. a funding-rate feed) too.
    """
    precompute = getattr(strategy, "precompute", None)
    if not callable(precompute):
        return view
    bars = view.to_frame().sort_values(["timestamp", "symbol"]).reset_index(drop=True)
    frames = {
        str(symbol): bars[bars["symbol"] == symbol].reset_index(drop=True)
        for symbol in sorted(bars["symbol"].astype(str).unique())
    }
    derived = precompute(frames) or {}
    for symbol, feats in derived.items():
        base = frames.get(str(symbol))
        if feats is None or base is None:
            continue
        if len(feats) != len(base):
            raise ValueError(
                f"precompute() returned {len(feats)} rows for {symbol!r}; "
                f"expected {len(base)} (one per input bar, same order)"
            )
        mask = (bars["symbol"] == str(symbol)).to_numpy()
        for column in feats.columns:
            if column in BAR_COLUMNS:
                continue
            if column not in bars.columns:
                bars[column] = None
            values = feats[column].to_numpy()
            bars.loc[mask, column] = values
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
                "age_seconds": float((now - latest["timestamp"]).total_seconds()),
                "row_count": int(len(frame)),
            }
        )
    return summary
