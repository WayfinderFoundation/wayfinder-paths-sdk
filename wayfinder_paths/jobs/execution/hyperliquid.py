from __future__ import annotations

import asyncio
import time
from typing import Any

from wayfinder_paths.core.clients.HyperliquidDataClient import (
    HYPERLIQUID_DATA_CLIENT,
    CandleEntry,
    HyperliquidDataClient,
)
from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    FillEvent,
    OrderIntent,
    StateSnapshot,
    TradeCapacity,
)


class SafeHyperliquidMarketClient:
    def __init__(self, client: HyperliquidDataClient | None = None) -> None:
        self.client = client or HYPERLIQUID_DATA_CLIENT

    async def get_completed_bars(
        self,
        asset_name: str,
        interval: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        lookback_hours: int | None = None,
        retries: int = 3,
    ) -> CompletedBarsView:
        last_error: Exception | None = None
        for attempt in range(max(1, retries)):
            try:
                rows = await self.client.get_candles(
                    asset_name,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    interval=interval,
                    lookback_hours=lookback_hours,
                )
                return _candles_to_completed_view(asset_name, rows)
            except Exception as exc:
                last_error = exc
                if "429" not in str(exc) or attempt >= retries - 1:
                    break
                await asyncio.sleep(0.25 * (2**attempt))
        raise RuntimeError(f"Hyperliquid candle fetch failed: {last_error}")


def summarize_trade_capacity(
    active_asset_data: dict[str, Any], side: str = "buy"
) -> TradeCapacity:
    available_long, available_short = _float_pair(active_asset_data, "availableToTrade")
    max_long, max_short = _float_pair(active_asset_data, "maxTradeSzs")
    leverage = active_asset_data.get("leverage")
    leverage_value = None
    if isinstance(leverage, dict):
        leverage_value = _float_or_none(leverage.get("value"))
    mark_px = _float_or_none(active_asset_data.get("markPx"))
    wants_short = str(side).lower() in {"sell", "short"}
    available_margin = available_short if wants_short else available_long
    max_base = max_short if wants_short else max_long
    max_notional = None
    candidates: list[float] = []
    if available_margin is not None and leverage_value is not None:
        candidates.append(max(0.0, available_margin * leverage_value))
    if max_base is not None and mark_px is not None:
        candidates.append(max(0.0, max_base * mark_px))
    if candidates:
        max_notional = min(candidates)
    return TradeCapacity(
        max_notional=max_notional,
        available_margin=available_margin,
        max_position_size=max_base,
        safe=max_notional is not None and max_notional > 0,
        source="activeAssetData.availableToTrade",
        raw=active_asset_data,
    )


async def get_trade_capacity(
    label: str, asset_name: str, side: str = "buy"
) -> TradeCapacity:
    from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_get_trade_asset

    result = await hyperliquid_get_trade_asset(label=label, asset_name=asset_name)
    data = None
    if isinstance(result, dict):
        data = result.get("result") if result.get("ok") is True else result.get("data")
    if not isinstance(data, dict):
        return TradeCapacity(safe=False, source="activeAssetData.availableToTrade")
    active = data.get("active_asset_data") or data.get("raw") or data
    if not isinstance(active, dict):
        return TradeCapacity(safe=False, source="activeAssetData.availableToTrade")
    return summarize_trade_capacity(active, side=side)


def safe_place_perp_order(
    intent: OrderIntent,
    *,
    state_snapshot: StateSnapshot,
    capacity: TradeCapacity | None = None,
    raw_result: dict[str, Any] | None = None,
) -> FillEvent:
    if state_snapshot.status != "valid":
        return FillEvent(
            status="ambiguous",
            venue=intent.venue,
            symbol=intent.symbol,
            side=intent.side,
            client_order_id=intent.client_order_id,
            error=f"state snapshot is {state_snapshot.status}",
            raw=state_snapshot.to_dict(),
        )
    if intent.action == "OPEN" and (capacity is None or not capacity.safe):
        return FillEvent(
            status="rejected",
            venue=intent.venue,
            symbol=intent.symbol,
            side=intent.side,
            client_order_id=intent.client_order_id,
            error="trade capacity is not safe",
            raw=capacity.to_dict() if capacity else {},
        )
    raw = raw_result or {}
    if not raw:
        return FillEvent(
            status="ambiguous",
            venue=intent.venue,
            symbol=intent.symbol,
            side=intent.side,
            client_order_id=intent.client_order_id,
            error="no exchange result supplied",
        )
    if raw.get("status") != "ok":
        return FillEvent(
            status="rejected",
            venue=intent.venue,
            symbol=intent.symbol,
            side=intent.side,
            client_order_id=intent.client_order_id,
            error=str(raw.get("error") or raw.get("response") or "order rejected"),
            raw=raw,
        )
    statuses = ((raw.get("response") or {}).get("data") or {}).get("statuses") or []
    if any(isinstance(item, dict) and "error" in item for item in statuses):
        return FillEvent(
            status="rejected",
            venue=intent.venue,
            symbol=intent.symbol,
            side=intent.side,
            client_order_id=intent.client_order_id,
            error="exchange status contains error",
            raw=raw,
        )
    filled = next(
        (
            item.get("filled")
            for item in statuses
            if isinstance(item, dict) and isinstance(item.get("filled"), dict)
        ),
        None,
    )
    if not isinstance(filled, dict):
        return FillEvent(
            status="resting",
            venue=intent.venue,
            symbol=intent.symbol,
            side=intent.side,
            client_order_id=intent.client_order_id,
            raw=raw,
        )
    return FillEvent(
        status="filled",
        venue=intent.venue,
        symbol=intent.symbol,
        side=intent.side,
        filled_size=float(filled.get("totalSz") or intent.size or 0),
        avg_price=_float_or_none(filled.get("avgPx")),
        order_id=str(filled.get("oid")) if filled.get("oid") is not None else None,
        client_order_id=intent.client_order_id,
        reduce_only=intent.reduce_only,
        raw=raw,
    )


def _candles_to_completed_view(
    asset_name: str, rows: list[CandleEntry]
) -> CompletedBarsView:
    now_ms = int(time.time() * 1000)
    parsed: list[dict[str, Any]] = []
    for row in rows:
        close_ms = int(row.get("T") or row.get("t") or 0)
        if close_ms > now_ms:
            continue
        parsed.append(
            {
                "timestamp": close_ms,
                "symbol": asset_name,
                "open": row.get("o"),
                "high": row.get("h"),
                "low": row.get("l"),
                "close": row.get("c"),
                "volume": row.get("v"),
            }
        )
    return CompletedBarsView.from_rows(parsed)


def _float_pair(data: dict[str, Any], key: str) -> tuple[float | None, float | None]:
    values = data.get(key)
    if not isinstance(values, list) or len(values) < 2:
        return None, None
    return _float_or_none(values[0]), _float_or_none(values[1])


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
