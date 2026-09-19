"""Feature feeds: on-chain token prices and DeFi yields as feature rows.

Pure I/O, no job store: each fetcher returns rows in the features.jsonl
shape ({timestamp, name, value, symbol: None} — feeds are global, every
traded symbol sees them) plus provenance metadata, the way the funding feed
in ccxt_feed does. Per-series failures land in metadata["errors"] so one
bad token id never costs the others their history.

One name grammar is the identity everywhere (the data contract, the fetch
verbs, ctx.defi_yield, the dry-run marks):

    token_price:<token_id>
    lend_supply_apr:<venue>:<symbol>[:<market_external_id>]
    lend_borrow_apr:<venue>:<symbol>[:<market_external_id>]
    yield_apy:<symbol>
    pendle_implied_apy:<venue>:<market_id>
    boros_fixed_rate:<venue>:<market_id>

Token candles are labelled by the source at the candle OPEN, so rows are
relabelled to the candle close and the in-progress candle is dropped — the
same convention
as the bars. Yield snapshots keep their observation time.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from functools import partial
from typing import Any

import pandas as pd

from wayfinder_paths.core.clients.delta_lab_types import DeltaLabAPIError
from wayfinder_paths.core.clients.DeltaLabClient import DELTA_LAB_CLIENT
from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.jobs.execution.ccxt_feed import BAR_CLOSE_LABEL
from wayfinder_paths.jobs.execution.features import FEED_KINDS
from wayfinder_paths.jobs.execution.primitives import bar_interval_seconds
from wayfinder_paths.jobs.execution.token_bars import (
    fetch_token_bar_window,
)
from wayfinder_paths.jobs.execution.token_bars import (
    resolve_token_feed as resolve_token_feed,  # re-exported for jobs.feeds
)
from wayfinder_paths.quant.pattern_match_context import INTERVAL_MS, SUPPORTED_INTERVALS

# The yield service keeps about seven months of hourly history.
YIELD_RETENTION_DAYS = 211
YIELD_CADENCE = "1h"
_RETRIES = 3
_BACKOFF_S = 2.0
LENDING_KINDS = frozenset({"lend_supply_apr", "lend_borrow_apr"})
MARKET_KINDS = frozenset({"pendle_implied_apy", "boros_fixed_rate"})
YIELD_KINDS = LENDING_KINDS | MARKET_KINDS | {"yield_apy"}
# Which column of the yield series a kind reads.
YIELD_COLUMNS: dict[str, str] = {
    "lend_supply_apr": "supply_apr",
    "lend_borrow_apr": "borrow_apr",
    "yield_apy": "apy_base",
    "pendle_implied_apy": "implied_apy",
    "boros_fixed_rate": "fixed_rate_mark",
}


# ---- names -------------------------------------------------------------------


def parse_feed_name(name: str) -> dict[str, Any]:
    """The feed mapping a feature name denotes; ValueError on anything else."""
    parts = str(name).split(":")
    kind, rest = parts[0], parts[1:]
    if kind not in FEED_KINDS:
        raise ValueError(f"unknown feed kind {kind!r} in {name!r}; kinds: {FEED_KINDS}")
    if kind == "token_price":
        if len(rest) != 1 or not rest[0]:
            raise ValueError(f"{name!r}: expected token_price:<token_id>")
        return {"kind": kind, "token_id": rest[0]}
    if kind in LENDING_KINDS:
        if len(rest) not in (2, 3) or not all(rest):
            raise ValueError(f"{name!r}: expected {kind}:<venue>:<symbol>[:<market>]")
        feed: dict[str, Any] = {"kind": kind, "venue": rest[0], "symbol": rest[1]}
        if len(rest) == 3:
            feed["market"] = rest[2]
        return feed
    if kind == "yield_apy":
        if len(rest) != 1 or not rest[0]:
            raise ValueError(f"{name!r}: expected yield_apy:<symbol>")
        return {"kind": kind, "symbol": rest[0]}
    if len(rest) != 2 or not all(rest) or not rest[1].isdigit():
        raise ValueError(f"{name!r}: expected {kind}:<venue>:<market_id>")
    return {"kind": kind, "venue": rest[0], "market_id": int(rest[1])}


def feed_feature_name(feed: Mapping[str, Any]) -> str:
    kind = str(feed.get("kind"))
    if kind == "token_price":
        return f"token_price:{feed['token_id']}"
    if kind in LENDING_KINDS:
        base = f"{kind}:{feed['venue']}:{feed['symbol']}"
        return f"{base}:{feed['market']}" if feed.get("market") else base
    if kind == "yield_apy":
        return f"yield_apy:{feed['symbol']}"
    if kind in MARKET_KINDS:
        return f"{kind}:{feed['venue']}:{int(feed['market_id'])}"
    raise ValueError(f"unknown feed kind {kind!r}")


def token_feed_interval(bar_interval: Any) -> str:
    """The largest candle interval the token feed supports that does not
    exceed the strategy's bar interval, so a feed never carries a value the
    bar it merges onto could not have seen."""
    seconds = bar_interval_seconds(bar_interval)
    if seconds is None:
        raise ValueError(
            f"bar interval {bar_interval!r} is not an interval like 5m or 1h"
        )
    fitting = [
        interval
        for interval in SUPPORTED_INTERVALS
        if INTERVAL_MS[interval] <= seconds * 1000
    ]
    if not fitting:
        return SUPPORTED_INTERVALS[0]
    return max(fitting, key=lambda interval: INTERVAL_MS[interval])


# ---- resolution (pins the source ids once) -----------------------------------


async def resolve_yield_feed(
    feed: Mapping[str, Any], *, client: Any = DELTA_LAB_CLIENT
) -> dict[str, Any]:
    """Pin the yield service's ids for a feed: the asset for yield_apy, the
    (market, asset) pair for lending. Pendle and Boros names already carry
    the market id. A no-op when pinned. Resolves by asset id, never by a
    basis symbol, so the basis-root rule of the screeners cannot bite."""
    resolved = dict(feed)
    kind = str(resolved["kind"])
    if kind in MARKET_KINDS:
        resolved["market_id"] = int(resolved["market_id"])
        return resolved
    if kind == "yield_apy":
        if resolved.get("asset_id"):
            return resolved
        resolved["asset_id"] = await _asset_id(client, str(resolved["symbol"]))
        return resolved
    if resolved.get("market_id") and resolved.get("asset_id"):
        return resolved
    asset_id = await _asset_id(client, str(resolved["symbol"]))
    venue = str(resolved["venue"])
    page = await client.screen_lending(asset_ids=[asset_id], venue=venue, limit=200)
    rows = [
        row
        for row in (page.get("data") or page.get("items") or [])
        if str(row.get("venue_name")) == venue
    ]
    market = resolved.get("market")
    if market:
        rows = [
            row
            for row in rows
            if str(market)
            in {str(row.get("market_external_id")), str(row.get("market_id"))}
        ]
    if not rows:
        everywhere = await client.screen_lending(asset_ids=[asset_id], limit=200)
        venues = sorted(
            {
                str(row.get("venue_name"))
                for row in (everywhere.get("data") or everywhere.get("items") or [])
            }
        )
        raise ValueError(
            f"{feed_feature_name(feed)!r}: no lending market for {resolved['symbol']} on "
            f"{venue}; venues carrying it: {venues or 'none'}"
        )
    if len(rows) > 1:
        choices = [
            f"{row.get('market_external_id')} ({row.get('market_label')})"
            for row in rows
        ]
        raise ValueError(
            f"{feed_feature_name(feed)!r}: {len(rows)} lending markets match; add "
            f":<market> to the name — one of {choices}"
        )
    row = rows[0]
    resolved["market_id"] = int(row["market_id"])
    resolved["asset_id"] = int(row["asset_id"])
    if row.get("chain_id") is not None:
        resolved["chain_id"] = int(row["chain_id"])
    return resolved


async def _asset_id(client: Any, symbol: str) -> int:
    try:
        basis = await client.get_asset_basis(symbol=symbol)
    except DeltaLabAPIError as exc:
        raise ValueError(f"unknown asset symbol {symbol!r}: {exc}") from exc
    asset_id = (basis or {}).get("asset_id")
    if not asset_id:
        raise ValueError(f"unknown asset symbol {symbol!r}")
    return int(asset_id)


# ---- token prices ------------------------------------------------------------


async def fetch_token_price_rows(
    feeds: Sequence[Mapping[str, Any]],
    *,
    days: float,
    since: pd.Timestamp | None = None,
    client: Any = TOKEN_CLIENT,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """USD close per candle for each resolved token feed (chain_id, address,
    interval pinned), labelled at the candle close, in-progress candle
    dropped. `since` narrows an incremental refresh; `days` bounds a backfill.
    History is read by window from the on-chain data source's store."""
    now_ms = int(time.time() * 1000)
    rows: list[dict[str, Any]] = []
    per_series: dict[str, int] = {}
    requests: dict[str, int] = {}
    errors: dict[str, str] = {}
    cadence: dict[str, str] = {}
    for feed in feeds:
        name = feed_feature_name(feed)
        try:
            interval = str(feed["interval"])
            start_ms = (
                int(since.timestamp() * 1000)
                if since is not None
                else now_ms - int(float(days) * 86_400_000)
            )
            window = await fetch_token_bar_window(
                str(feed["token_id"]),
                interval,
                start_ms=start_ms - INTERVAL_MS[interval],
                end_ms=now_ms,
                client=client,
                chain_id=int(feed["chain_id"]),
                address=str(feed["address"]),
            )
            requests[name] = window.requests
            count = 0
            for bar in window.bars:
                if bar.close_ms < start_ms:
                    continue
                rows.append(
                    {
                        "timestamp": pd.Timestamp(
                            bar.close_ms, unit="ms", tz="UTC"
                        ).isoformat(),
                        "name": name,
                        "value": bar.close,
                        "symbol": None,
                    }
                )
                count += 1
            per_series[name] = count
            cadence[name] = interval
        except Exception as exc:  # noqa: BLE001 — one bad token id keeps the others' history
            per_series[name] = 0
            errors[name] = str(exc)[:300]
    metadata = {
        "feature_kind": "token_price",
        "days": float(days),
        "per_series": per_series,
        "requests": requests,
        "errors": errors,
        "cadence": cadence,
        "label_convention": BAR_CLOSE_LABEL,
    }
    return rows, metadata


