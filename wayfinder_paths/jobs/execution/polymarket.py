from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    FillEvent,
    OrderIntent,
    PositionRecord,
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
from wayfinder_paths.jobs.models import utc_now_iso

POLYMARKET_CAPABILITIES = VenueCapabilities(
    market_kind="prediction",
    supports_brackets=False,
    supports_shorts=False,
    supports_notional_sizing=True,  # BUY sizing is pUSD collateral
    supports_limit_orders=True,
    position_model="outcome_tokens",
    settlement="resolution",
)


def parse_prediction_symbol(symbol: str) -> tuple[str, str]:
    """`polymarket:<market_id>:<OUTCOME>` -> (market_id, outcome).
    market_id is a Gamma slug or a 0x condition id."""
    parts = str(symbol).split(":", 2)
    if len(parts) != 3 or parts[0] != "polymarket" or not parts[1] or not parts[2]:
        raise ValueError(
            f"prediction symbol must be polymarket:<market_id>:<OUTCOME>, "
            f"got {symbol!r}"
        )
    return parts[1], parts[2]


def _adapter() -> Any:
    from wayfinder_paths.adapters.polymarket_adapter.adapter import PolymarketAdapter

    return PolymarketAdapter()


class PolymarketResolver:
    """Cached symbol -> {token_id, outcome_index, market} resolution shared by
    the feed and broker so Gamma is hit once per market."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self._cache: dict[str, dict[str, Any]] = {}

    async def resolve(self, symbol: str) -> dict[str, Any]:
        if symbol in self._cache:
            return self._cache[symbol]
        market_id, outcome = parse_prediction_symbol(symbol)
        ok, market = await self._fetch_market(market_id)
        if not ok:
            raise RuntimeError(f"polymarket market lookup failed: {market}")
        ok_token, token_id = self.adapter.resolve_clob_token_id(
            market=market, outcome=outcome
        )
        if not ok_token:
            raise RuntimeError(f"polymarket outcome resolution failed: {token_id}")
        token_ids = [str(t) for t in market.get("clobTokenIds") or []]
        entry = {
            "token_id": str(token_id),
            "outcome_index": token_ids.index(str(token_id)),
            "market": market,
            "market_id": market_id,
            "outcome": outcome,
        }
        self._cache[symbol] = entry
        return entry

    async def refresh_market(self, symbol: str) -> dict[str, Any]:
        entry = await self.resolve(symbol)
        ok, market = await self._fetch_market(entry["market_id"])
        if ok:
            entry["market"] = market
        return entry

    def symbol_for_token(self, token_id: str) -> str | None:
        for symbol, entry in self._cache.items():
            if entry["token_id"] == str(token_id):
                return symbol
        return None

    async def _fetch_market(self, market_id: str) -> tuple[bool, Any]:
        if market_id.startswith("0x"):
            return await self.adapter.get_market_by_condition_id(
                condition_id=market_id
            )
        return await self.adapter.get_market_by_slug(market_id)


class PolymarketMarketFeed:
    """Bars from CLOB /prices-history mid samples.

    The endpoint returns sampled midpoints {t, p} (prices in [0,1]), not OHLC
    aggregates, so each bucketed sample becomes a degenerate o=h=l=c bar —
    fabricating highs/lows would hand the bracket engine data that never
    existed (brackets are rejected on this venue anyway). bar_interval must be
    expressible in whole minutes (fidelity is minutes-based)."""

    def __init__(
        self,
        adapter: Any | None = None,
        *,
        resolver: PolymarketResolver | None = None,
    ) -> None:
        self.adapter = adapter or _adapter()
        self.resolver = resolver or PolymarketResolver(self.adapter)

    async def get_completed_bars(
        self,
        symbols: Sequence[str],
        interval: str,
        *,
        lookback_bars: int,
        as_of: datetime | None = None,
    ) -> CompletedBarsView:
        bar_seconds = bar_interval_seconds(interval)
        if not bar_seconds or bar_seconds % 60:
            raise ValueError(
                f"polymarket bars need a whole-minute interval, got {interval!r}"
            )
        end_ts = int(as_of.timestamp()) if as_of else int(pd.Timestamp.now(tz="UTC").timestamp())
        start_ts = end_ts - lookback_bars * bar_seconds
        rows: list[dict[str, Any]] = []
        for symbol in symbols:
            entry = await self.resolver.resolve(symbol)
            ok, payload = await self.adapter.get_prices_history(
                token_id=entry["token_id"],
                interval=None,  # startTs/endTs mode; interval is a range preset
                start_ts=start_ts,
                end_ts=end_ts,
                fidelity=bar_seconds // 60,
            )
            if not ok:
                raise RuntimeError(f"polymarket price history failed: {payload}")
            buckets: dict[int, float] = {}
            for sample in payload.get("history") or []:
                ts = int(sample["t"])
                price = float(sample["p"])
                close_ts = (ts // bar_seconds + 1) * bar_seconds
                if close_ts > end_ts:
                    continue  # in-progress bucket
                buckets[close_ts] = price  # last sample in bucket wins
            for close_ts, price in sorted(buckets.items()):
                rows.append(
                    {
                        "timestamp": pd.Timestamp(close_ts, unit="s", tz="UTC"),
                        "symbol": symbol,
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price,
                        "volume": None,
                    }
                )
        if not rows:
            raise RuntimeError(
                f"polymarket returned no price history for {list(symbols)}"
            )
        return CompletedBarsView.from_rows(rows)

    async def get_events(
        self, symbols: Sequence[str], *, since: datetime | None = None
    ) -> list[MarketEvent]:
        events: list[MarketEvent] = []
        for symbol in symbols:
            entry = await self.resolver.refresh_market(symbol)
            market = entry["market"]
            if not market.get("closed"):
                continue
            prices = market.get("outcomePrices") or []
            index = entry["outcome_index"]
            if index >= len(prices):
                continue
            events.append(
                MarketEvent(
                    kind="resolution",
                    symbol=symbol,
                    timestamp=utc_now_iso(),
                    payload={
                        "value": float(prices[index]),
                        "venue": "polymarket",
                        "condition_id": market.get("conditionId"),
                    },
                )
            )
        return events


class PolymarketBroker:
    """Broker over the CLOB order paths. BUY sizing is pUSD collateral;
    fills are normalized so filled_size is SHARES and avg_price is
    collateral/shares — mixing those up corrupts the ledger."""

    capabilities = POLYMARKET_CAPABILITIES

    def __init__(
        self,
        adapter: Any | None = None,
        *,
        resolver: PolymarketResolver | None = None,
        slippage_pct: float = 2.0,
    ) -> None:
        self.adapter = adapter or _adapter()
        self.resolver = resolver or PolymarketResolver(self.adapter)
        self.slippage_pct = slippage_pct
        self._order_ids: dict[str, str] = {}  # client_order_id -> CLOB orderID

    async def place(
        self,
        intent: OrderIntent,
        *,
        timestamp: str,
        price: float | None = None,
    ) -> FillEvent:
        try:
            entry = await self.resolver.resolve(intent.symbol)
        except Exception as exc:
            return self._fill(
                intent, "rejected", timestamp, error=f"symbol resolution: {exc}"
            )
        token_id = entry["token_id"]
        side = "SELL" if intent.reduce_only or intent.action != "OPEN" else "BUY"
        try:
            if intent.limit_price is not None:
                if intent.size is None:
                    return self._fill(
                        intent,
                        "rejected",
                        timestamp,
                        error="limit orders require size in shares",
                    )
                ok, resp = await self.adapter.place_limit_order(
                    token_id=token_id,
                    side=side,
                    price=float(intent.limit_price),
                    size=float(intent.size),
                )
            else:
                amount = await self._market_amount(
                    intent, side, token_id, ref_price=price
                )
                if amount is None:
                    return self._fill(
                        intent,
                        "rejected",
                        timestamp,
                        error="market orders need notional (BUY) or size (SELL)",
                    )
                ok, resp = await self.adapter.place_market_order(
                    token_id=token_id,
                    side=side,
                    amount=amount,
                    max_slippage_pct=self.slippage_pct,
                )
        except Exception as exc:
            return self._fill(intent, "ambiguous", timestamp, error=str(exc))
        if not ok:
            if isinstance(resp, dict):
                return self._fill(
                    intent,
                    "rejected",
                    timestamp,
                    error=str(resp.get("error") or resp),
                    raw=resp,
                )
            return self._fill(intent, "ambiguous", timestamp, error=str(resp))
        return self._fill_from_clob_response(intent, side, resp, timestamp)

    async def fetch_state(self, symbols: Sequence[str] | Any = ()) -> VenueState:
        token_to_symbol: dict[str, str] = {}
        for symbol in symbols or []:
            entry = await self.resolver.resolve(symbol)
            token_to_symbol[entry["token_id"]] = symbol
        user = self.adapter.deposit_wallet_address()
        ok, rows = await self.adapter.get_positions(user=user)
        if not ok:
            raise RuntimeError(f"polymarket positions fetch failed: {rows}")
        positions: dict[str, PositionRecord] = {}
        for row in rows:
            token_id = str(row.get("asset") or "")
            symbol = token_to_symbol.get(token_id)
            if symbol is None:
                continue  # other strategies' tokens on the shared wallet
            size = _float_or_none(row.get("size")) or 0.0
            if size <= 0:
                continue
            positions[symbol] = PositionRecord(
                symbol=symbol,
                side="long",
                size=size,
                avg_price=_float_or_none(row.get("avgPrice")) or 0.0,
                metadata={
                    "redeemable": row.get("redeemable"),
                    "cur_price": row.get("curPrice"),
                },
            )
        return VenueState(
            positions=positions, source="polymarket_positions", fetched_at=utc_now_iso()
        )

    async def get_capacity(self, symbol: str, side: str) -> TradeCapacity:
        try:
            entry = await self.resolver.resolve(symbol)
            ok, book = await self.adapter.get_order_book(token_id=entry["token_id"])
        except Exception:
            return TradeCapacity(safe=False, source="polymarket_book")
        if not ok or not isinstance(book, dict):
            return TradeCapacity(safe=False, source="polymarket_book")
        levels = book.get("asks") if str(side).lower() in {"buy", "long"} else book.get("bids")
        depth = 0.0
        for level in levels or []:
            price = _float_or_none(level.get("price"))
            size = _float_or_none(level.get("size"))
            if price and size:
                depth += price * size
        return TradeCapacity(
            max_notional=depth, safe=depth > 0, source="polymarket_book"
        )

    async def cancel(self, client_order_id: str) -> FillEvent:
        order_id = self._order_ids.get(client_order_id)
        if not order_id:
            return FillEvent(
                status="rejected",
                venue="polymarket",
                symbol="",
                side="",
                client_order_id=client_order_id,
                error="unknown client_order_id",
            )
        try:
            ok, resp = await self.adapter.cancel_order(order_id=order_id)
        except Exception as exc:
            return FillEvent(
                status="ambiguous",
                venue="polymarket",
                symbol="",
                side="",
                client_order_id=client_order_id,
                error=str(exc),
            )
        return FillEvent(
            status="filled" if ok else "rejected",
            venue="polymarket",
            symbol="",
            side="",
            client_order_id=client_order_id,
            order_id=order_id,
            error=None if ok else str(resp),
        )

    async def _market_amount(
        self,
        intent: OrderIntent,
        side: str,
        token_id: str,
        *,
        ref_price: float | None,
    ) -> float | None:
        if side == "SELL":
            return float(intent.size) if intent.size is not None else None
        if intent.notional is not None:
            return abs(float(intent.notional))
        if intent.size is None:
            return None
        price = ref_price
        if not price:
            ok, quote = await self.adapter.get_price(token_id=token_id, side="BUY")
            price = _float_or_none(quote.get("price")) if ok else None
        return float(intent.size) * float(price) if price else None

    def _fill_from_clob_response(
        self, intent: OrderIntent, side: str, resp: dict[str, Any], timestamp: str
    ) -> FillEvent:
        status = str(resp.get("status") or "").lower()
        making = _float_or_none(resp.get("makingAmount"))
        taking = _float_or_none(resp.get("takingAmount"))
        if side == "BUY":
            collateral, shares = making, taking
        else:
            shares, collateral = making, taking
        order_id = str(resp.get("orderID") or "") or None
        if order_id and intent.client_order_id:
            self._order_ids[intent.client_order_id] = order_id
        if status == "matched" and shares:
            return FillEvent(
                status="filled",
                venue=intent.venue,
                symbol=intent.symbol,
                side=intent.side,
                filled_size=float(shares),
                avg_price=(
                    float(collateral) / float(shares) if collateral else None
                ),
                order_id=order_id,
                client_order_id=intent.client_order_id,
                reduce_only=intent.reduce_only,
                raw=resp,
                timestamp=timestamp,
            )
        if status in {"live", "delayed"}:
            return self._fill(
                intent, "resting", timestamp, order_id=order_id, raw=resp
            )
        if resp.get("errorMsg") or status == "unmatched":
            return self._fill(
                intent,
                "rejected",
                timestamp,
                error=str(resp.get("errorMsg") or "order unmatched"),
                raw=resp,
            )
        return self._fill(
            intent,
            "ambiguous",
            timestamp,
            error=f"unrecognized CLOB status {status!r}",
            raw=resp,
        )

    def _fill(
        self,
        intent: OrderIntent,
        status: str,
        timestamp: str,
        *,
        error: str | None = None,
        order_id: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> FillEvent:
        return FillEvent(
            status=status,  # type: ignore[arg-type]
            venue=intent.venue,
            symbol=intent.symbol,
            side=intent.side,
            order_id=order_id,
            client_order_id=intent.client_order_id,
            reduce_only=intent.reduce_only,
            error=error,
            raw=raw or {},
            timestamp=timestamp,
        )


class PolymarketVenueAdapter:
    name = "polymarket"
    capabilities = POLYMARKET_CAPABILITIES

    def __init__(self, *, mode: str, params: dict[str, Any] | None = None) -> None:
        params = params or {}
        adapter = _adapter()
        resolver = PolymarketResolver(adapter)
        self.feed = PolymarketMarketFeed(adapter, resolver=resolver)
        if mode == "live":
            self.broker: Any = PolymarketBroker(
                adapter,
                resolver=resolver,
                slippage_pct=float(params.get("slippage_pct") or 2.0),
            )
        else:
            from wayfinder_paths.jobs.execution.paper import PaperBroker

            self.broker = PaperBroker(
                capabilities=POLYMARKET_CAPABILITIES,
                fee_bps=float(params.get("fee_bps") or 0.0),
                slippage_bps=float(params.get("slippage_bps") or 0.0),
            )


def build_polymarket_adapter(
    *, mode: str, spec: Any = None, params: dict[str, Any] | None = None
) -> PolymarketVenueAdapter:
    return PolymarketVenueAdapter(mode=mode, params=params)


register_venue("polymarket", build_polymarket_adapter)
