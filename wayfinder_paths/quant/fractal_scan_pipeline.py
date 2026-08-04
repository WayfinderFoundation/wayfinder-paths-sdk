"""Hydrate, cache, and expand deterministic Fractal Scan analysis."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from wayfinder_paths.core.clients.HyperliquidDataClient import (
    HYPERLIQUID_DATA_CLIENT,
)
from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.quant.fractal_scan import (
    MIN_PATTERN_BARS,
    PriceSeries,
    find_price_analogs,
    summarize_forward_outcomes,
)
from wayfinder_paths.quant.fractal_scan_context import (
    INTERVAL_MS,
    MAX_PATTERN_BARS,
    SUPPORTED_INTERVALS,
    FractalScanRequest,
    FractalScanScope,
)
from wayfinder_paths.quant.fractal_scan_output import (
    build_view_data,
    confidence_label,
    label_match_scopes,
    pattern_metrics,
    regime_stats,
)

MAX_HISTORY_BARS = 10_000
MAX_ONCHAIN_HISTORY_BARS = 2_000
ONCHAIN_CANDLE_PAGE_SIZE = 1_000
MAX_ONCHAIN_HISTORY_PAGES = math.ceil(
    MAX_ONCHAIN_HISTORY_BARS / ONCHAIN_CANDLE_PAGE_SIZE
)
MAX_HISTORY_DAYS = 3 * 366
MIN_EXACT_MATCHES = 12
TOP_MATCHES = 15
MAX_PEERS = 4
PEER_SYMBOLS = ("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "AVAX", "LINK")
SCAN_CACHE_TTL_SECONDS = 15 * 60
SCAN_CACHE_MAX_ENTRIES = 32


@dataclass(frozen=True)
class _ScanWindow:
    interval: str
    interval_ms: int
    start_ms: int
    end_ms: int
    last_closed_ms: int


@dataclass
class _PreparedScan:
    request: FractalScanRequest
    scan_id: str
    window: _ScanWindow
    symbol: str
    source: str
    rows: list[dict[str, float | int | None]]
    selected_rows: list[dict[str, float | int | None]]
    expected_bars: int
    coverage_ratio: float
    warnings: list[str]
    data_fetch_ms: float
    created_at: float = field(default_factory=time.monotonic)
    peer_histories: list[PriceSeries] | None = None
    same_asset_history: PriceSeries | None = None


_SCAN_CACHE: OrderedDict[str, _PreparedScan] = OrderedDict()


async def run_fractal_scan(
    *,
    scope: FractalScanScope = "same_market",
    request: FractalScanRequest | None = None,
    scan_id: str | None = None,
    include_views: bool = True,
    now_ms: int | None = None,
) -> dict[str, Any]:
    if scope not in {"same_market", "adaptive", "broad"}:
        raise ValueError("scope must be same_market, adaptive, or broad")
    started = time.perf_counter()
    prepared = await _resolve_prepared_scan(request, scan_id, now_ms=now_ms)
    exact_history = _series(prepared.symbol, prepared.source, prepared.rows)
    pattern = _series(prepared.symbol, prepared.source, prepared.selected_rows)
    histories = [exact_history]
    match_started = time.perf_counter()
    exact_analysis = find_price_analogs(
        pattern,
        histories,
        top=TOP_MATCHES,
        shape_paths=5 if include_views else 0,
    )

    scope_used: FractalScanScope = "same_market"
    exact_match_count = len(exact_analysis["matches"])
    should_expand = scope == "broad" or (
        scope == "adaptive" and exact_match_count < MIN_EXACT_MATCHES
    )
    if should_expand:
        histories.extend(await _get_peer_histories(prepared))
        scope_used = scope
        if scope == "broad":
            same_asset = await _get_same_asset_history(prepared)
            if same_asset is not None:
                histories.append(same_asset)

    analysis = (
        exact_analysis
        if len(histories) == 1
        else find_price_analogs(
            pattern,
            histories,
            top=TOP_MATCHES,
            shape_paths=5 if include_views else 0,
        )
    )
    match_ms = (time.perf_counter() - match_started) * 1000
    matches = label_match_scopes(
        analysis["matches"],
        selected_symbol=_canonical_symbol(prepared.symbol),
        exact_symbol=prepared.symbol,
        exact_source=prepared.source,
        canonical_symbol=_canonical_symbol,
    )
    warnings = list(prepared.warnings)
    if len(matches) < 8:
        warnings.append("small_analogue_sample")
    if any(match["match_scope"] != "same_market" for match in matches):
        warnings.append("fuzzy_analogues_included")

    exact_matches = [
        match for match in matches if match["match_scope"] == "same_market"
    ]
    fuzzy_matches = [
        match for match in matches if match["match_scope"] != "same_market"
    ]
    confidence = confidence_label(len(exact_matches), prepared.coverage_ratio)
    metrics = pattern_metrics(prepared.selected_rows)
    result = {
        "schema_version": 2,
        "mode": "fractal_scan",
        "scan_id": prepared.scan_id,
        "scope_requested": scope,
        "scope_used": scope_used,
        "market_id": prepared.request.market_id,
        "chart_id": prepared.request.chart_id,
        "display_symbol": prepared.request.display_symbol,
        "requested_window": {
            "start_ms": prepared.request.start_ms,
            "end_ms": prepared.request.end_ms,
            "interval": prepared.request.interval,
        },
        "analyzed_window": {
            "start_ms": int(prepared.selected_rows[0]["t"]),
            "end_ms": int(prepared.selected_rows[-1]["t"]),
            "interval": prepared.window.interval,
            "bars": len(prepared.selected_rows),
        },
        "coverage": {
            "ratio": round(prepared.coverage_ratio, 3),
            "expected_bars": prepared.expected_bars,
            "actual_bars": len(prepared.selected_rows),
            "history_bars": sum(len(history.closes) for history in histories),
            "sources": sorted({history.source for history in histories}),
        },
        "pattern": {**analysis["pattern"], **metrics},
        "matches": matches,
        "outcome_distributions": analysis["outcome_distributions"],
        "outcome_distributions_by_scope": {
            "same_market": summarize_forward_outcomes(exact_matches, (1, 3, 6, 12)),
            "fuzzy": summarize_forward_outcomes(fuzzy_matches, (1, 3, 6, 12)),
        },
        "regime": regime_stats(prepared.rows, prepared.window.interval_ms),
        "levels": {
            "selection_low": prepared.request.selected_price_min or metrics["low"],
            "selection_high": prepared.request.selected_price_max or metrics["high"],
            "last_close": metrics["last"],
        },
        "evidence": {
            "same_market_samples": len(exact_matches),
            "fuzzy_samples": len(fuzzy_matches),
            "fuzzy_match_scopes": sorted(
                {match["match_scope"] for match in fuzzy_matches}
            ),
        },
        "confidence": confidence,
        "warnings": list(dict.fromkeys(warnings)),
        "expansion": {
            "available_scopes": ["same_market", "adaptive", "broad"],
            "reuse_scan_id": prepared.scan_id,
            "same_market_target": MIN_EXACT_MATCHES,
        },
        "view_data": build_view_data(analysis, matches) if include_views else None,
        "timing_ms": {
            "data_fetch": round(prepared.data_fetch_ms, 1),
            "matching": round(match_ms, 1),
            "total": round((time.perf_counter() - started) * 1000, 1),
        },
        "data_ready": True,
    }
    return result


async def _resolve_prepared_scan(
    request: FractalScanRequest | None,
    scan_id: str | None,
    *,
    now_ms: int | None,
) -> _PreparedScan:
    _evict_expired_scans()
    if scan_id is not None:
        prepared = _SCAN_CACHE.get(scan_id)
        if prepared is None:
            raise ValueError(
                "scan_id is unknown or expired; start a new same_market scan"
            )
        _SCAN_CACHE.move_to_end(scan_id)
        return prepared
    if request is None:
        raise ValueError("Initial scans require exact market and chart context")
    identity = _scan_identity(request)
    if cached := _SCAN_CACHE.get(identity):
        _SCAN_CACHE.move_to_end(identity)
        return cached
    prepared = await _prepare_scan(
        request,
        identity,
        now_ms=now_ms or int(datetime.now(UTC).timestamp() * 1000),
    )
    _SCAN_CACHE[identity] = prepared
    _SCAN_CACHE.move_to_end(identity)
    while len(_SCAN_CACHE) > SCAN_CACHE_MAX_ENTRIES:
        _SCAN_CACHE.popitem(last=False)
    return prepared


async def _prepare_scan(
    request: FractalScanRequest, scan_id: str, *, now_ms: int
) -> _PreparedScan:
    fetch_started = time.perf_counter()
    window = _scan_window(request, now_ms)
    warnings: list[str] = []
    if window.interval != request.interval:
        warnings.append(f"auto_coarsened:{request.interval}->{window.interval}")
    if window.end_ms < request.end_ms:
        warnings.append("selection_clamped_to_last_closed_candle")

    if request.kind == "hyperliquid":
        symbol = _normalize_hl_coin(request.hl_coin or "")
        source = "hyperliquid"
        rows = await _hyperliquid_history(symbol, window)
    else:
        symbol = request.display_symbol.upper()
        source = f"coingecko_onchain:{request.chain_id}"
        rows = await _onchain_history(request, window)
    rows = [row for row in rows if int(row["t"]) <= window.last_closed_ms]
    selected_rows, expected_bars, coverage_ratio = _selected_pattern(rows, window)
    if coverage_ratio < 0.9:
        warnings.append("selected_window_has_data_gaps")
    return _PreparedScan(
        request=request,
        scan_id=scan_id,
        window=window,
        symbol=symbol,
        source=source,
        rows=rows,
        selected_rows=selected_rows,
        expected_bars=expected_bars,
        coverage_ratio=coverage_ratio,
        warnings=warnings,
        data_fetch_ms=(time.perf_counter() - fetch_started) * 1000,
    )


def _scan_identity(request: FractalScanRequest) -> str:
    encoded = json.dumps(
        request.__dict__, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


def _scan_window(request: FractalScanRequest, now_ms: int) -> _ScanWindow:
    requested_ms = INTERVAL_MS[request.interval]
    duration_ms = request.end_ms - request.start_ms
    candidates = [
        interval
        for interval in SUPPORTED_INTERVALS
        if INTERVAL_MS[interval] >= requested_ms
    ]
    interval = next(
        (
            candidate
            for candidate in candidates
            if math.floor(duration_ms / INTERVAL_MS[candidate]) + 1 <= MAX_PATTERN_BARS
        ),
        None,
    )
    if interval is None:
        raise ValueError("Selection is too large even at the daily interval")
    interval_ms = INTERVAL_MS[interval]
    pattern_bars = math.floor(duration_ms / interval_ms) + 1
    if pattern_bars < MIN_PATTERN_BARS:
        raise ValueError(
            f"Selection has fewer than {MIN_PATTERN_BARS} completed {interval} candles"
        )
    last_closed_ms = (now_ms // interval_ms) * interval_ms - interval_ms
    end_ms = min((request.end_ms // interval_ms) * interval_ms, last_closed_ms)
    start_ms = (request.start_ms // interval_ms) * interval_ms
    if end_ms <= start_ms:
        raise ValueError("Selection does not contain completed candles")
    return _ScanWindow(interval, interval_ms, start_ms, end_ms, last_closed_ms)


async def _hyperliquid_history(
    symbol: str, window: _ScanWindow
) -> list[dict[str, float | int | None]]:
    start_ms = window.end_ms - _history_lookback_ms(window.interval_ms)
    response = await HYPERLIQUID_DATA_CLIENT.get_candles_response(
        symbol, start_ms, window.end_ms, window.interval
    )
    rows = _normalize_rows(response.get("rows") or [])[-MAX_HISTORY_BARS:]
    if not rows:
        raise ValueError(
            f"No completed {window.interval} candles are available for {symbol}"
        )
    return rows


async def _onchain_history(
    request: FractalScanRequest, window: _ScanWindow
) -> list[dict[str, float | int | None]]:
    if request.chain_id is None or request.token_address is None:
        raise ValueError("On-chain scans require chain_id and token_address")

    rows: list[dict[str, float | int | None]] = []
    before_timestamp: int | None = window.end_ms // 1000 + window.interval_ms // 1000
    oldest_timestamp: int | None = None
    request_count = 0
    request_limit = MAX_ONCHAIN_HISTORY_PAGES

    while request_count < request_limit and len(rows) < MAX_ONCHAIN_HISTORY_BARS:
        page = await TOKEN_CLIENT.get_candles(
            request.token_address,
            window.interval,
            chain_id=request.chain_id,
            before_timestamp=before_timestamp,
        )
        request_count += 1
        page_rows = _normalize_rows(list(page.get("rows") or []))

        # A bounded request can occasionally return an empty page even when
        # the token has recent history. Retry the provider's default page once
        # and continue paging backwards from there. The downstream selected-
        # window check still prevents us from analyzing unrelated recent data.
        if not page_rows and request_count == 1:
            before_timestamp = None
            request_limit += 1
            continue
        if not page_rows:
            break

        next_oldest_value = page_rows[0].get("t")
        if next_oldest_value is None:
            break
        next_oldest = int(next_oldest_value)
        if oldest_timestamp is not None and next_oldest >= oldest_timestamp:
            break

        rows.extend(page_rows)
        oldest_timestamp = next_oldest
        before_timestamp = next_oldest // 1000 - 1

    normalized = _normalize_rows(rows)[-MAX_ONCHAIN_HISTORY_BARS:]
    if not normalized:
        raise ValueError("No on-chain candles are available for this token")
    return normalized


async def _get_peer_histories(prepared: _PreparedScan) -> list[PriceSeries]:
    if prepared.peer_histories is not None:
        return prepared.peer_histories
    peers = [symbol for symbol in PEER_SYMBOLS if symbol != prepared.symbol][:MAX_PEERS]
    start_ms = prepared.window.end_ms - _history_lookback_ms(
        prepared.window.interval_ms
    )

    async def fetch(symbol: str) -> PriceSeries | None:
        try:
            response = await HYPERLIQUID_DATA_CLIENT.get_candles_response(
                symbol,
                start_ms,
                prepared.window.end_ms,
                prepared.window.interval,
            )
        except Exception:
            return None
        rows = _normalize_rows(response.get("rows") or [])[-MAX_HISTORY_BARS:]
        return _series(symbol, "hyperliquid", rows) if rows else None

    try:
        async with asyncio.timeout(8):
            fetched = await asyncio.gather(*(fetch(symbol) for symbol in peers))
    except TimeoutError:
        fetched = []
    prepared.peer_histories = [series for series in fetched if series is not None]
    return prepared.peer_histories


async def _get_same_asset_history(prepared: _PreparedScan) -> PriceSeries | None:
    if prepared.same_asset_history is not None:
        return prepared.same_asset_history
    symbol = _canonical_symbol(prepared.symbol)
    if symbol is None:
        return None
    from wayfinder_paths.core.backtesting.data import fetch_prices

    start = datetime.fromtimestamp(
        (prepared.window.end_ms - _history_lookback_ms(prepared.window.interval_ms))
        / 1000,
        tz=UTC,
    )
    end = datetime.fromtimestamp(prepared.window.end_ms / 1000, tz=UTC)
    try:
        async with asyncio.timeout(8):
            prices = await fetch_prices(
                [symbol],
                start.isoformat(),
                end.isoformat(),
                interval=prepared.window.interval,
                source="ccxt",
            )
    except Exception:
        return None
    if symbol not in prices:
        return None
    values = prices[symbol].dropna().tail(MAX_HISTORY_BARS)
    if values.empty:
        return None
    prepared.same_asset_history = PriceSeries(
        symbol=symbol,
        source="binance",
        timestamps_ms=[int(timestamp.timestamp() * 1000) for timestamp in values.index],
        closes=[float(value) for value in values],
    )
    return prepared.same_asset_history


def _normalize_rows(
    rows: list[dict[str, Any]],
) -> list[dict[str, float | int | None]]:
    by_timestamp: dict[int, dict[str, float | int | None]] = {}
    for row in rows:
        try:
            timestamp = int(row["t"])
            close = float(row["c"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(close) or close <= 0:
            continue
        by_timestamp[timestamp] = {
            "t": timestamp,
            "o": _float_or_none(row.get("o")),
            "h": _float_or_none(row.get("h")),
            "l": _float_or_none(row.get("l")),
            "c": close,
            "v": _float_or_none(row.get("v")),
        }
    return [by_timestamp[timestamp] for timestamp in sorted(by_timestamp)]


def _selected_pattern(
    rows: list[dict[str, float | int | None]], window: _ScanWindow
) -> tuple[list[dict[str, float | int | None]], int, float]:
    selected = [
        row for row in rows if window.start_ms <= int(row["t"]) <= window.end_ms
    ]
    if len(selected) < MIN_PATTERN_BARS:
        raise ValueError(
            f"Only {len(selected)} completed candles are available in the selected "
            f"window; {MIN_PATTERN_BARS} are required"
        )
    expected = math.floor((window.end_ms - window.start_ms) / window.interval_ms) + 1
    return selected, expected, min(1.0, len(selected) / expected)


def _series(
    symbol: str,
    source: str,
    rows: list[dict[str, float | int | None]],
) -> PriceSeries:
    return PriceSeries(
        symbol=symbol,
        source=source,
        timestamps_ms=[int(row["t"]) for row in rows],
        closes=[float(row["c"]) for row in rows],
    )


def _history_lookback_ms(interval_ms: int) -> int:
    return min(MAX_HISTORY_DAYS * 86_400_000, MAX_HISTORY_BARS * interval_ms)


def _normalize_hl_coin(value: str) -> str:
    return value.strip().upper().removesuffix("-USDC").removesuffix("/USDC")


def _canonical_symbol(value: str) -> str | None:
    symbol = value.upper().removesuffix("-USDC")
    aliases = {"WBTC": "BTC", "WETH": "ETH"}
    symbol = aliases.get(symbol, symbol)
    return symbol if symbol in PEER_SYMBOLS else None


def _float_or_none(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _evict_expired_scans() -> None:
    cutoff = time.monotonic() - SCAN_CACHE_TTL_SECONDS
    expired = [
        scan_id
        for scan_id, prepared in _SCAN_CACHE.items()
        if prepared.created_at < cutoff
    ]
    for scan_id in expired:
        _SCAN_CACHE.pop(scan_id, None)


def _clear_fractal_scan_cache() -> None:
    _SCAN_CACHE.clear()
