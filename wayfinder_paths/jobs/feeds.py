"""Job-level feature feeds: fetch token prices and DeFi yields into a job's
feature store, declare them in the data contract (pinned, with cadence and
smoothing), keep them fresh from the wake path, and reconcile revisions.

A declaration lives in the revision-bound schema, so consuming a new feed
IS a strategy change: the workspace revision restamps and promotion re-gates
like any code edit. Feed data lives in state/features.jsonl, outside the
revision hash, so refreshes never re-gate. Reads collapse re-appended
(revised) rows to the last written one, so a source that restates a value
wins everywhere once the refresh re-appends it.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from wayfinder_paths.core.clients.DeltaLabClient import DELTA_LAB_CLIENT
from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.jobs.derived_features import MAX_APPEND_ROWS
from wayfinder_paths.jobs.execution.feature_feeds import (
    YIELD_CADENCE,
    YIELD_RETENTION_DAYS,
    feed_feature_name,
    fetch_token_price_rows,
    fetch_yield_rows,
    parse_feed_name,
    resolve_token_feed,
    resolve_yield_feed,
    token_feed_interval,
)
from wayfinder_paths.jobs.execution.features import (
    DEFAULT_FEATURES_PATH,
    SMOOTHING_METHODS,
    FeatureSpec,
    parse_feature_specs,
)
from wayfinder_paths.jobs.execution.job import _load_job_yaml
from wayfinder_paths.jobs.execution.primitives import (
    ExecutionSpec,
    bar_interval_seconds,
)
from wayfinder_paths.jobs.execution.validation import resolve_execution_spec
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.quant.pattern_match_context import SUPPORTED_INTERVALS

# A day of hourly rates is the number a lender quotes; a price is a price.
DEFAULT_YIELD_SMOOTHING: dict[str, Any] = {"method": "mean", "window": "24h"}
NO_SMOOTHING: dict[str, Any] = {"method": "none"}
DEFAULT_DAYS = 30.0
# Stale after three missed periods, and decide anyway: a stale yield must
# not silently halt a price strategy — the guard event says so.
STALE_PERIODS = 3
# A refresh re-fetches this many periods behind the newest stored row so a
# restated recent value is seen and reconciled.
WARMUP_PERIODS = 2
REVISION_TOLERANCE = 1e-9

SeriesKey = tuple[str, str | None]


# ---- the store --------------------------------------------------------------


def _iter_rows(features_path: Path):
    if not features_path.exists():
        return
    with features_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


def _key(row: Mapping[str, Any]) -> SeriesKey:
    symbol = row.get("symbol")
    return (str(row.get("name")), None if symbol is None else str(symbol))


def series_bounds(features_path: Path) -> dict[SeriesKey, tuple[str, str]]:
    """Oldest and newest stored stamp per (name, symbol) series, streamed —
    never a per-row key set, which at a live store's size is a memory hog."""
    bounds: dict[SeriesKey, tuple[str, str]] = {}
    for row in _iter_rows(features_path):
        key = _key(row)
        stamp = str(row.get("timestamp"))
        oldest, newest = bounds.get(key, (stamp, stamp))
        bounds[key] = (min(oldest, stamp), max(newest, stamp))
    return bounds


def series_values(
    features_path: Path, names: set[str]
) -> dict[SeriesKey, dict[str, Any]]:
    """Stored value per stamp for the named series, last write winning —
    what a refresh compares a re-fetched overlap against."""
    values: dict[SeriesKey, dict[str, Any]] = {}
    for row in _iter_rows(features_path):
        if str(row.get("name")) not in names:
            continue
        values.setdefault(_key(row), {})[str(row.get("timestamp"))] = row.get("value")
    return values


def _differs(stored: Any, fresh: Any) -> bool:
    try:
        old = float(stored)
        new = float(fresh)
    except (TypeError, ValueError):
        return stored != fresh
    if math.isnan(old) and math.isnan(new):
        return False
    return abs(new - old) > REVISION_TOLERANCE * max(1.0, abs(old), abs(new))


