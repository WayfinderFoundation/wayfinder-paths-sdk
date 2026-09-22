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
    bar_interval_seconds,
)

DEFAULT_FEATURES_PATH = "state/features.jsonl"
# Feeds the fetch verbs know how to pull and refresh; the `feed` mapping on a
# declared feature pins the resolved source ids so nothing re-searches.
FEED_KINDS: tuple[str, ...] = (
    "token_price",
    "lend_supply_apr",
    "lend_borrow_apr",
    "yield_apy",
    "pendle_implied_apy",
    "boros_fixed_rate",
)
SMOOTHING_METHODS: tuple[str, ...] = ("none", "mean", "ewm")
# A gap wider than this many cadence periods is reported, never silently held.
GAP_PERIODS = 2


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
    # Pinned source ids written by the fetch verbs (see FEED_KINDS).
    feed: Mapping[str, Any] | None = None
    # The feed's native period ("5m", "1h", "1d"): rows are snapped to this
    # grid before the as-of merge, and gaps are measured against it.
    cadence: str | None = None
    # {"method": "none" | "mean" | "ewm", "window": "<interval>"} in feed time,
    # applied identically by the backtest loader and the live driver.
    smoothing: Mapping[str, Any] | None = None

    @property
    def column_name(self) -> str:
        return self.column or self.name

    @property
    def raw_column_name(self) -> str:
        return f"{self.column_name}__raw"

    @property
    def cadence_seconds(self) -> int | None:
        return bar_interval_seconds(self.cadence) if self.cadence else None

    @property
    def smoothing_method(self) -> str:
        return str((self.smoothing or {}).get("method") or "none")

    @property
    def smoothing_seconds(self) -> int | None:
        window = (self.smoothing or {}).get("window")
        return bar_interval_seconds(window) if window else None

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
        feed = data.get("feed")
        feed_map = dict(feed) if isinstance(feed, Mapping) and feed else None
        if feed_map is not None and feed_map.get("kind") not in FEED_KINDS:
            raise ValueError(f"feature {name!r}: feed.kind must be one of {FEED_KINDS}")
        cadence = str(data["cadence"]) if data.get("cadence") else None
        cadence_seconds = bar_interval_seconds(cadence) if cadence else None
        if cadence is not None and cadence_seconds is None:
            raise ValueError(
                f"feature {name!r}: cadence {cadence!r} is not an interval like 1h or 1d"
            )
        smoothing = data.get("smoothing")
        smoothing_map: dict[str, Any] | None = None
        if isinstance(smoothing, Mapping) and smoothing:
            method = str(smoothing.get("method") or "none")
            if method not in SMOOTHING_METHODS:
                raise ValueError(
                    f"feature {name!r}: smoothing.method must be one of {SMOOTHING_METHODS}"
                )
            smoothing_map = {"method": method}
            if method != "none":
                if cadence_seconds is None:
                    raise ValueError(f"feature {name!r}: smoothing needs a cadence")
                window = smoothing.get("window")
                window_seconds = bar_interval_seconds(window) if window else None
                if window_seconds is None or window_seconds < cadence_seconds:
                    raise ValueError(
                        f"feature {name!r}: smoothing.window must be an interval of "
                        "at least the cadence"
                    )
                smoothing_map["window"] = str(window)
        return cls(
            name=name,
            source=str(data.get("source") or "file"),
            path=_contained_feature_path(name, data.get("path")),
            max_age_seconds=int(raw_age) if raw_age is not None else None,
            stale_policy=policy,
            column=str(data["column"]) if data.get("column") else None,
            feed=feed_map,
            cadence=cadence,
            smoothing=smoothing_map,
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


# Single-entry parse cache keyed on file identity (path, mtime_ns, size) plus
# the names it was parsed for. The live driver calls load_feature_rows every
# tick; without this each 5-minute tick re-parsed the full store (95MB /
# 600k+ lines observed live) once per declared feature. Any append
# invalidates via mtime/size. A request for a SUBSET of the cached names is a
# hit: callers index `columns[name]`, so the superset dict is transparent —
# the sync snapshot's per-feature revised_row_count calls reuse the one parse
# summarize_features already paid for instead of re-reading the store K times.
_FEATURE_FILE_CACHE: dict[str, Any] = {}


def _parse_feature_file(path: Path, names: set[str]) -> dict[str, dict[str, list]]:
    """ONE streaming pass over the jsonl store collecting column lists for
    every requested name — the store is parsed once, not once per spec."""
    stat = path.stat()
    identity = (str(path), stat.st_mtime_ns, stat.st_size)
    if (
        _FEATURE_FILE_CACHE.get("identity") == identity
        and names <= _FEATURE_FILE_CACHE["names"]
    ):
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
    _FEATURE_FILE_CACHE["identity"] = identity
    _FEATURE_FILE_CACHE["names"] = set(names)
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
        # Stable sort keeps file (append) order for equal stamps, so a row a
        # refresh re-appended with a revised value wins over the original.
        frame = frame.dropna(subset=["timestamp"]).sort_values(
            "timestamp", kind="stable"
        )
        frame = frame.drop_duplicates(subset=["timestamp", "symbol"], keep="last")
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
    columns: list[str] = []
    for spec in specs:
        feature = frames.get(spec.name)
        if feature is None or feature.empty:
            for column in _spec_columns(spec):
                bars[column] = None
                columns.append(column)
            continue
        for column, sub_frame in _feature_columns(spec, feature):
            columns.append(column)
            per_symbol = sub_frame["symbol"].notna().any()
            if per_symbol:
                sub = sub_frame.dropna(subset=["symbol"]).rename(
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
                sub = sub_frame.rename(columns={"value": column})
                merged = pd.merge_asof(
                    bars.sort_values("timestamp"),
                    sub[["timestamp", column]].sort_values("timestamp"),
                    on="timestamp",
                    direction="backward",
                )
            bars = merged
    for column in columns:
        if column in bars.columns:
            bars[column] = bars[column].astype(object).where(bars[column].notna(), None)
    return CompletedBarsView(bars)


def _spec_columns(spec: FeatureSpec) -> list[str]:
    if spec.cadence_seconds is not None and spec.smoothing_method != "none":
        return [spec.column_name, spec.raw_column_name]
    return [spec.column_name]


def _feature_columns(
    spec: FeatureSpec, feature: pd.DataFrame
) -> list[tuple[str, pd.DataFrame]]:
    """The (column, rows) pairs a spec contributes to the bars: the raw rows
    for a feature without a cadence; the gridded rows for one with a cadence;
    the smoothed series plus a `<column>__raw` sibling when smoothing is on.
    Everything here is computed from feed rows alone, never from bars, so the
    backtest loader and the live driver produce identical columns."""
    cadence = spec.cadence_seconds
    if cadence is None:
        return [(spec.column_name, feature)]
    gridded = grid_feature_rows(feature, cadence)
    if spec.smoothing_method == "none":
        return [(spec.column_name, gridded)]
    return [
        (spec.column_name, smooth_feature_rows(gridded, spec)),
        (spec.raw_column_name, gridded),
    ]


def grid_feature_rows(feature: pd.DataFrame, cadence_seconds: int) -> pd.DataFrame:
    """One row per cadence period and symbol — the last observation in the
    period wins — with its real observation time kept, so a jittered
    12:00:07 snapshot still becomes visible only to bars at or after it (no
    lookahead) while duplicates inside a period collapse and two feeds of
    different cadence line up on the bars through the same as-of merge."""
    frame = feature.copy()
    bucket = frame["timestamp"].dt.floor(f"{int(cadence_seconds)}s")
    frame = (
        frame.assign(_bucket=bucket)
        .sort_values("timestamp", kind="stable")
        .drop_duplicates(subset=["_bucket", "symbol"], keep="last")
        .drop(columns="_bucket")
    )
    return frame.reset_index(drop=True)


def smooth_feature_rows(gridded: pd.DataFrame, spec: FeatureSpec) -> pd.DataFrame:
    """Trailing smoothing in feed time over the declared window: `mean` is a
    rolling mean over the window, `ewm` an exponential mean with the window as
    half-life. Both see only rows at or before each point."""
    window = pd.Timedelta(seconds=int(spec.smoothing_seconds or 0))
    frame = gridded.copy()
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    parts: list[pd.DataFrame] = []
    for _symbol, group in frame.groupby(
        frame["symbol"].astype(object), dropna=False, sort=False
    ):
        ordered = group.sort_values("timestamp", kind="stable")
        series = ordered.set_index("timestamp")["value"]
        if spec.smoothing_method == "mean":
            smoothed = series.rolling(window, min_periods=1).mean()
        else:
            smoothed = series.ewm(halflife=window, times=series.index).mean()
        parts.append(ordered.assign(value=smoothed.to_numpy()))
    if not parts:
        return frame
    return (
        pd.concat(parts).sort_values("timestamp", kind="stable").reset_index(drop=True)
    )


def feature_gaps(
    frame: pd.DataFrame, cadence_seconds: int | None
) -> dict[str, Any] | None:
    """Gaps wider than GAP_PERIODS cadence periods within a feature's rows,
    per symbol series: count, the largest, and when the first one opened."""
    if cadence_seconds is None or frame is None or len(frame) < 2:
        return None
    threshold = GAP_PERIODS * int(cadence_seconds)
    count = 0
    largest = 0.0
    first_at: pd.Timestamp | None = None
    for _symbol, group in frame.groupby(
        frame["symbol"].astype(object), dropna=False, sort=False
    ):
        stamps = group["timestamp"].sort_values(kind="stable").reset_index(drop=True)
        deltas = stamps.diff().dt.total_seconds()
        for position in deltas[deltas > threshold].index:
            count += 1
            gap = float(deltas.iloc[position])
            largest = max(largest, gap)
            opened = stamps.iloc[position - 1] + pd.Timedelta(
                seconds=int(cadence_seconds)
            )
            if first_at is None or opened < first_at:
                first_at = opened
    if count == 0:
        return {"count": 0, "largest_seconds": 0.0, "first_at": None}
    return {
        "count": count,
        "largest_seconds": largest,
        "first_at": first_at.isoformat() if first_at is not None else None,
    }


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
        frame = frames.get(spec.name)
        cadence = spec.cadence_seconds
        if cadence is not None and frame is not None and len(frame) >= 2:
            # A hole in a cadenced feed is telemetry, not a halt: the tick
            # still decides under its stale policy, and the hole is visible.
            gap = float(
                (
                    frame["timestamp"].iloc[-1] - frame["timestamp"].iloc[-2]
                ).total_seconds()
            )
            if gap > GAP_PERIODS * cadence:
                guard_events.append(
                    {
                        "kind": "feed_gap",
                        "feature": spec.name,
                        "gap_seconds": gap,
                        "cadence_seconds": cadence,
                        "timestamp": now.isoformat(),
                    }
                )
        if spec.max_age_seconds is None:
            continue
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


# The sync snapshot only needs the latest value per feature plus recent
# cadence gaps; without a window a multi-month store (77MB observed live)
# became per-feature DataFrames on every sync. _trim_to_window keeps the
# as-of anchor row per series, so latest_value/latest_timestamp/age_seconds/
# available are unchanged; gaps and row_count describe the trailing window.
FEATURE_SUMMARY_LOOKBACK = pd.Timedelta(days=30)


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
    frames = load_feature_rows(
        [Path(root)], specs, window=(now - FEATURE_SUMMARY_LOOKBACK, now)
    )
    summary: list[dict[str, Any]] = []
    for item in specs:
        frame = frames.get(item.name)
        if frame is None or frame.empty:
            summary.append({"name": item.name, "available": False})
            continue
        latest = frame.iloc[-1]
        entry: dict[str, Any] = {
            "name": item.name,
            "available": True,
            "latest_value": latest["value"],
            "latest_timestamp": latest["timestamp"].isoformat(),
            "age_seconds": float((now - latest["timestamp"]).total_seconds()),
            "row_count": int(len(frame)),
        }
        if item.cadence is not None:
            entry["cadence"] = item.cadence
            entry["smoothing"] = dict(item.smoothing) if item.smoothing else None
            entry["gaps"] = feature_gaps(frame, item.cadence_seconds)
            entry["revised_rows"] = revised_row_count(Path(root) / item.path, item.name)
        summary.append(entry)
    return summary


def revised_row_count(path: Path, name: str) -> int:
    """Rows a refresh re-appended with a revised value: the store's raw
    (timestamp, symbol) pairs for the feature beyond the unique ones."""
    if not path.exists():
        return 0
    columns = _parse_feature_file(path, {name}).get(name)
    if not columns:
        return 0
    pairs = list(zip(columns["timestamp"], columns["symbol"], strict=True))
    return len(pairs) - len(set(pairs))
