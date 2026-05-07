"""MarketHandler protocol — same surface for live, backtest, and reconcile handlers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

import pandas as pd

Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit", "ioc_limit"]


@dataclass
class OrderResult:
    ok: bool
    venue: str
    symbol: str
    side: Side
    size: float
    order_type: OrderType
    limit_price: float | None = None
    fill_price: float | None = None
    fill_size: float = 0.0
    fee_paid: float = 0.0
    order_id: str | None = None
    reduce_only: bool = False
    timestamp: datetime | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Position:
    symbol: str
    size: float  # signed: + long, - short
    entry_price: float
    mark_price: float
    notional: float  # |size| * mark_price
    unrealized_pnl: float = 0.0
    leverage: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Order:
    order_id: str
    symbol: str
    side: Side
    size: float
    order_type: OrderType
    limit_price: float | None
    placed_at: datetime
    venue: str
    reduce_only: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderBook:
    symbol: str
    bids: list[tuple[float, float]]  # (price, size), best first
    asks: list[tuple[float, float]]  # (price, size), best first
    timestamp: datetime
    venue: str


@runtime_checkable
class MarketHandler(Protocol):
    """Single perp venue surface (Hyperliquid primary perp or one HIP-3 dex)."""

    venue: str  # "perp" or "hip3:<dex>"

    # ---------- writes ----------
    async def place_order(
        self,
        symbol: str,
        side: Side,
        size: float,
        order_type: OrderType,
        limit_price: float | None = None,
        reduce_only: bool = False,
    ) -> OrderResult: ...

    async def cancel(self, order_id: str) -> bool: ...

    # ---------- state reads ----------
    async def get_positions(self) -> dict[str, Position]: ...
    async def get_open_orders(self) -> list[Order]: ...

    # ---------- market reads — pointwise ----------
    def mid(self, symbol: str) -> float: ...
    def funding(self, symbol: str) -> float: ...
    async def orderbook(self, symbol: str, depth: int = 10) -> OrderBook: ...

    # ---------- market reads — disciplined slippage helpers ----------
    async def quantity_at_price(
        self, symbol: str, side: Side, target_price: float
    ) -> float: ...
    async def price_for_quantity(
        self, symbol: str, side: Side, qty: float
    ) -> float: ...

    # ---------- pre-trade sizing ----------
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
        """Largest size of (symbol, side) that fits `free_margin`, given current
        positions + already-queued pending orders on this venue.

        Backtest: computes against tracked positions/queue (FIFO-faithful — call this
        per order so each one consumes from the budget).

        Live: queries account margin and computes against exchange-reported state.
        """
        ...

    # ---------- market reads — history ----------
    async def recent_prices(
        self, symbols: list[str], lookback_bars: int
    ) -> pd.DataFrame: ...
    async def recent_funding(
        self, symbols: list[str], lookback_bars: int
    ) -> pd.DataFrame: ...

    # ---------- collateral ----------
    async def get_margin_balance(self) -> float: ...
    async def transfer_in(self, amount: float) -> OrderResult: ...
    async def transfer_out(self, amount: float) -> OrderResult: ...

    # ---------- time ----------
    def now(self) -> datetime: ...
