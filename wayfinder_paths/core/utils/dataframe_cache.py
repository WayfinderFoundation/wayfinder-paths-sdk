"""Disk-cached async fetchers for DataFrames.

`@disk_cached(namespace=...)` memoises an async function whose return value is a
pandas DataFrame (or Series). Subsequent calls with the same bound arguments
read from a local pickle file instead of awaiting the wrapped function.

Use for expensive remote data fetches: CCXT pagination, Delta Lab timeseries,
funding-rate history, etc. Especially valuable for Phase 5a grid search where
many cells fetch the same multi-year window from the same exchange.

Cache files are pickled (no extra dependency). Pickle is Python-only but
losslessly preserves DatetimeIndex, tz, sorted-ness, and column dtypes.

Default cache root resolution order:
1. `cache_dir` decorator argument
2. `WAYFINDER_CACHE_DIR` environment variable
3. `<cwd>/.wayfinder/cache`
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import pickle
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


_DEFAULT_END_ARG_NAMES = ("end", "end_date", "end_ts", "as_of")


def _resolve_cache_root(cache_dir: str | Path | None) -> Path:
    if cache_dir is not None:
        return Path(cache_dir).expanduser()
    env = os.environ.get("WAYFINDER_CACHE_DIR")
    if env:
        return Path(env).expanduser()
    return Path.cwd() / ".wayfinder" / "cache"


def _normalize(value: Any) -> Any:
    """Make `value` hash-stable: sort lists/sets, ISO-format datetimes."""
    if isinstance(value, (list, tuple, set, frozenset)):
        return sorted(
            [_normalize(v) for v in value], key=lambda x: json.dumps(x, default=str)
        )
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in sorted(value.items())}
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    return value


def _bound_args(fn: Callable, args: tuple, kwargs: dict) -> dict[str, Any]:
    sig = inspect.signature(fn)
    bound = sig.bind_partial(*args, **kwargs)
    bound.apply_defaults()
    return dict(bound.arguments)


def _make_key(
    fn_name: str,
    bound: dict[str, Any],
    key_args: Iterable[str] | None,
) -> tuple[str, dict[str, Any]]:
    if key_args is not None:
        bound = {k: bound[k] for k in key_args if k in bound}
    normalized = {k: _normalize(v) for k, v in bound.items()}
    payload = {"fn": fn_name, "args": normalized}
    blob = json.dumps(payload, sort_keys=True, default=str)
    digest = hashlib.sha256(blob.encode()).hexdigest()[:16]
    return digest, payload


def _detect_end_arg(bound: dict[str, Any], end_arg: str | None) -> Any | None:
    if end_arg:
        return bound.get(end_arg)
    for name in _DEFAULT_END_ARG_NAMES:
        if name in bound:
            return bound[name]
    return None


def _is_stale(end_value: Any, staleness: timedelta) -> bool:
    """Return True if `end_value` is too close to now to safely cache."""
    if end_value is None:
        return False
    try:
        if isinstance(end_value, str):
            end_dt = datetime.fromisoformat(end_value.replace("Z", "+00:00"))
        elif isinstance(end_value, pd.Timestamp):
            end_dt = end_value.to_pydatetime()
        elif isinstance(end_value, datetime):
            end_dt = end_value
        else:
            return False
    except (ValueError, TypeError):
        return False
    now = datetime.now(end_dt.tzinfo) if end_dt.tzinfo else datetime.now()
    return end_dt >= now - staleness


def _should_skip_caching(result: Any) -> bool:
    if result is None:
        return True
    if isinstance(result, pd.DataFrame) and result.empty:
        return True
    if isinstance(result, pd.Series) and result.empty:
        return True
    return False


def disk_cached(
    *,
    namespace: str,
    cache_dir: str | Path | None = None,
    end_arg: str | None = None,
    staleness: timedelta = timedelta(hours=2),
    key_args: Iterable[str] | None = None,
) -> Callable:
    """Decorate an async fn so its DataFrame return value is cached to disk.

    Args:
        namespace: Subdirectory under the cache root. Keep distinct namespaces
            for distinct data sources so keys never collide.
        cache_dir: Optional explicit cache root. If None, uses
            `WAYFINDER_CACHE_DIR` env var, else `<cwd>/.wayfinder/cache`.
        end_arg: Name of the wrapped fn's argument representing the window's
            end timestamp. Used for the staleness check. If None, the decorator
            auto-detects among {end, end_date, end_ts, as_of}.
        staleness: If the end-of-window arg is within `staleness` of now,
            results are returned but NOT written to disk (avoids stamping a
            half-finished window). Reads honour cache unconditionally.
        key_args: Optional allowlist of bound-arg names to include in the cache
            key. Default: all bound args. Use this to exclude args that don't
            affect the result (e.g. verbosity flags).

    Per-call controls:
        - Set env var `WAYFINDER_CACHE_DISABLE=1` to bypass all caching.
        - Pass `_cache=False` as a kwarg to the wrapped fn to bypass for one
          call. The kwarg is stripped before the underlying fn is invoked.
    """

    def decorator(fn: Callable) -> Callable:
        if not asyncio.iscoroutinefunction(fn):
            raise TypeError(
                f"disk_cached only supports async functions; got {fn.__qualname__}"
            )

        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            bypass = kwargs.pop("_cache", True) is False
            if bypass or os.environ.get("WAYFINDER_CACHE_DISABLE") == "1":
                return await fn(*args, **kwargs)

            bound = _bound_args(fn, args, kwargs)
            key, payload = _make_key(fn.__qualname__, bound, key_args)

            root = _resolve_cache_root(cache_dir) / namespace
            cache_path = root / f"{key}.pkl"
            sidecar_path = root / f"{key}.json"

            if cache_path.exists():
                try:
                    with cache_path.open("rb") as fh:
                        return pickle.load(fh)
                except (pickle.PickleError, EOFError, OSError) as exc:
                    logger.warning(
                        "disk_cached: failed to read %s (%s); re-fetching",
                        cache_path,
                        exc,
                    )

            result = await fn(*args, **kwargs)

            if _should_skip_caching(result):
                return result

            end_value = _detect_end_arg(bound, end_arg)
            if _is_stale(end_value, staleness):
                logger.debug(
                    "disk_cached: skipping write for %s (end too recent)",
                    cache_path,
                )
                return result

            try:
                root.mkdir(parents=True, exist_ok=True)
                tmp_path = cache_path.with_suffix(f".pkl.tmp.{os.getpid()}")
                with tmp_path.open("wb") as fh:
                    pickle.dump(result, fh, protocol=pickle.HIGHEST_PROTOCOL)
                os.replace(tmp_path, cache_path)
                with sidecar_path.open("w") as fh:
                    json.dump(payload, fh, default=str, indent=2)
            except OSError as exc:
                logger.warning(
                    "disk_cached: failed to write %s (%s); returning uncached result",
                    cache_path,
                    exc,
                )

            return result

        return wrapper

    return decorator


def cache_stats(
    namespace: str | None = None,
    cache_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Return basic stats about the cache (counts, total size, mtime range)."""
    root = _resolve_cache_root(cache_dir)
    if namespace:
        root = root / namespace
    if not root.exists():
        return {"root": str(root), "files": 0, "size_bytes": 0}
    files = list(root.rglob("*.pkl"))
    sizes = [f.stat().st_size for f in files]
    mtimes = [f.stat().st_mtime for f in files]
    return {
        "root": str(root),
        "files": len(files),
        "size_bytes": sum(sizes),
        "oldest_mtime": min(mtimes) if mtimes else None,
        "newest_mtime": max(mtimes) if mtimes else None,
    }


def cache_clear(
    namespace: str | None = None,
    older_than: timedelta | None = None,
    cache_dir: str | Path | None = None,
) -> int:
    """Delete cache entries. Returns count deleted.

    Args:
        namespace: Restrict to a single namespace subdir. None = all.
        older_than: Only delete entries with mtime older than now - older_than.
            None = delete all matched.
        cache_dir: Cache root override.
    """
    root = _resolve_cache_root(cache_dir)
    if namespace:
        root = root / namespace
    if not root.exists():
        return 0
    cutoff = None
    if older_than is not None:
        cutoff = datetime.now().timestamp() - older_than.total_seconds()
    deleted = 0
    for pkl_path in root.rglob("*.pkl"):
        if cutoff is not None and pkl_path.stat().st_mtime >= cutoff:
            continue
        pkl_path.unlink(missing_ok=True)
        pkl_path.with_suffix(".json").unlink(missing_ok=True)
        deleted += 1
    return deleted
