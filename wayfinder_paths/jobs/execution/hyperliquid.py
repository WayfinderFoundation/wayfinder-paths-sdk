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
from wayfinder_paths.jobs.execution.paper import PaperBroker
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
    NativeProtectionResult,
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
                return hyperliquid_candles_to_completed_view(asset_name, rows)
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
        lookback_hours = _lookback_hours(lookback_bars, interval)
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

    def __init__(
        self,
        *,
        wallet_label: str = "main",
        slippage: float = 0.01,
        fee_bps: float = 4.5,
    ) -> None:
        self.wallet_label = wallet_label
        self.slippage = slippage
        # Fallback taker rate when the user-fills fee lookup cannot resolve
        # the actual venue fee — a fee_bps estimate beats recording 0.0
        # (observed live: gross-of-fee trade rows overstated PnL ~2x).
        self.fee_bps = fee_bps
        self.snapshot = StateSnapshot(status="valid")

    async def place(
        self,
        intent: OrderIntent,
        *,
        timestamp: str,
        price: float | None = None,
    ) -> FillEvent:
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
        return await _submit_market_order(
            intent,
            snapshot=self.snapshot,
            capacity=capacity,
            timestamp=timestamp,
            wallet_label=self.wallet_label,
            is_buy=str(intent.side).lower() in {"buy", "long"},
            size=intent.size,
            usd_amount=intent.notional if intent.size is None else None,
            slippage=self.slippage,
            fee_bps=self.fee_bps,
        )

    async def fetch_state(self, symbols: Sequence[str] | Any = ()) -> VenueState:
        # _hl_state_result raises on any failed leg (the tool errs whole-hog),
        # so a returned result IS a successful fetch. The tool speaks the
        # UnifiedAccount shape: flat `perp_positions` rows + a money `summary`
        # — parsing the pre-unified `perp.state.assetPositions` shape here
        # made every live tick raise "perp state fetch unsuccessful", which
        # fired reconcile_mismatch wakes until the advisor reverted the
        # operator's live switch (observed live on majors-5m-lab).
        result = await _hl_state_result(self.wallet_label)
        if "perp_positions" not in result:
            raise RuntimeError(
                "hyperliquid state fetch returned unexpected shape "
                f"(keys: {sorted(result)})"
            )
        positions: dict[str, PositionRecord] = {}
        for position in result.get("perp_positions") or []:
            szi = _float_or_none(position.get("szi"))
            coin = str(position.get("coin") or "")
            if not coin or not szi:
                continue
            positions[coin] = PositionRecord(
                symbol=coin,
                side="long" if szi > 0 else "short",
                size=abs(szi),
                avg_price=_float_or_none(position.get("entryPx")) or 0.0,
                metadata={
                    "source": "hyperliquid",
                    "unrealized_pnl": _float_or_none(position.get("unrealizedPnl")),
                    "position_value": _float_or_none(position.get("positionValue")),
                    # Observability only — exact funding accounting uses the
                    # user-funding ledger (cumFunding vanishes at close).
                    "cum_funding_since_open": _float_or_none(
                        (position.get("cumFunding") or {}).get("sinceOpen")
                    ),
                },
            )
        summary = result.get("summary") or {}
        account_value = _float_or_none(summary.get("unified_usdc_equity"))
        if account_value is None:  # classic (non-unified) account ledgers
            account_value = _float_or_none(summary.get("perp_account_value"))
        balances: dict[str, float] = {}
        if account_value is not None:
            balances["accountValue"] = account_value
        return VenueState(
            positions=positions,
            open_orders=[dict(order) for order in result.get("open_orders") or []],
            balances=balances,
            source="hyperliquid_get_state",
            fetched_at=None,
        )

    async def get_capacity(self, symbol: str, side: str) -> TradeCapacity:
        return await get_trade_capacity(self.wallet_label, symbol, side=side)

    async def get_funding_payments(self, since_ms: int) -> list[dict[str, Any]]:
        """Signed user funding rows since `since_ms` ({time_ms, coin, usdc,
        funding_rate, szi}; usdc negative = paid). Exceptions bubble — the
        driver treats funding collection as best-effort telemetry."""
        # lazy: keeps execution/ decoupled from the MCP tool stack
        from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_get_user_funding

        outcome = await hyperliquid_get_user_funding(self.wallet_label, since_ms)
        match outcome:
            case {"ok": True, "result": {"rows": list() as rows}}:
                return rows
            case _:
                raise RuntimeError(f"user funding fetch failed: {_mcp_error(outcome)}")

    async def cancel(self, client_order_id: str) -> FillEvent:
        return _cancel_needs_asset_context("hyperliquid", client_order_id)

    async def place_stop_loss(
        self,
        *,
        symbol: str,
        side: str,
        size: float,
        trigger_price: float,
        client_order_id: str,
    ) -> NativeProtectionResult:
        from wayfinder_paths.mcp.tools.hyperliquid import (
            _make_hl_adapter,
            _resolve_asset,
            hyperliquid_place_trigger_order,
        )

        try:
            # Engine stop prices are entry × stop-multiple products — never
            # on the venue tick grid. The tool is agent-facing and strictly
            # rejects off-grid prices (rounding direction is the agent's
            # decision there), so this deterministic caller aligns its own
            # price first with the same adapter flooring. Sub-tick movement
            # is risk-neutral for protection; an unplaced stop force-closes
            # the position and halts the job.
            adapter, _sender = await _make_hl_adapter(self.wallet_label)
            asset_id, _market_type = await _resolve_asset(adapter, symbol)
            trigger_price = adapter.get_valid_order_price(
                asset_id, float(trigger_price)
            )
            outcome = await hyperliquid_place_trigger_order(
                wallet_label=self.wallet_label,
                asset_name=symbol,
                tpsl="sl",
                trigger_price=trigger_price,
                is_buy=side.lower() in {"buy", "long"},
                size=size,
                is_market_trigger=True,
                reduce_only=True,
                cloid=client_order_id,
            )
        except Exception as exc:
            return await self._resolve_stop_after_error(
                symbol, client_order_id, f"trigger submission failed: {exc}"
            )
        match outcome:
            case {"ok": True, "result": dict() as payload}:
                status = str(payload.get("status") or "")
                if status == "confirmed":
                    return NativeProtectionResult(
                        status="confirmed",
                        symbol=symbol,
                        client_order_id=client_order_id,
                        order_id=_trigger_order_id(payload),
                        raw=payload,
                    )
                error = str(payload)
            case _:
                error = str(_mcp_error(outcome) or "no trigger acknowledgement")
        return await self._resolve_stop_after_error(symbol, client_order_id, error)

    async def _resolve_stop_after_error(
        self, symbol: str, client_order_id: str, error: str
    ) -> NativeProtectionResult:
        """Ambiguous submit is confirmed only when the exact cloid is open."""
        try:
            state = await _hl_state_result(self.wallet_label)
            matching = next(
                (
                    order
                    for order in state.get("open_orders") or []
                    if str(order.get("cloid") or "") == client_order_id
                ),
                None,
            )
            if matching is not None:
                return NativeProtectionResult(
                    status="confirmed",
                    symbol=symbol,
                    client_order_id=client_order_id,
                    order_id=(
                        str(matching.get("oid"))
                        if matching.get("oid") is not None
                        else None
                    ),
                    raw={"reconciled_open_order": dict(matching)},
                )
        except Exception:
            pass
        return NativeProtectionResult(
            status="ambiguous",
            symbol=symbol,
            client_order_id=client_order_id,
            error=error,
        )

    async def cancel_stop_loss(
        self, *, symbol: str, client_order_id: str
    ) -> NativeProtectionResult:
        from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_cancel_order

        try:
            outcome = await hyperliquid_cancel_order(
                wallet_label=self.wallet_label,
                asset_name=symbol,
                cancel_cloid=client_order_id,
            )
        except Exception as exc:
            return NativeProtectionResult(
                status="ambiguous",
                symbol=symbol,
                client_order_id=client_order_id,
                error=f"stop cancellation failed: {exc}",
            )
        match outcome:
            case {"ok": True, "result": {"status": "confirmed"} as payload}:
                return NativeProtectionResult(
                    status="confirmed",
                    symbol=symbol,
                    client_order_id=client_order_id,
                    raw=payload,
                )
            case _:
                return NativeProtectionResult(
                    status="ambiguous",
                    symbol=symbol,
                    client_order_id=client_order_id,
                    error=str(_mcp_error(outcome) or "stop cancellation unconfirmed"),
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
                fee_bps=(
                    float(params["fee_bps"])
                    if params.get("fee_bps") is not None
                    else 4.5
                ),
            )
        else:
            self.broker = _paper_broker(HYPERLIQUID_CAPABILITIES, params)


