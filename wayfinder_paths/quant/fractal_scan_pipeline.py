"""Hydrate, cache, and match exact-market Pattern Match history."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, TypedDict

from wayfinder_paths.core.clients.HyperliquidDataClient import (
    HYPERLIQUID_DATA_CLIENT,
)
from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.core.utils.symbols import normalize_symbol
from wayfinder_paths.quant.fractal_scan import (
    MIN_PATTERN_BARS,
    PriceSeries,
    find_price_analogs,
)
from wayfinder_paths.quant.fractal_scan_context import (
    INTERVAL_MS,
    MAX_PATTERN_BARS,
    SUPPORTED_INTERVALS,
    FractalScanRequest,
)

MAX_HISTORY_BARS = 10_000
MAX_ONCHAIN_HISTORY_BARS = 2_000
ONCHAIN_CANDLE_PAGE_SIZE = 1_000
MAX_ONCHAIN_HISTORY_PAGES = math.ceil(
    MAX_ONCHAIN_HISTORY_BARS / ONCHAIN_CANDLE_PAGE_SIZE
)
MAX_HISTORY_DAYS = 3 * 366
TOP_MATCHES = 15
SCAN_CACHE_TTL_SECONDS = 15 * 60
SCAN_CACHE_MAX_ENTRIES = 32
FORWARD_HORIZONS_MS = {
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "12h": 12 * 60 * 60_000,
    "24h": 24 * 60 * 60_000,
}


class CandleRow(TypedDict):
    t: int
    o: float | None
    h: float | None
    l: float | None  # noqa: E741 - provider OHLC field name
    c: float
    v: float | None


@dataclass(frozen=True)
class _ScanWindow:
    interval: str
    interval_ms: int
    start_ms: int
    end_ms: int
    last_closed_ms: int


@dataclass
class _CachedScan:
    result: dict[str, Any]
    created_at: float = field(default_factory=time.monotonic)


_SCAN_CACHE: OrderedDict[str, _CachedScan] = OrderedDict()


async def run_fractal_scan(
    *,
    request: FractalScanRequest,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Return a compact exact-market analogue baseline.

    The quant agent owns any decision to widen the comparison universe.
    Identical requests reuse the result cache instead of pulling candles again.
    """

    started = time.perf_counter()
    scan_id = _scan_identity(request)
    if cached := _cached_scan(scan_id):
        return cached

    fetch_started = time.perf_counter()
    current_ms = (
        now_ms if now_ms is not None else int(datetime.now(UTC).timestamp() * 1000)
    )
    window = _scan_window(request, current_ms)
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
    data_fetch_ms = (time.perf_counter() - fetch_started) * 1000

    horizon_bars = _forward_horizon_bars(window.interval_ms)
    match_started = time.perf_counter()
    analysis, matches, distributions = _analyze_history(
        _series(symbol, source, selected_rows),
        _series(symbol, source, rows),
        horizon_bars,
    )
    match_ms = (time.perf_counter() - match_started) * 1000
    if len(matches) < 8:
        warnings.append("small_analogue_sample")

    result = {
        "schema_version": 3,
        "mode": "fractal_scan",
        "scan_id": scan_id,
        "market_id": request.market_id,
        "chart_id": request.chart_id,
        "display_symbol": request.display_symbol,
        "requested_window": {
            "start_ms": request.start_ms,
            "end_ms": request.end_ms,
            "interval": request.interval,
        },
        "analyzed_window": {
            "start_ms": int(selected_rows[0]["t"]),
            "end_ms": int(selected_rows[-1]["t"]),
            "interval": window.interval,
            "bars": len(selected_rows),
        },
        "coverage": {
            "ratio": round(coverage_ratio, 3),
            "expected_bars": expected_bars,
            "actual_bars": len(selected_rows),
            "history_bars": len(rows),
            "source": source,
        },
        "pattern": analysis["pattern"],
        "matches": matches,
        "outcome_distributions": distributions,
        "forward_horizons": {
            label: {
                "bars": bars,
                "actual_ms": bars * window.interval_ms,
            }
            for label, bars in horizon_bars.items()
        },
        "levels": _selection_levels(request, selected_rows),
        "evidence": {"same_market_samples": len(matches)},
        "warnings": warnings,
        "timing_ms": {
            "data_fetch": round(data_fetch_ms, 1),
            "matching": round(match_ms, 1),
            "total": round((time.perf_counter() - started) * 1000, 1),
        },
        "data_ready": True,
    }
    _cache_scan(scan_id, result)
    return result


