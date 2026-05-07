"""RecordingHandler — delegating wrapper that captures `place_order` calls.

Wraps any `MarketHandler` (live, backtest, recon) and tees every order intent
into an internal list. Every other protocol method passes through unchanged.

Used by `ActivePerpsStrategy._run_trigger` to capture live intents into the
state snapshot so the reconciler can diff replay-intents vs *what live actually
intended* (strict), in addition to the existing replay-intents-vs-fills axis.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from wayfinder_paths.core.perps.handlers.protocol import (
    MarketHandler,
    Order,
    OrderBook,
    OrderResult,
    OrderType,
    Position,
    Side,
)


class RecordingHandler:
    """Wrap a `MarketHandler`, log `place_order` intents, pass through everything else."""

    def __init__(self, inner: MarketHandler):
        self._inner = inner
        self.intents: list[dict[str, Any]] = []

    @property
    def venue(self) -> str:
        return self._inner.venue

    # ---------- writes (intercepted) ----------
    async def place_order(
        self,
        symbol: str,
        side: Side,
        size: float,
        order_type: OrderType,
        limit_price: float | None = None,
        reduce_only: bool = False,
    ) -> OrderResult:
        result = await self._inner.place_order(
            symbol,
            side,
            size,
            order_type,
            limit_price=limit_price,
            reduce_only=reduce_only,
        )
        # Log every attempted order — successful or rejected — so the reconciler
        # can tell "live tried but rejected" from "live didn't try at all".
        self.intents.append(
            {
                "symbol": symbol,
                "side": side,
                "size": size,
                "order_type": order_type,
                "limit_price": limit_price,
                "reduce_only": reduce_only,
                "venue": self.venue,
                "ok": result.ok,
                "fill_price": result.fill_price,
                "fill_size": result.fill_size,
                "order_id": result.order_id,
                "error": result.error,
            }
        )
        return result

    async def cancel(self, order_id: str) -> bool:
        return await self._inner.cancel(order_id)

    # ---------- everything else passes through ----------
    async def get_positions(self) -> dict[str, Position]:
        return await self._inner.get_positions()

    async def get_open_orders(self) -> list[Order]:
        return await self._inner.get_open_orders()

    def mid(self, symbol: str) -> float:
        return self._inner.mid(symbol)

    def funding(self, symbol: str) -> float:
        return self._inner.funding(symbol)

    async def orderbook(self, symbol: str, depth: int = 10) -> OrderBook:
        return await self._inner.orderbook(symbol, depth)

    async def quantity_at_price(
        self, symbol: str, side: Side, target_price: float
    ) -> float:
        return await self._inner.quantity_at_price(symbol, side, target_price)

    async def price_for_quantity(self, symbol: str, side: Side, qty: float) -> float:
        return await self._inner.price_for_quantity(symbol, side, qty)

    async def reservable_size(
        self,
        symbol: str,
        side: Side,
        requested_size: float,
        *,
        free_margin: float,
        leverage: float = 1.0,
        cost_bps: float = 0.0,
    ) -> float:
        return await self._inner.reservable_size(
            symbol,
            side,
            requested_size,
            free_margin=free_margin,
            leverage=leverage,
            cost_bps=cost_bps,
        )

    async def recent_prices(
        self, symbols: list[str], lookback_bars: int
    ) -> pd.DataFrame:
        return await self._inner.recent_prices(symbols, lookback_bars)

    async def recent_funding(
        self, symbols: list[str], lookback_bars: int
    ) -> pd.DataFrame:
        return await self._inner.recent_funding(symbols, lookback_bars)

    async def get_margin_balance(self) -> float:
        return await self._inner.get_margin_balance()

    async def transfer_in(self, amount: float) -> OrderResult:
        return await self._inner.transfer_in(amount)

    async def transfer_out(self, amount: float) -> OrderResult:
        return await self._inner.transfer_out(amount)

    def now(self) -> datetime:
        return self._inner.now()

    @property
    def inner(self) -> MarketHandler:
        return self._inner
