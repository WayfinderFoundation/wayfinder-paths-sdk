from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import httpx

from wayfinder_paths.jobs.execution.hyperliquid import (
    SafeHyperliquidMarketClient,
    _cancel_needs_asset_context,
    _candles_to_completed_view,
    _hl_state_result,
    _lookback_hours,
    _paper_broker,
    _submit_market_order,
)
from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    FillEvent,
    OrderIntent,
    PositionRecord,
    StateSnapshot,
    TradeCapacity,
    _float_or_none,
)
from wayfinder_paths.jobs.execution.venues import (
    MarketEvent,
    VenueCapabilities,
    VenueState,
    register_venue,
)
from wayfinder_paths.jobs.models import utc_now_iso

HYPERLIQUID_PREDICTION_CAPABILITIES = VenueCapabilities(
    market_kind="prediction",
    supports_brackets=False,  # HL rejects trigger orders on HIP-4
    supports_shorts=False,
    supports_notional_sizing=True,  # market orders accept usd_amount
    supports_limit_orders=True,
    position_model="outcome_tokens",
    settlement="resolution",
)
MIN_ORDER_USD = 10.0
DEFAULT_RESOLUTION_EPSILON = 0.05


class DirectHyperliquidCandleClient:
    """Fallback candle source hitting HL's public candleSnapshot directly.

    The gateway client may not pass `#N` outcome coins through; this keeps
    that unknown contained to the prediction venue (the perp path never uses
    it). SDK-owned fetch — the strategy-level candle-fetch ban is untouched.
    Returns the same t/T/o/h/l/c/v rows as the gateway client."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.hyperliquid.xyz/info",
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url
        self.timeout = timeout

    async def get_candles(
        self,
        coin: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        interval: str = "1h",
        *,
        lookback_hours: int | None = None,
    ) -> list[dict[str, Any]]:
        if start_ms is None or end_ms is None:
            if lookback_hours is None:
                raise TypeError("provide start_ms/end_ms or lookback_hours")
            end_ms = int(time.time() * 1000)
            start_ms = end_ms - int(lookback_hours) * 3_600_000
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self.base_url,
                json={
                    "type": "candleSnapshot",
                    "req": {
                        "coin": coin,
                        "interval": interval,
                        "startTime": start_ms,
                        "endTime": end_ms,
                    },
                },
            )
            response.raise_for_status()
        match response.json():
            case list() as rows:
                return rows
            case _:
                return []


class HyperliquidPredictionFeed:
    """Bars for `#N` outcome coins: gateway first, direct candleSnapshot on
    failure or empty rows."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        fallback: Any | None = None,
        outcome_lister: Any | None = None,
        resolution_epsilon: float = DEFAULT_RESOLUTION_EPSILON,
    ) -> None:
        self._safe = SafeHyperliquidMarketClient(client)
        self._fallback = fallback or DirectHyperliquidCandleClient()
        self._outcome_lister = outcome_lister
        self.resolution_epsilon = resolution_epsilon

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
            view = await self._bars_for(symbol, interval, lookback_hours)
            rows.extend(view.to_rows())
        merged = CompletedBarsView.from_rows(rows)
        if as_of is not None:
            merged = merged.through(as_of)
        return merged

    async def _bars_for(
        self, symbol: str, interval: str, lookback_hours: int
    ) -> CompletedBarsView:
        try:
            view = await self._safe.get_completed_bars(
                symbol, interval, lookback_hours=lookback_hours
            )
            if len(view.to_frame()):
                return view
        except Exception:
            pass
        raw = await self._fallback.get_candles(
            symbol, interval=interval, lookback_hours=lookback_hours
        )
        return _candles_to_completed_view(symbol, raw)

    async def get_events(
        self, symbols: Sequence[str], *, since: datetime | None = None
    ) -> list[MarketEvent]:
        """Resolution inference (HIP-4 has no explicit resolution field):
        emit `resolution` ONLY when the outcome has left outcomeMeta AND the
        last completed close is terminal (within resolution_epsilon of 0/1) —
        value = round(last_close). Absent but non-terminal -> `halt`; the live
        reconcile ambiguity path handles it. Never book a fictitious settle."""
        live_assets = await self._live_outcome_assets()
        if live_assets is None:
            return []
        events: list[MarketEvent] = []
        for symbol in symbols:
            if symbol in live_assets:
                continue
            try:
                view = await self._bars_for(symbol, "1h", lookback_hours=48)
                last_close = float(view.latest(symbol)["close"])
            except Exception:
                last_close = None
            if last_close is not None and (
                last_close >= 1 - self.resolution_epsilon
                or last_close <= self.resolution_epsilon
            ):
                events.append(
                    MarketEvent(
                        kind="resolution",
                        symbol=symbol,
                        timestamp=utc_now_iso(),
                        payload={
                            "value": float(round(last_close)),
                            "venue": "hyperliquid_prediction",
                            "last_close": last_close,
                        },
                    )
                )
            else:
                events.append(
                    MarketEvent(
                        kind="halt",
                        symbol=symbol,
                        timestamp=utc_now_iso(),
                        payload={
                            "reason": "outcome absent from outcomeMeta",
                            "last_close": last_close,
                        },
                    )
                )
        return events

    async def _live_outcome_assets(self) -> set[str] | None:
        if self._outcome_lister is not None:
            return set(await self._outcome_lister())
        try:
            # lazy: HyperliquidAdapter pulls the signing/exchange stack; only the
            # no-lister live path needs it, and import failure must degrade to None
            from wayfinder_paths.adapters.hyperliquid_adapter.adapter import (
                HyperliquidAdapter,
            )

            adapter = HyperliquidAdapter()
            ok, markets = await adapter.get_outcome_markets()
        except Exception:
            return None
        if not ok:
            return None
        assets: set[str] = set()
        for market in markets or []:
            for outcome in (
                market.get("matched_outcomes") or market.get("outcomes") or []
            ):
                for side in outcome.get("sides") or []:
                    name = side.get("asset_name")
                    if name:
                        assets.add(str(name))
        return assets