async def run_fractal_scan_ccxt_proxy(
    *,
    scan_id: str,
    symbol: str,
) -> dict[str, Any]:
    """Compare a cached exact pattern with an explicitly selected CCXT asset."""

    exact = _cached_scan(scan_id)
    if exact is None or exact.get("mode") != "fractal_scan":
        raise ValueError("scan_id is unknown or expired; run the exact scan first")

    proxy_symbol = _normalize_ccxt_symbol(symbol)
    cache_key = f"{scan_id}:ccxt:{proxy_symbol}"
    if cached := _cached_scan(cache_key):
        return cached

    analyzed = exact["analyzed_window"]
    interval = str(analyzed["interval"])
    interval_ms = INTERVAL_MS[interval]
    end_ms = int(analyzed["end_ms"])
    start = datetime.fromtimestamp(
        (end_ms - _history_lookback_ms(interval_ms)) / 1000,
        tz=UTC,
    )
    end = datetime.fromtimestamp(end_ms / 1000, tz=UTC)

    # Keep pandas and the CCXT adapter off the MCP startup path. They are only
    # needed when the quant agent explicitly asks for proxy evidence.
    from wayfinder_paths.core.backtesting.data import fetch_prices

    started = time.perf_counter()
    prices = await fetch_prices(
        [proxy_symbol],
        start.isoformat(),
        end.isoformat(),
        interval=interval,
        source="ccxt",
    )
    values = prices[proxy_symbol].dropna().tail(MAX_HISTORY_BARS)
    if values.empty:
        raise ValueError(f"No CCXT candles are available for {proxy_symbol}")

    source = "ccxt:binance"
    shape = [float(value) for value in exact["pattern"]["shape_path_bps"]]
    pattern_start_ms = int(analyzed["start_ms"])
    pattern = PriceSeries(
        symbol=proxy_symbol,
        source=source,
        timestamps_ms=[
            pattern_start_ms + index * interval_ms for index in range(len(shape))
        ],
        closes=[1 + value / 10_000 for value in shape],
    )
    history = PriceSeries(
        symbol=proxy_symbol,
        source=source,
        timestamps_ms=[int(timestamp.timestamp() * 1000) for timestamp in values.index],
        closes=[float(value) for value in values],
    )
    horizon_bars = {
        label: int(details["bars"])
        for label, details in exact["forward_horizons"].items()
    }
    _, matches, distributions = _analyze_history(
        pattern,
        history,
        horizon_bars,
        match_scope="same_asset_proxy",
    )
    warnings = ["ccxt_same_asset_proxy"]
    if len(matches) < 8:
        warnings.append("small_proxy_sample")
    result = {
        "schema_version": 3,
        "mode": "fractal_scan_proxy",
        "scan_id": scan_id,
        "proxy": {"symbol": proxy_symbol, "source": source, "interval": interval},
        "coverage": {"history_bars": len(values), "source": source},
        "matches": matches,
        "outcome_distributions": distributions,
        "forward_horizons": exact["forward_horizons"],
        "evidence": {"same_asset_proxy_samples": len(matches)},
        "warnings": warnings,
        "timing_ms": {"total": round((time.perf_counter() - started) * 1000, 1)},
        "data_ready": True,
    }
    _cache_scan(cache_key, result)
    return result