def append_feature_rows(
    root: Path, rows: Sequence[Mapping[str, Any]], *, reconcile: bool = False
) -> dict[str, Any]:
    """Append rows the store does not have: stamps newer than a series'
    newest or older than its oldest (a wider backfill never duplicates).
    With `reconcile`, a row inside the stored span whose value the source
    restated is appended again — same stamp, newer written_at — and reads
    take the last written row, so the revision wins everywhere."""
    features_path = root / DEFAULT_FEATURES_PATH
    features_path.parent.mkdir(parents=True, exist_ok=True)
    bounds = series_bounds(features_path)
    stored = (
        series_values(features_path, {str(row.get("name")) for row in rows})
        if reconcile
        else {}
    )
    written_at = pd.Timestamp.now(tz="UTC").isoformat()
    appended = 0
    revised = 0
    largest = 0.0
    newest = ""
    seen: set[tuple[SeriesKey, str]] = set()
    with features_path.open("a", encoding="utf-8") as handle:
        for row in rows:
            key = _key(row)
            stamp = str(row["timestamp"])
            if (key, stamp) in seen:
                continue
            seen.add((key, stamp))
            # Judged against the span as it was when the batch started: a
            # backfill of several older rows must not shrink its own window.
            span = bounds.get(key)
            fresh = span is None or stamp > span[1] or stamp < span[0]
            if not fresh:
                if not reconcile:
                    continue
                known = stored.get(key, {})
                if stamp not in known or not _differs(known[stamp], row["value"]):
                    continue
                revised += 1
                try:
                    largest = max(
                        largest, abs(float(row["value"]) - float(known[stamp]))
                    )
                except (TypeError, ValueError):
                    pass
            if appended + revised > MAX_APPEND_ROWS:
                raise ValueError(f"append cap {MAX_APPEND_ROWS} hit — narrow the fetch")
            handle.write(json.dumps({**row, "written_at": written_at}) + "\n")
            if fresh:
                appended += 1
            newest = max(newest, stamp)
    return {
        "rows_appended": appended,
        "revised_rows": revised,
        "largest_revision": largest,
        "newest_ts": newest,
        "features_path": str(features_path),
    }


# ---- the schema --------------------------------------------------------------


def declare_features(
    store: JobStore, job_id: str, entries: Sequence[Mapping[str, Any]]
) -> list[str]:
    """Add the entries whose name is not yet declared to
    execution_spec.data_contract.features — through the job model when the
    spec is embedded in job.yaml (the compiler rewrites execution_spec.json
    from it, so a file-only edit would be lost), else in the spec file."""
    job = store.load(job_id)
    if job.execution_spec:
        contract = job.execution_spec.setdefault("data_contract", {})
        features = contract.setdefault("features", [])
        added = _missing(features, entries)
        if added:
            features.extend(added)
            store.save(job)
        return [str(item["name"]) for item in added]
    target = store.job_dir(job_id) / "execution_spec.json"
    doc = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    contract = doc.setdefault("data_contract", {})
    features = contract.setdefault("features", [])
    added = _missing(features, entries)
    if added:
        features.extend(added)
        target.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return [str(item["name"]) for item in added]