# ---- yields ------------------------------------------------------------------


async def _retry(call: Callable[[], Awaitable[pd.DataFrame]]) -> pd.DataFrame:
    """The yield service answers transient 500s under load: three attempts
    with a linear backoff; anything below 500 fails at once."""
    for attempt in range(_RETRIES):
        try:
            return await call()
        except DeltaLabAPIError as exc:
            status = getattr(exc, "status", None)
            if status is None or int(status) < 500 or attempt == _RETRIES - 1:
                raise
            await asyncio.sleep(_BACKOFF_S * (attempt + 1))
    raise AssertionError("unreachable")


async def _yield_frame(
    client: Any, feed: Mapping[str, Any], lookback_days: int
) -> pd.DataFrame:
    kind = str(feed["kind"])
    if kind in LENDING_KINDS:
        return await client.get_market_lending_ts(
            market_id=int(feed["market_id"]),
            asset_id=int(feed["asset_id"]),
            lookback_days=lookback_days,
        )
    if kind == "yield_apy":
        return await client.get_asset_yield_ts(
            asset_id=int(feed["asset_id"]), lookback_days=lookback_days
        )
    if kind == "pendle_implied_apy":
        return await client.get_market_pendle_ts(
            market_id=int(feed["market_id"]), lookback_days=lookback_days
        )
    if kind == "boros_fixed_rate":
        return await client.get_market_boros_ts(
            market_id=int(feed["market_id"]), lookback_days=lookback_days
        )
    raise ValueError(f"{kind!r} is not a yield feed")