def _scan_identity(request: FractalScanRequest) -> str:
    encoded = json.dumps(
        asdict(request), sort_keys=True, separators=(",", ":")
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


async def _hyperliquid_history(symbol: str, window: _ScanWindow) -> list[CandleRow]:
    start_ms = window.end_ms - _history_lookback_ms(window.interval_ms)
    response_rows = await HYPERLIQUID_DATA_CLIENT.get_candles(
        symbol, start_ms, window.end_ms, window.interval
    )
    rows = _normalize_rows(response_rows)[-MAX_HISTORY_BARS:]
    if not rows:
        raise ValueError(
            f"No completed {window.interval} candles are available for {symbol}"
        )
    return rows


async def _onchain_history(
    request: FractalScanRequest, window: _ScanWindow
) -> list[CandleRow]:
    if request.chain_id is None or request.token_address is None:
        raise ValueError("On-chain scans require chain_id and token_address")

    rows: list[CandleRow] = []
    before_timestamp: int | None = window.end_ms // 1000 + window.interval_ms // 1000
    oldest_timestamp: int | None = None
    request_count = 0
    request_limit = MAX_ONCHAIN_HISTORY_PAGES

    while request_count < request_limit and len(rows) < MAX_ONCHAIN_HISTORY_BARS:
        page_rows = _normalize_rows(
            await TOKEN_CLIENT.get_candles(
                request.token_address,
                window.interval,
                chain_id=request.chain_id,
                before_timestamp=before_timestamp,
            )
        )
        request_count += 1

        # Some pools reject a bounded first page despite having recent history.
        # Retry the provider default once, then continue normal backward paging.
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


def _normalize_rows(
    rows: Sequence[Mapping[str, Any]],
) -> list[CandleRow]:
    by_timestamp: dict[int, CandleRow] = {}
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
    rows: list[CandleRow], window: _ScanWindow
) -> tuple[list[CandleRow], int, float]:
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
    rows: list[CandleRow],
) -> PriceSeries:
    return PriceSeries(
        symbol=symbol,
        source=source,
        timestamps_ms=[int(row["t"]) for row in rows],
        closes=[float(row["c"]) for row in rows],
    )


def _forward_horizon_bars(interval_ms: int) -> dict[str, int]:
    return {
        label: max(1, math.ceil(duration_ms / interval_ms))
        for label, duration_ms in FORWARD_HORIZONS_MS.items()
    }


def _analyze_history(
    pattern: PriceSeries,
    history: PriceSeries,
    horizon_bars: dict[str, int],
    *,
    match_scope: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Apply the shared matcher configuration and label wall-clock outcomes."""

    analysis = find_price_analogs(
        pattern,
        [history],
        horizons=tuple(sorted(set(horizon_bars.values()))),
        top=TOP_MATCHES,
        shape_paths=5,
    )
    matches = [
        {
            **match,
            "outcomes": {
                f"{label}_bps": match["outcomes"][f"{bars}_bar_bps"]
                for label, bars in horizon_bars.items()
            },
        }
        for match in analysis["matches"]
    ]
    if match_scope is not None:
        matches = [{**match, "match_scope": match_scope} for match in matches]
    distributions = {
        label: analysis["outcome_distributions"][f"{bars}_bar"]
        for label, bars in horizon_bars.items()
    }
    return analysis, matches, distributions


def _selection_levels(
    request: FractalScanRequest,
    selected_rows: list[CandleRow],
) -> dict[str, float]:
    closes = [float(row["c"]) for row in selected_rows]
    lows = [float(row["l"]) for row in selected_rows if row["l"] is not None]
    highs = [float(row["h"]) for row in selected_rows if row["h"] is not None]
    return {
        "selection_low": request.selected_price_min
        or (min(lows) if lows else min(closes)),
        "selection_high": request.selected_price_max
        or (max(highs) if highs else max(closes)),
        "last_close": closes[-1],
    }


def _history_lookback_ms(interval_ms: int) -> int:
    return min(MAX_HISTORY_DAYS * 86_400_000, MAX_HISTORY_BARS * interval_ms)


def _normalize_hl_coin(value: str) -> str:
    return value.strip().upper().removesuffix("-USDC").removesuffix("/USDC")


def _normalize_ccxt_symbol(value: str) -> str:
    symbol = normalize_symbol(value).upper()
    for quote in ("USDC", "USDT"):
        symbol = symbol.removesuffix(quote)
    symbol = {"WBTC": "BTC", "WETH": "ETH"}.get(symbol, symbol)
    if not symbol.isalnum() or len(symbol) > 16:
        raise ValueError("symbol must be a simple CCXT base asset such as BTC or ETH")
    return symbol


def _float_or_none(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _cached_scan(scan_id: str) -> dict[str, Any] | None:
    _evict_expired_scans()
    cached = _SCAN_CACHE.get(scan_id)
    if cached is None:
        return None
    _SCAN_CACHE.move_to_end(scan_id)
    return cached.result


def _cache_scan(scan_id: str, result: dict[str, Any]) -> None:
    _SCAN_CACHE[scan_id] = _CachedScan(result)
    _SCAN_CACHE.move_to_end(scan_id)
    while len(_SCAN_CACHE) > SCAN_CACHE_MAX_ENTRIES:
        _SCAN_CACHE.popitem(last=False)


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