def _missing(
    features: list[Any], entries: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    present = {str(item.get("name")) for item in features if isinstance(item, Mapping)}
    return [dict(entry) for entry in entries if str(entry["name"]) not in present]


def contract_entry(
    feed: Mapping[str, Any], *, cadence: str, smoothing: Mapping[str, Any]
) -> dict[str, Any]:
    seconds = bar_interval_seconds(cadence)
    if seconds is None:
        raise ValueError(f"cadence {cadence!r} is not an interval like 1h or 1d")
    entry = {
        "name": feed_feature_name(feed),
        "feed": dict(feed),
        "cadence": cadence,
        "smoothing": dict(smoothing),
        "max_age_seconds": STALE_PERIODS * seconds,
        "stale_policy": "decide_anyway",
    }
    FeatureSpec.from_dict(entry)  # the schema the readers will validate
    return entry


def parse_smoothing(text: str | None) -> dict[str, Any] | None:
    """`none`, `mean:24h`, `ewm:12h` → a smoothing mapping; None → caller's default."""
    if text is None or not str(text).strip():
        return None
    method, _, window = str(text).strip().partition(":")
    if method not in SMOOTHING_METHODS:
        raise ValueError(
            f"smoothing must be one of {SMOOTHING_METHODS}, got {method!r}"
        )
    if method == "none":
        return dict(NO_SMOOTHING)
    if bar_interval_seconds(window) is None:
        raise ValueError(f"smoothing {text!r} needs a window like mean:24h")
    return {"method": method, "window": window}


def dataset_days(root: Path) -> float | None:
    """The span the job's backtest dataset covers, from the dataset file's
    own metadata, so a feed backfill matches the candles by default."""
    path = root / "results" / "backtest" / "input_bars.json"
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    meta = doc.get("metadata") if isinstance(doc, dict) else None
    days = (meta or {}).get("days")
    return float(days) if days else None


# ---- the verbs ---------------------------------------------------------------


def _spec_contract(root: Path) -> dict[str, Any]:
    spec_data, _ = resolve_execution_spec(root, _load_job_yaml(root))
    return dict(spec_data.get("data_contract") or {})


def _coverage(
    names: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    metadata: Mapping[str, Any],
    days: float,
) -> dict[str, Any]:
    per_series = dict(metadata.get("per_series") or {})
    missing = sorted(name for name in names if not per_series.get(name))
    result: dict[str, Any] = {
        "rows_fetched": len(rows),
        "per_series": per_series,
        "missing": missing,
        "coverage_fraction": round((len(names) - len(missing)) / max(1, len(names)), 3),
        "days_requested": float(days),
        "errors": dict(metadata.get("errors") or {}),
    }
    stamps = sorted(str(row["timestamp"]) for row in rows)
    warnings: list[str] = []
    if stamps:
        first, last = pd.Timestamp(stamps[0]), pd.Timestamp(stamps[-1])
        received = round((last - first).total_seconds() / 86_400, 1)
        result.update(
            {"first_ts": str(first), "last_ts": str(last), "days_received": received}
        )
        if received < 0.9 * float(days):
            warnings.append(f"history covers {received} of the {days} days requested")
    if missing:
        warnings.append("no rows for: " + ", ".join(missing))
    for name, error in result["errors"].items():
        warnings.append(f"{name}: {error}")
    if warnings:
        result["warning"] = "; ".join(warnings)
    return result


def fetch_token_features(
    job_id: str,
    *,
    token_ids: Sequence[str],
    interval: str | None = None,
    days: float | None = None,
    store: JobStore | None = None,
    client: Any = None,
    resolver: Any = None,
) -> dict[str, Any]:
    """Fetch an on-chain token's USD price history into the job's feature
    store as `token_price:<token_id>` and declare it, pinned to the resolved
    chain and address, at the largest candle interval that fits the bars.
    Default history = the dataset's own span, so coverage matches the candles."""
    if not token_ids:
        raise ValueError("fetch_token_features needs at least one token id")
    store = store or JobStore()
    root = store.job_dir(job_id)
    contract = _spec_contract(root)
    interval = str(
        interval or token_feed_interval(contract.get("bar_interval") or "5m")
    )
    if interval not in SUPPORTED_INTERVALS:
        raise ValueError(
            f"interval must be one of {SUPPORTED_INTERVALS}, got {interval!r}"
        )
    span = float(days) if days else (dataset_days(root) or DEFAULT_DAYS)
    resolver_kwargs = {"resolver": resolver} if resolver is not None else {}

    async def _resolve() -> list[dict[str, Any]]:
        return [
            await resolve_token_feed(
                {
                    "kind": "token_price",
                    "token_id": str(token_id),
                    "interval": interval,
                },
                **resolver_kwargs,
            )
            for token_id in token_ids
        ]

    feeds = asyncio.run(_resolve())
    rows, metadata = asyncio.run(
        fetch_token_price_rows(feeds, days=span, client=client or TOKEN_CLIENT)
    )
    written = append_feature_rows(root, rows)
    names = [feed_feature_name(feed) for feed in feeds]
    entries = [
        contract_entry(feed, cadence=interval, smoothing=NO_SMOOTHING)
        for feed, name in zip(feeds, names, strict=True)
        if metadata["per_series"].get(name)
    ]
    declared = declare_features(store, job_id, entries)
    return {
        **_coverage(names, rows, metadata, span),
        **written,
        "feature_declared_now": declared,
        "interval": interval,
        "metadata": metadata,
    }


def fetch_yield_features(
    job_id: str,
    *,
    feeds: Sequence[str],
    days: float | None = None,
    smoothing: str | Mapping[str, Any] | None = None,
    store: JobStore | None = None,
    client: Any = None,
) -> dict[str, Any]:
    """Fetch DeFi yield history by feed name into the job's feature store and
    declare each feed pinned to the service's ids, with its observed cadence
    and the smoothing (default: a trailing day). History is bounded by the
    service's retention; a longer dataset carries None before that."""
    if not feeds:
        raise ValueError("fetch_yield_features needs at least one feed name")
    store = store or JobStore()
    root = store.job_dir(job_id)
    parsed = [parse_feed_name(str(name)) for name in feeds]
    for feed in parsed:
        if feed["kind"] == "token_price":
            raise ValueError("token prices go through fetch_token_features")
    dataset_span = dataset_days(root)
    span = (
        float(days) if days else min(dataset_span or DEFAULT_DAYS, YIELD_RETENTION_DAYS)
    )
    smooth = (
        parse_smoothing(smoothing)
        if isinstance(smoothing, str) or smoothing is None
        else dict(smoothing)
    ) or DEFAULT_YIELD_SMOOTHING
    delta_client = client or DELTA_LAB_CLIENT

    async def _resolve() -> list[dict[str, Any]]:
        return [await resolve_yield_feed(feed, client=delta_client) for feed in parsed]

    resolved = asyncio.run(_resolve())
    rows, metadata = asyncio.run(
        fetch_yield_rows(resolved, days=span, client=delta_client)
    )
    written = append_feature_rows(root, rows)
    names = [feed_feature_name(feed) for feed in resolved]
    entries = [
        contract_entry(
            feed,
            cadence=str((metadata.get("cadence") or {}).get(name) or YIELD_CADENCE),
            smoothing=smooth,
        )
        for feed, name in zip(resolved, names, strict=True)
        if metadata["per_series"].get(name)
    ]
    declared = declare_features(store, job_id, entries)
    result = {
        **_coverage(names, rows, metadata, span),
        **written,
        "feature_declared_now": declared,
        "smoothing": smooth,
        "metadata": metadata,
    }
    if dataset_span and dataset_span > YIELD_RETENTION_DAYS:
        note = (
            f"yield history is retained for about {YIELD_RETENTION_DAYS} days; the dataset "
            f"spans {dataset_span:.0f} — earlier bars carry None"
        )
        result["warning"] = (
            f"{result['warning']}; {note}" if result.get("warning") else note
        )
    return result


def refresh_declared_feeds(
    job_id: str,
    *,
    store: JobStore | None = None,
    token_client: Any = None,
    delta_client: Any = None,
) -> dict[str, Any]:
    """Advance every declared feed from the newest stored row (minus a
    warmup, so a restated recent value is reconciled) and record what
    changed. Healthy feeds are appended before a failed one raises, so the
    wake's degradation counter sees the failure without losing the rest."""
    store = store or JobStore()
    root = store.job_dir(job_id)
    spec = ExecutionSpec.from_dict(
        resolve_execution_spec(root, _load_job_yaml(root))[0]
    )
    specs = [item for item in parse_feature_specs(spec) if item.feed]
    empty = {
        "feeds": 0,
        "rows_appended": 0,
        "revised_rows": 0,
        "largest_revision": 0.0,
        "newest_feature_ts": "",
        "errors": {},
    }
    if not specs:
        return empty
    bounds = series_bounds(root / DEFAULT_FEATURES_PATH)
    span = dataset_days(root) or DEFAULT_DAYS
    totals = dict(empty, feeds=len(specs))
    revised_feeds: list[str] = []
    for item in specs:
        feed = dict(item.feed or {})
        cadence = (
            item.cadence_seconds
            or bar_interval_seconds(feed.get("interval"))
            or bar_interval_seconds(YIELD_CADENCE)
            or 3600
        )
        stored = bounds.get((item.name, None))
        since = (
            pd.Timestamp(stored[1]) - pd.Timedelta(seconds=WARMUP_PERIODS * cadence)
            if stored
            else None
        )
        if feed.get("kind") == "token_price":
            rows, metadata = asyncio.run(
                fetch_token_price_rows(
                    [feed], days=span, since=since, client=token_client or TOKEN_CLIENT
                )
            )
        else:
            rows, metadata = asyncio.run(
                fetch_yield_rows(
                    [feed],
                    days=span,
                    since=since,
                    client=delta_client or DELTA_LAB_CLIENT,
                )
            )
        error = (metadata.get("errors") or {}).get(item.name)
        if error:
            totals["errors"][item.name] = error
            continue
        written = append_feature_rows(root, rows, reconcile=True)
        totals["rows_appended"] += written["rows_appended"]
        totals["revised_rows"] += written["revised_rows"]
        totals["largest_revision"] = max(
            totals["largest_revision"], written["largest_revision"]
        )
        totals["newest_feature_ts"] = max(
            totals["newest_feature_ts"], written["newest_ts"]
        )
        if written["revised_rows"]:
            revised_feeds.append(item.name)
    if totals["revised_rows"]:
        store.append_journal(
            job_id,
            {
                "type": "feed_revised",
                "rows": totals["revised_rows"],
                "largest_change": totals["largest_revision"],
                "feeds": revised_feeds,
            },
        )
    if totals["errors"]:
        raise RuntimeError(
            "feed refresh failed: "
            + "; ".join(
                f"{name}: {message}" for name, message in totals["errors"].items()
            )
        )
    return totals
