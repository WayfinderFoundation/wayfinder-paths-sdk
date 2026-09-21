"""Hyperliquid spot venue: tokens bought and sold on the Hyperliquid spot book.

Symbols are Hyperliquid spot pairs (``HYPE/USDC``, ``PURR/USDC``), the same
names the Hyperliquid tools use. A buy opens a long-only inventory position
priced in USDC, a sell closes it; no shorts, no leverage, no brackets. Bars
are the pair's completed candles (the venue's candle coin is `@<index>`, or
`PURR/USDC` for index 0). Paper fills go through the shared paper broker; live fills go
through the same market-order tool the perp venue uses, on the job wallet,
so a spot leg and a perp leg can share one account (delta-neutral books).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from wayfinder_paths.jobs.execution.hyperliquid import (
    SafeHyperliquidMarketClient,
    _lookback_hours,
    _paper_broker,
    _submit_market_order,
    hyperliquid_candles_to_completed_view,
)
from wayfinder_paths.jobs.execution.hyperliquid_prediction import (
    DirectHyperliquidCandleClient,
)
from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    FillEvent,
    OrderIntent,
    StateSnapshot,
    TradeCapacity,
)
from wayfinder_paths.jobs.execution.venues import (
    Broker,
    MarketDataFeed,
    MarketEvent,
    VenueAdapter,
    VenueCapabilities,
    VenueState,
    register_venue,
)

HYPERLIQUID_SPOT_CAPABILITIES = VenueCapabilities(
    market_kind="spot",
    supports_brackets=False,
    supports_shorts=False,
    supports_notional_sizing=True,
    supports_limit_orders=False,
    position_model="netting",
    settlement="continuous",
)


def is_spot_pair(symbol: str) -> bool:
    return "/" in str(symbol)


class DirectHyperliquidSpotIndex:
    """Spot pair name → universe index from HL's public spotMeta, fetched once
    per process. Index 0 is `PURR/USDC`; every other pair's candle coin is
    `@<index>`."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.hyperliquid.xyz/info",
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self._index: dict[str, int] | None = None

    async def index_for(self, pair: str) -> int | None:
        if self._index is None:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self.base_url, json={"type": "spotMeta"})
                response.raise_for_status()
            meta = response.json() or {}
            tokens = {
                int(token["index"]): str(token["name"])
                for token in meta.get("tokens") or []
                if token.get("index") is not None and token.get("name")
            }
            index: dict[str, int] = {}
            for entry in meta.get("universe") or []:
                pair_tokens = entry.get("tokens") or []
                if len(pair_tokens) < 2 or entry.get("index") is None:
                    continue
                base = tokens.get(int(pair_tokens[0]))
                quote = tokens.get(int(pair_tokens[1]))
                if base and quote:
                    index[f"{base}/{quote}"] = int(entry["index"])
            self._index = index
        return self._index.get(str(pair))


def spot_candle_coin(index: int) -> str:
    return "PURR/USDC" if int(index) == 0 else f"@{int(index)}"


class HyperliquidSpotFeed:
    """Completed candles per spot pair. A pair resolves to its candle coin
    (`PURR/USDC` for index 0, `@<index>` otherwise); the gateway candle
    client answers first, HL's public candleSnapshot on failure or empty,
    and the mid-price table stands in for a pair with no candles yet, as one
    flat bar. Symbols that are not pairs belong to another venue's feed."""

    def __init__(
        self,
        mids: Any | None = None,
        *,
        client: Any | None = None,
        fallback: Any | None = None,
        spot_index: Any | None = None,
    ) -> None:
        self._mids = mids
        self._safe = SafeHyperliquidMarketClient(client)
        self._fallback = fallback or DirectHyperliquidCandleClient()
        self._spot_index = spot_index or DirectHyperliquidSpotIndex()
        self._coins: dict[str, str | None] = {}

    async def _mid_prices(self, symbols: Sequence[str]) -> Mapping[str, Any]:
        if self._mids is not None:
            return await self._mids(list(symbols))
        # lazy: keeps execution/ decoupled from the MCP tool stack and patchable in tests
        from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_search_mid_prices

        outcome = await hyperliquid_search_mid_prices(asset_names=list(symbols))
        result = (outcome or {}).get("result") or {}
        return result.get("prices") or {}

    async def _candle_coin(self, pair: str) -> str | None:
        if pair not in self._coins:
            try:
                index = await self._spot_index.index_for(pair)
            except Exception:  # noqa: BLE001 — an unreachable index falls back to mids
                index = None
            self._coins[pair] = spot_candle_coin(index) if index is not None else None
        return self._coins[pair]

    async def _bars_for(
        self, pair: str, interval: str, lookback_hours: int
    ) -> list[dict[str, Any]]:
        coin = await self._candle_coin(pair)
        if coin is None:
            return []
        rows: list[dict[str, Any]] = []
        try:
            view = await self._safe.get_completed_bars(
                coin, interval, lookback_hours=lookback_hours
            )
            rows = view.to_rows()
        except Exception:  # noqa: BLE001 — the direct snapshot is the fallback
            rows = []
        if not rows:
            raw = await self._fallback.get_candles(
                coin, interval=interval, lookback_hours=lookback_hours
            )
            rows = hyperliquid_candles_to_completed_view(coin, raw).to_rows()
        # candles come back under the coin; the book knows the pair
        return [{**row, "symbol": pair} for row in rows]

    async def get_completed_bars(
        self,
        symbols: Sequence[str],
        interval: str,
        *,
        lookback_bars: int,
        as_of: datetime | None = None,
    ) -> CompletedBarsView:
        lookback_hours = _lookback_hours(lookback_bars, interval)
        rows: list[Mapping[str, Any]] = []
        without_candles: list[str] = []
        for symbol in symbols:
            if not is_spot_pair(symbol):
                continue
            bars = await self._bars_for(str(symbol), interval, lookback_hours)
            if bars:
                rows.extend(bars)
            else:
                without_candles.append(str(symbol))
        if without_candles:
            stamp = (as_of or datetime.now(UTC)).isoformat()
            prices = await self._mid_prices(without_candles)
            for symbol in without_candles:
                mid = prices.get(symbol)
                if mid is None:
                    continue
                value = float(mid)
                rows.append(
                    {
                        "timestamp": stamp,
                        "symbol": symbol,
                        "open": value,
                        "high": value,
                        "low": value,
                        "close": value,
                    }
                )
        merged = CompletedBarsView.from_rows(rows)
        if as_of is not None:
            merged = merged.through(as_of)
        return merged

    async def get_events(
        self, symbols: Sequence[str], *, since: datetime | None = None
    ) -> list[MarketEvent]:
        return []


