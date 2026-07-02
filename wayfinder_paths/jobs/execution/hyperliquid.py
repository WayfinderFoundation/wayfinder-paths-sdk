from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import pandas as pd

from wayfinder_paths.core.clients.HyperliquidDataClient import (
    HYPERLIQUID_DATA_CLIENT,
    CandleEntry,
    HyperliquidDataClient,
)
from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    FillEvent,
    OrderIntent,
    PositionRecord,
    StateSnapshot,
    TradeCapacity,
    _float_or_none,
    bar_interval_seconds,
)
from wayfinder_paths.jobs.execution.venues import (
    MarketEvent,
    VenueCapabilities,
    VenueState,
    register_venue,
)

HYPERLIQUID_CAPABILITIES = VenueCapabilities(
    market_kind="perp",
    supports_brackets=True,
    supports_shorts=True,
    supports_notional_sizing=True,
    supports_limit_orders=True,
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
    leverage_value = None
    match active_asset_data.get("leverage"):
        case dict() as leverage:
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
    # lazy: keeps execution/ decoupled from the MCP tool stack (backtest path never loads it) and patchable in tests
    from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_get_trade_asset

    result = await hyperliquid_get_trade_asset(label=label, asset_name=asset_name)
    unsafe = TradeCapacity(safe=False, source="activeAssetData.availableToTrade")
    data = None
    match result:
        case dict() if result.get("ok") is True:
            data = result.get("result")
        case dict():
            data = result.get("data")
    match data:
        case dict():
            active = data.get("active_asset_data") or data.get("raw") or data
            match active:
                case dict():
                    return summarize_trade_capacity(active, side=side)
    return unsafe


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
    for item in statuses:
        match item:
            case {"error": _}:
                return FillEvent(
                    status="rejected",
                    venue=intent.venue,
                    symbol=intent.symbol,
                    side=intent.side,
                    client_order_id=intent.client_order_id,
                    error="exchange status contains error",
                    raw=raw,
                )
    filled = None
    for item in statuses:
        match item:
            case {"filled": dict() as fill}:
                filled = fill
                break
    if filled is None:
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


class HyperliquidMarketFeed:
    """MarketDataFeed over the SDK Hyperliquid data client — the same candle
    path validation and backtest dataset building use, so live never fetches
    differently than what was validated."""

    def __init__(self, client: HyperliquidDataClient | None = None) -> None:
        self._safe = SafeHyperliquidMarketClient(client)

    async def get_completed_bars(
        self,
        symbols: Sequence[str],
        interval: str,
        *,
        lookback_bars: int,
        as_of: datetime | None = None,
    ) -> CompletedBarsView:
        bar_seconds = bar_interval_seconds(interval) or 3600
        lookback_hours = max(1, math.ceil(lookback_bars * bar_seconds / 3600))
        rows: list[dict[str, Any]] = []
        for symbol in symbols:
            view = await self._safe.get_completed_bars(
                symbol, interval, lookback_hours=lookback_hours
            )
            rows.extend(view.to_rows())
        merged = CompletedBarsView.from_rows(rows)
        if as_of is not None:
            merged = merged.through(as_of)
        return merged

    async def get_events(
        self, symbols: Sequence[str], *, since: datetime | None = None
    ) -> list[MarketEvent]:
        return []


class HyperliquidPerpBroker:
    """Live Broker over the MCP Hyperliquid order tools.

    All exchange responses are parsed through safe_place_perp_order, so
    resting/rejected/ambiguous outcomes surface as explicit fill statuses that
    the ledger refuses to treat as success. Transport failures return
    `ambiguous`, never raise — an ambiguous fill must not clear state.
    """

    capabilities = HYPERLIQUID_CAPABILITIES

    def __init__(self, *, wallet_label: str = "main", slippage: float = 0.01) -> None:
        self.wallet_label = wallet_label
        self.slippage = slippage
        self.snapshot = StateSnapshot(status="valid")

    async def place(
        self,
        intent: OrderIntent,
        *,
        timestamp: str,
        price: float | None = None,
    ) -> FillEvent:
        from wayfinder_paths.mcp.tools.hyperliquid import (
            hyperliquid_place_market_order,
        )

        capacity: TradeCapacity | None = None
        if intent.action == "OPEN":
            try:
                capacity = await get_trade_capacity(
                    self.wallet_label, intent.symbol, side=intent.side
                )
            except Exception:
                capacity = None
        if self.snapshot.status != "valid" or (
            intent.action == "OPEN" and (capacity is None or not capacity.safe)
        ):
            fill = safe_place_perp_order(
                intent,
                state_snapshot=self.snapshot,
                capacity=capacity,
                raw_result=None,
            )
            fill.timestamp = timestamp
            return fill
        try:
            outcome = await hyperliquid_place_market_order(
                wallet_label=self.wallet_label,
                asset_name=intent.symbol,
                is_buy=str(intent.side).lower() in {"buy", "long"},
                size=intent.size,
                usd_amount=intent.notional if intent.size is None else None,
                slippage=self.slippage,
                reduce_only=intent.reduce_only,
                cloid=intent.client_order_id,
            )
        except Exception as exc:
            return FillEvent(
                status="ambiguous",
                venue=intent.venue,
                symbol=intent.symbol,
                side=intent.side,
                client_order_id=intent.client_order_id,
                error=f"order submission failed: {exc}",
                timestamp=timestamp,
            )
        raw = _exchange_result_from_mcp(outcome)
        if raw is None:
            return FillEvent(
                status="ambiguous",
                venue=intent.venue,
                symbol=intent.symbol,
                side=intent.side,
                client_order_id=intent.client_order_id,
                error=str(_mcp_error(outcome) or "no exchange result in MCP response"),
                raw=outcome if isinstance(outcome, dict) else {},
                timestamp=timestamp,
            )
        fill = safe_place_perp_order(
            intent,
            state_snapshot=self.snapshot,
            capacity=capacity,
            raw_result=raw,
        )
        fill.timestamp = timestamp
        return fill

    async def fetch_state(self, symbols: Sequence[str] | Any = ()) -> VenueState:
        from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_get_state

        outcome = await hyperliquid_get_state(self.wallet_label)
        match outcome:
            case {"ok": True, "result": dict() as result}:
                pass
            case _:
                raise RuntimeError(
                    f"hyperliquid_get_state failed: {_mcp_error(outcome)}"
                )
        perp = (result.get("perp") or {}).get("state") or {}
        if not (result.get("perp") or {}).get("success"):
            raise RuntimeError("hyperliquid perp state fetch unsuccessful")
        positions: dict[str, PositionRecord] = {}
        for item in perp.get("assetPositions") or []:
            position = item.get("position") or {}
            szi = _float_or_none(position.get("szi"))
            coin = str(position.get("coin") or "")
            if not coin or not szi:
                continue
            positions[coin] = PositionRecord(
                symbol=coin,
                side="long" if szi > 0 else "short",
                size=abs(szi),
                avg_price=_float_or_none(position.get("entryPx")) or 0.0,
                metadata={"source": "hyperliquid"},
            )
        balances: dict[str, float] = {}
        margin = perp.get("crossMarginSummary") or {}
        account_value = _float_or_none(margin.get("accountValue"))
        if account_value is not None:
            balances["accountValue"] = account_value
        return VenueState(
            positions=positions,
            open_orders=[],
            balances=balances,
            source="hyperliquid_get_state",
            fetched_at=None,
        )

    async def get_capacity(self, symbol: str, side: str) -> TradeCapacity:
        return await get_trade_capacity(self.wallet_label, symbol, side=side)

    async def cancel(self, client_order_id: str) -> FillEvent:
        return FillEvent(
            status="rejected",
            venue="hyperliquid",
            symbol="",
            side="",
            error="cancel by cloid requires asset context; use hyperliquid_cancel_order",
            client_order_id=client_order_id,
        )


class HyperliquidPerpAdapter:
    name = "hyperliquid"
    capabilities = HYPERLIQUID_CAPABILITIES

    def __init__(self, *, mode: str, params: dict[str, Any] | None = None) -> None:
        params = params or {}
        self.feed = HyperliquidMarketFeed()
        if mode == "live":
            self.broker: Any = HyperliquidPerpBroker(
                wallet_label=str(params.get("wallet_label") or "main"),
                slippage=float(params.get("live_slippage") or 0.01),
            )
        else:
            from wayfinder_paths.jobs.execution.paper import PaperBroker

            self.broker = PaperBroker(
                capabilities=HYPERLIQUID_CAPABILITIES,
                fee_bps=float(params.get("fee_bps") or 0.0),
                slippage_bps=float(params.get("slippage_bps") or 0.0),
            )


def build_hyperliquid_adapter(
    *, mode: str, spec: Any = None, params: dict[str, Any] | None = None
) -> HyperliquidPerpAdapter:
    return HyperliquidPerpAdapter(mode=mode, params=params)


register_venue("hyperliquid", build_hyperliquid_adapter)


def _exchange_result_from_mcp(outcome: Any) -> dict[str, Any] | None:
    match outcome:
        case {"ok": True, "result": dict() as result}:
            for effect in result.get("effects") or []:
                if effect.get("label") == "place_market_order":
                    return effect.get("result")
            for effect in reversed(result.get("effects") or []):
                if effect.get("type") == "hl":
                    return effect.get("result")
    return None


def _mcp_error(outcome: Any) -> Any:
    match outcome:
        case {"error": error}:
            return error
        case dict():
            return outcome.get("message")
    return outcome


def _candles_to_completed_view(
    asset_name: str, rows: list[CandleEntry]
) -> CompletedBarsView:
    now_ms = int(time.time() * 1000)
    parsed: list[dict[str, Any]] = []
    for row in rows:
        close_ms = row["T"]
        if close_ms > now_ms:
            continue
        parsed.append(
            {
                # Explicit ms conversion: CompletedBarsView's pd.to_datetime
                # has no unit=, so a raw ms int would parse as NANOSECONDS
                # (1970 epoch) and every live bar would read as stale.
                "timestamp": pd.Timestamp(int(close_ms), unit="ms", tz="UTC"),
                "symbol": asset_name,
                "open": row["o"],
                "high": row["h"],
                "low": row["l"],
                "close": row["c"],
                "volume": row.get("v"),
            }
        )
    return CompletedBarsView.from_rows(parsed)


def _float_pair(data: dict[str, Any], key: str) -> tuple[float | None, float | None]:
    match data.get(key):
        case [first, second, *_]:
            return _float_or_none(first), _float_or_none(second)
        case _:
            return None, None