class HyperliquidPredictionBroker:
    """Orders on HIP-4 outcome markets (`#N` asset names, integer contracts,
    $10 min). Exchange responses parse through safe_place_perp_order so
    resting/rejected/ambiguous never read as success."""

    capabilities = HYPERLIQUID_PREDICTION_CAPABILITIES

    def __init__(self, *, wallet_label: str = "main", slippage: float = 0.02) -> None:
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
        size: float | None = None
        usd_amount: float | None = None
        if intent.size is not None:
            if float(intent.size) != int(float(intent.size)):
                return self._reject(
                    intent, timestamp, "HIP-4 contracts are integer-sized"
                )
            size = float(int(float(intent.size)))
            reference = price
            if (
                reference
                and size * reference < MIN_ORDER_USD
                and not intent.reduce_only
            ):
                return self._reject(
                    intent,
                    timestamp,
                    f"order value {size * reference:.2f} below ${MIN_ORDER_USD:.0f} minimum",
                )
        elif intent.notional is not None:
            usd_amount = abs(float(intent.notional))
            if usd_amount < MIN_ORDER_USD and not intent.reduce_only:
                return self._reject(
                    intent,
                    timestamp,
                    f"notional {usd_amount:.2f} below ${MIN_ORDER_USD:.0f} minimum",
                )
        else:
            return self._reject(intent, timestamp, "size or notional is required")
        return await _submit_market_order(
            intent,
            snapshot=self.snapshot,
            # No activeAssetData capacity concept for HIP-4; sizing floors are
            # enforced above ($10 min) and by the exchange.
            capacity=TradeCapacity(safe=True, source="hyperliquid_prediction"),
            timestamp=timestamp,
            wallet_label=self.wallet_label,
            is_buy=str(intent.side).lower() in {"buy", "long"}
            and not intent.reduce_only,
            size=size,
            usd_amount=usd_amount,
            slippage=self.slippage,
        )

    async def fetch_state(self, symbols: Sequence[str] | Any = ()) -> VenueState:
        result = await _hl_state_result(self.wallet_label)
        positions: dict[str, PositionRecord] = {}
        for row in (result.get("outcomes") or {}).get("positions") or []:
            coin = str(row.get("coin") or "")
            total = _float_or_none(row.get("total")) or 0.0
            if not coin.startswith("+") or total <= 0:
                continue
            symbol = f"#{coin[1:]}"
            entry_ntl = _float_or_none(row.get("entryNtl")) or 0.0
            positions[symbol] = PositionRecord(
                symbol=symbol,
                side="long",
                size=total,
                avg_price=entry_ntl / total if total else 0.0,
                metadata={"hold": row.get("hold"), "source": "hyperliquid_hip4"},
            )
        return VenueState(
            positions=positions,
            source="hyperliquid_get_state.outcomes",
            fetched_at=utc_now_iso(),
        )

    async def get_capacity(self, symbol: str, side: str) -> TradeCapacity:
        return TradeCapacity(safe=True, source="hyperliquid_prediction")

    async def cancel(self, client_order_id: str) -> FillEvent:
        return _cancel_needs_asset_context("hyperliquid_prediction", client_order_id)

    def _reject(self, intent: OrderIntent, timestamp: str, error: str) -> FillEvent:
        return FillEvent(
            status="rejected",
            venue=intent.venue,
            symbol=intent.symbol,
            side=intent.side,
            client_order_id=intent.client_order_id,
            error=error,
            timestamp=timestamp,
        )


class HyperliquidPredictionAdapter:
    name = "hyperliquid_prediction"
    capabilities = HYPERLIQUID_PREDICTION_CAPABILITIES

    def __init__(self, *, mode: str, params: dict[str, Any] | None = None) -> None:
        params = params or {}
        self.feed = HyperliquidPredictionFeed(
            resolution_epsilon=float(
                params.get("resolution_epsilon") or DEFAULT_RESOLUTION_EPSILON
            )
        )
        if mode == "live":
            self.broker: Any = HyperliquidPredictionBroker(
                wallet_label=str(params.get("wallet_label") or "main"),
                slippage=float(params.get("live_slippage") or 0.02),
            )
        else:
            self.broker = _paper_broker(HYPERLIQUID_PREDICTION_CAPABILITIES, params)


def build_hyperliquid_prediction_adapter(
    *, mode: str, spec: Any = None, params: dict[str, Any] | None = None
) -> HyperliquidPredictionAdapter:
    return HyperliquidPredictionAdapter(mode=mode, params=params)


register_venue("hyperliquid_prediction", build_hyperliquid_prediction_adapter)