def observed_cadence(index: pd.DatetimeIndex) -> str:
    """The series' native period from its median spacing: daily when the
    source publishes about once a day, hourly otherwise."""
    if len(index) < 2:
        return YIELD_CADENCE
    spacing = pd.Series(index).diff().dropna().dt.total_seconds().median()
    return "1d" if float(spacing) >= 20 * 3600 else YIELD_CADENCE


async def fetch_yield_rows(
    feeds: Sequence[Mapping[str, Any]],
    *,
    days: float,
    since: pd.Timestamp | None = None,
    client: Any = DELTA_LAB_CLIENT,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Hourly yield series per resolved feed as feature rows: rates stay the
    decimals the service reports (0.05 = 5% per year); duplicate observation
    times collapse to the last; history is bounded by the service's
    retention. `since` narrows an incremental refresh."""
    now = pd.Timestamp.now(tz="UTC")
    if since is not None:
        lookback_days = math.ceil((now - since).total_seconds() / 86_400) + 1
    else:
        lookback_days = math.ceil(float(days))
    lookback_days = max(1, min(lookback_days, YIELD_RETENTION_DAYS))
    rows: list[dict[str, Any]] = []
    per_series: dict[str, int] = {}
    errors: dict[str, str] = {}
    cadence: dict[str, str] = {}
    for feed in feeds:
        name = feed_feature_name(feed)
        try:
            frame = await _retry(partial(_yield_frame, client, feed, lookback_days))
            column = YIELD_COLUMNS[str(feed["kind"])]
            if column not in frame.columns:
                raise LookupError(f"{name}: series has no {column} column")
            series = pd.to_numeric(frame[column], errors="coerce")
            index = pd.DatetimeIndex(series.index)
            index = (
                index.tz_localize("UTC")
                if index.tz is None
                else index.tz_convert("UTC")
            )
            series.index = index
            series = series[~series.index.duplicated(keep="last")].dropna().sort_index()
            if since is not None:
                series = series[series.index >= since]
            for stamp, value in series.items():
                rows.append(
                    {
                        "timestamp": stamp.isoformat(),
                        "name": name,
                        "value": float(value),
                        "symbol": None,
                    }
                )
            per_series[name] = int(len(series))
            cadence[name] = observed_cadence(series.index)
        except Exception as exc:  # noqa: BLE001 — one dead market keeps the others' history
            per_series[name] = 0
            errors[name] = str(exc)[:300]
    metadata = {
        "feature_kind": "yield",
        "lookback_days": lookback_days,
        "retention_days": YIELD_RETENTION_DAYS,
        "per_series": per_series,
        "errors": errors,
        "cadence": cadence,
    }
    return rows, metadata