def build_hyperliquid_adapter(
    *, mode: str, spec: Any = None, params: dict[str, Any] | None = None
) -> HyperliquidPerpAdapter:
    return HyperliquidPerpAdapter(mode=mode, params=params)


register_venue("hyperliquid", build_hyperliquid_adapter)


def _mcp_error(outcome: Any) -> Any:
    match outcome:
        case {"error": error}:
            return error
        case dict():
            return outcome.get("message")
    return outcome


def _trigger_order_id(payload: dict[str, Any]) -> str | None:
    for effect in payload.get("effects") or []:
        if effect.get("label") != "place_trigger_order":
            continue
        statuses = ((effect.get("result") or {}).get("response") or {}).get(
            "data", {}
        ).get("statuses") or []
        for status in statuses:
            order = status.get("resting") or status.get("filled") or {}
            if order.get("oid") is not None:
                return str(order["oid"])
    return None


async def _submit_market_order(
    intent: OrderIntent,
    *,
    snapshot: StateSnapshot,
    capacity: TradeCapacity | None,
    timestamp: str,
    wallet_label: str,
    is_buy: bool,
    size: float | None,
    usd_amount: float | None,
    slippage: float,
    fee_bps: float = 0.0,
) -> FillEvent:
    """Shared MCP submit -> FillEvent normalization for the perp and HIP-4
    brokers. Transport failures and missing exchange results return
    `ambiguous`, never raise."""
    # lazy: keeps execution/ decoupled from the MCP tool stack and patchable in tests
    from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_place_market_order

    submit_ms = int(time.time() * 1000)
    try:
        outcome = await hyperliquid_place_market_order(
            wallet_label=wallet_label,
            asset_name=intent.symbol,
            is_buy=is_buy,
            size=size,
            usd_amount=usd_amount,
            slippage=slippage,
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
    raw = None
    match outcome:
        case {"ok": True, "result": dict() as result}:
            effects = result.get("effects") or []
            labeled = [e for e in effects if e.get("label") == "place_market_order"]
            hl = [e for e in effects if e.get("type") == "hl"]
            if labeled:
                raw = labeled[0].get("result")
            elif hl:
                raw = hl[-1].get("result")
    if raw is None:
        payload: dict[str, Any] = {}
        match outcome:
            case dict():
                payload = outcome
        return FillEvent(
            status="ambiguous",
            venue=intent.venue,
            symbol=intent.symbol,
            side=intent.side,
            client_order_id=intent.client_order_id,
            error=str(_mcp_error(outcome) or "no exchange result in MCP response"),
            raw=payload,
            timestamp=timestamp,
        )
    fill = safe_place_perp_order(
        intent, state_snapshot=snapshot, capacity=capacity, raw_result=raw
    )
    fill.timestamp = timestamp
    if fill.status == "filled":
        await _attach_fill_fee(
            fill, wallet_label=wallet_label, submit_ms=submit_ms, fee_bps=fee_bps
        )
    return fill


async def _user_fills_result(wallet_label: str, start_ms: int) -> list[dict[str, Any]]:
    # lazy: keeps execution/ decoupled from the MCP tool stack and patchable in tests
    from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_get_user_fills

    outcome = await hyperliquid_get_user_fills(wallet_label, start_ms=start_ms)
    match outcome:
        case {"ok": True, "result": {"rows": list() as rows}}:
            return rows
    raise RuntimeError(f"user fills fetch failed: {_mcp_error(outcome)}")


# userFills can lag the order ack; bounded backoff, then estimate.
_FEE_LOOKUP_DELAYS_S = (0.0, 1.5, 3.0)
_FEE_LOOKUP_EARLY_MS = 5_000
_FEE_LOOKUP_LATE_MS = 30_000


async def _attach_fill_fee(
    fill: FillEvent, *, wallet_label: str, submit_ms: int, fee_bps: float
) -> None:
    """Resolve the ACTUAL venue fee for a filled order via the user-fills
    ledger (the HL order ack carries no fee — recording 0.0 made live trade
    PnL gross of fees, overstating it ~2x in the observed sample). Falls
    back to a fee_bps estimate when the ledger has not caught up; the
    source is tagged either way for audit."""
    matched_fee = None
    try:
        for delay in _FEE_LOOKUP_DELAYS_S:
            if delay:
                await asyncio.sleep(delay)
            rows = await _user_fills_result(
                wallet_label, submit_ms - _FEE_LOOKUP_EARLY_MS
            )
            fee_total = 0.0
            size_total = 0.0
            for row in rows:
                time_ms = row.get("time")
                if (
                    isinstance(time_ms, (int, float))
                    and time_ms > submit_ms + _FEE_LOOKUP_LATE_MS
                ):
                    continue
                oid_match = fill.order_id is not None and str(row.get("oid")) == str(
                    fill.order_id
                )
                cloid_match = fill.client_order_id is not None and row.get(
                    "cloid"
                ) == str(fill.client_order_id)
                if not (oid_match or cloid_match):
                    continue
                fee_total += float(row.get("fee") or 0.0) + float(
                    row.get("builderFee") or 0.0
                )
                size_total += abs(float(row.get("sz") or 0.0))
            if size_total > 0:
                # Ledger may not carry every partial yet — pro-rata up.
                if size_total < abs(fill.filled_size) * 0.99:
                    fee_total *= abs(fill.filled_size) / size_total
                matched_fee = fee_total
                break
    except Exception:
        matched_fee = None
    if matched_fee is not None:
        fill.fee = matched_fee
        fill.raw["fee_source"] = "user_fills"
    else:
        notional = abs(fill.filled_size * (fill.avg_price or 0.0))
        fill.fee = notional * fee_bps / 10_000
        fill.raw["fee_source"] = "estimate"


async def _hl_state_result(wallet_label: str) -> dict[str, Any]:
    # lazy: keeps execution/ decoupled from the MCP tool stack and patchable in tests
    from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_get_state

    outcome = await hyperliquid_get_state(wallet_label)
    match outcome:
        case {"ok": True, "result": dict() as result}:
            return result
        case _:
            message = str(_mcp_error(outcome))
            if "not found" in message.lower():
                message += (
                    " — the live wallet comes from execution_params."
                    "wallet_label in job.yaml (engine default 'main'); set it "
                    "to a label from core_get_wallets()"
                )
            raise RuntimeError(f"hyperliquid_get_state failed: {message}")


def _cancel_needs_asset_context(venue: str, client_order_id: str) -> FillEvent:
    return FillEvent(
        status="rejected",
        venue=venue,
        symbol="",
        side="",
        error="cancel by cloid requires asset context; use hyperliquid_cancel_order",
        client_order_id=client_order_id,
    )


def _paper_broker(
    capabilities: VenueCapabilities, params: dict[str, Any]
) -> PaperBroker:
    raw_maker_fee = params.get("maker_fee_bps")
    return PaperBroker(
        capabilities=capabilities,
        fee_bps=float(params.get("fee_bps") or 0.0),
        maker_fee_bps=1.5 if raw_maker_fee is None else float(raw_maker_fee),
        slippage_bps=float(params.get("slippage_bps") or 0.0),
    )


def _lookback_hours(lookback_bars: int, interval: str) -> int:
    bar_seconds = bar_interval_seconds(interval) or 3600
    return max(1, math.ceil(lookback_bars * bar_seconds / 3600))


def hyperliquid_candles_to_completed_view(
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