class HyperliquidSpotBroker:
    """Live broker over the shared market-order submit path. Spot has no
    margin snapshot to reconcile, so the submit runs against a valid empty
    state and the fee comes from the user-fills ledger like a perp fill."""

    capabilities = HYPERLIQUID_SPOT_CAPABILITIES

    def __init__(
        self, *, wallet_label: str, slippage: float = 0.01, fee_bps: float = 7.0
    ) -> None:
        if not wallet_label:
            raise ValueError(
                "live hyperliquid_spot trading needs the job's wallet_label"
            )
        self.wallet_label = wallet_label
        self.slippage = float(slippage)
        self.fee_bps = float(fee_bps)

    async def place(
        self,
        intent: OrderIntent,
        *,
        timestamp: str,
        price: float | None = None,
    ) -> FillEvent:
        if not is_spot_pair(intent.symbol):
            return self._reject(
                intent, timestamp, "hyperliquid_spot symbols are pairs like HYPE/USDC"
            )
        if intent.limit_price is not None:
            return self._reject(
                intent,
                timestamp,
                "spot fills at market; limit orders are not supported",
            )
        if intent.action == "OPEN" and intent.side != "long":
            return self._reject(
                intent,
                timestamp,
                "hyperliquid_spot is spot: it holds tokens and cannot short",
            )
        buying = intent.action == "OPEN"
        size = intent.size if not buying or intent.notional is None else None
        usd_amount = intent.notional if buying and intent.size is None else None
        if buying and size is None and not usd_amount:
            return self._reject(
                intent, timestamp, "a buy needs a USD notional or a size"
            )
        if not buying and (size is None or float(size) <= 0):
            return self._reject(
                intent, timestamp, "a sell needs the token size to sell"
            )
        return await _submit_market_order(
            intent,
            snapshot=StateSnapshot(status="valid"),
            # Spot has no margin capacity to check: the order is sized in USDC
            # the wallet either has or the venue rejects.
            capacity=TradeCapacity(safe=True, source="hyperliquid_spot"),
            timestamp=timestamp,
            wallet_label=self.wallet_label,
            is_buy=buying,
            size=size,
            usd_amount=usd_amount,
            slippage=self.slippage,
            fee_bps=self.fee_bps,
        )

    async def fetch_state(self, symbols: Sequence[str] | Any = ()) -> VenueState:
        return VenueState(source="hyperliquid_spot")

    async def get_capacity(self, symbol: str, side: str) -> TradeCapacity:
        return TradeCapacity(safe=True, source="hyperliquid_spot")

    async def cancel(self, client_order_id: str) -> FillEvent:
        return FillEvent(
            status="rejected",
            venue="hyperliquid_spot",
            symbol="",
            side="",
            error="spot market orders do not rest; nothing to cancel",
            client_order_id=client_order_id,
        )

    def _reject(self, intent: OrderIntent, timestamp: str, error: str) -> FillEvent:
        return FillEvent(
            status="rejected",
            venue=intent.venue,
            symbol=intent.symbol,
            side=intent.side,
            client_order_id=intent.client_order_id,
            reduce_only=intent.reduce_only,
            error=error,
            timestamp=timestamp,
        )


class HyperliquidSpotAdapter:
    name = "hyperliquid_spot"
    capabilities = HYPERLIQUID_SPOT_CAPABILITIES
    feed: MarketDataFeed
    broker: Broker

    def __init__(self, *, mode: str, params: dict[str, Any] | None = None) -> None:
        params = params or {}
        self.feed = HyperliquidSpotFeed()
        if mode == "live":
            self.broker = HyperliquidSpotBroker(
                wallet_label=str(params.get("wallet_label") or ""),
                slippage=float(params.get("live_slippage") or 0.01),
                fee_bps=float(params["fee_bps"])
                if params.get("fee_bps") is not None
                else 7.0,
            )
        else:
            self.broker = _paper_broker(
                HYPERLIQUID_SPOT_CAPABILITIES, params, venue="hyperliquid_spot"
            )


def build_hyperliquid_spot_adapter(
    *, mode: str, spec: Any = None, params: dict[str, Any] | None = None
) -> VenueAdapter:
    return HyperliquidSpotAdapter(mode=mode, params=params)


register_venue(
    "hyperliquid_spot",
    build_hyperliquid_spot_adapter,
    capabilities=HYPERLIQUID_SPOT_CAPABILITIES,
)
