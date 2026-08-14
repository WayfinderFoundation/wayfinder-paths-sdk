from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

DEFAULT_PERP_EXCHANGES = ("okx", "bitget", "gate")
PAGE_LIMIT = 1_000
MAX_PAGES = 200
RETRIES = 3
RETRYABLE_ERRORS = {
    "DDoSProtection",
    "ExchangeNotAvailable",
    "NetworkError",
    "RateLimitExceeded",
    "RequestTimeout",
}


@dataclass(frozen=True)
class CcxtPerpHistory:
    """Completed linear-perpetual candles with auditable venue provenance."""

    exchange_id: str
    market_symbol: str
    rows: list[dict[str, float | int]]
    failures: tuple[str, ...]


async def fetch_ccxt_perp_history(
    base_symbol: str,
    interval: str,
    *,
    interval_ms: int,
    start_ms: int,
    end_ms: int,
    exchange_ids: Sequence[str] = DEFAULT_PERP_EXCHANGES,
) -> CcxtPerpHistory:
    """Fetch the first healthy linear-perpetual history without using spot.

    Venue failures are isolated: restricted regions, unsupported contracts, and
    availability errors advance to the next configured exchange. Transient
    request failures receive a small bounded retry budget within one venue.
    """

    failures: list[str] = []
    for exchange_id in exchange_ids:
        try:
            market_symbol, rows = await _fetch_exchange_history(
                exchange_id,
                base_symbol,
                interval,
                interval_ms=interval_ms,
                start_ms=start_ms,
                end_ms=end_ms,
            )
            if rows:
                return CcxtPerpHistory(
                    exchange_id=exchange_id,
                    market_symbol=market_symbol,
                    rows=rows,
                    failures=tuple(failures),
                )
            failures.append(f"{exchange_id}:no_completed_candles")
        except Exception as exc:  # noqa: BLE001 - one venue must not stop failover
            failures.append(f"{exchange_id}:{type(exc).__name__}:{exc}")

    detail = "; ".join(failures) or "no exchanges configured"
    raise RuntimeError(
        f"No supported perpetual history is available for {base_symbol}: {detail}"
    )


async def _fetch_exchange_history(
    exchange_id: str,
    base_symbol: str,
    interval: str,
    *,
    interval_ms: int,
    start_ms: int,
    end_ms: int,
) -> tuple[str, list[dict[str, float | int]]]:
    # Lazy import keeps the full CCXT exchange registry off the MCP startup path.
    from wayfinder_paths.adapters.ccxt_adapter import CCXTAdapter

    adapter = CCXTAdapter(
        exchanges={
            exchange_id: {
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},
            }
        }
    )
    try:
        exchange = getattr(adapter, exchange_id)
        markets = await exchange.load_markets()
        market_symbol = _resolve_linear_perp_symbol(markets, base_symbol)
        candles: dict[int, list[Any]] = {}
        cursor = start_ms
        pages = 0
        while cursor <= end_ms and pages < MAX_PAGES:
            batch = await _fetch_with_retry(
                exchange,
                market_symbol,
                interval,
                since=cursor,
            )
            if not batch:
                break
            for row in batch:
                if len(row) >= 6:
                    candles[int(row[0])] = list(row)
            last_timestamp = int(batch[-1][0])
            if last_timestamp <= cursor:
                break
            cursor = last_timestamp + interval_ms
            pages += 1

        rows = [
            {
                "t": timestamp,
                "o": float(row[1]),
                "h": float(row[2]),
                "l": float(row[3]),
                "c": float(row[4]),
                "v": float(row[5]),
            }
            for timestamp, row in sorted(candles.items())
            if timestamp <= end_ms
        ]
        return market_symbol, rows
    finally:
        await adapter.close()


def _resolve_linear_perp_symbol(markets: Mapping[str, Any], base_symbol: str) -> str:
    base = base_symbol.upper()
    preferred = (f"{base}/USDT:USDT", f"{base}/USDC:USDC")
    for symbol in preferred:
        market = markets.get(symbol)
        if _is_active_linear_swap(market, base):
            return symbol

    candidates = [
        str(symbol)
        for symbol, market in markets.items()
        if _is_active_linear_swap(market, base)
    ]
    if candidates:
        return sorted(
            candidates,
            key=lambda symbol: (
                0 if "/USDT:" in symbol else 1,
                0 if "/USDC:" in symbol else 1,
                symbol,
            ),
        )[0]
    raise ValueError(f"no active linear perpetual market for {base_symbol}")


def _is_active_linear_swap(market: Any, base_symbol: str) -> bool:
    if not isinstance(market, Mapping):
        return False
    return (
        market.get("active") is not False
        and market.get("swap") is True
        and market.get("linear") is not False
        and str(market.get("base") or "").upper() == base_symbol
        and str(market.get("quote") or "").upper() in {"USDT", "USDC"}
    )


async def _fetch_with_retry(
    exchange: Any,
    market_symbol: str,
    interval: str,
    *,
    since: int,
) -> list[list[Any]]:
    last_error: Exception | None = None
    for attempt in range(RETRIES):
        try:
            return await exchange.fetch_ohlcv(
                market_symbol,
                interval,
                since=since,
                limit=PAGE_LIMIT,
            )
        except Exception as exc:  # noqa: BLE001 - CCXT error classes are optional
            last_error = exc
            retryable = type(exc).__name__ in RETRYABLE_ERRORS or "429" in str(exc)
            if not retryable or attempt == RETRIES - 1:
                raise
            await asyncio.sleep(0.25 * (2**attempt))
    raise RuntimeError(f"CCXT request failed: {last_error}")
