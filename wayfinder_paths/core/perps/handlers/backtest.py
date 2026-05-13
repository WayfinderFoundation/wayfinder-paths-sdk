"""BacktestHandler: numpy-backed market handler for `backtest_perps_trigger`.

Fills queue to next-bar open (D6). Idealized depth — `quantity_at_price` /
`price_for_quantity` assume infinite depth at mid (honest fact noted in the
backtest skill prompt; recon catches deviation).
"""

from __future__ import annotations

import contextlib
import random
import time
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

from wayfinder_paths.core.perps.handlers.protocol import (
    Order,
    OrderBook,
    OrderResult,
    OrderType,
    Position,
    Side,
)


class PurityViolation(RuntimeError):
    """Raised when decide() touches wall-clock or randomness during a backtest."""


def _violation(name: str):
    def _raise(*args: Any, **kwargs: Any):
        raise PurityViolation(
            f"decide() called {name} during backtest — signal/decide must be deterministic. "
            "Use ctx.t for time and seed your own RNG via params."
        )

    return _raise


@contextlib.contextmanager
def purity_sandbox():
    """Catch the obvious wall-clock / RNG calls inside decide().

    Cannot patch `datetime.datetime.now` (immutable C type). The skill prompt
    instructs strategy authors to use `ctx.t` for time; this sandbox covers the
    `time` module and `random.random` which are the more common foot-guns.
    """
    real_time = time.time
    real_mono = time.monotonic
    real_rand = random.random
    time.time = _violation("time.time")  # type: ignore[assignment]
    time.monotonic = _violation("time.monotonic")  # type: ignore[assignment]
    random.random = _violation("random.random")  # type: ignore[assignment]
    try:
        yield
    finally:
        time.time = real_time  # type: ignore[assignment]
        time.monotonic = real_mono  # type: ignore[assignment]
        random.random = real_rand  # type: ignore[assignment]


class BacktestHandler:
    """Handler for one venue (`perp` or `hip3:<dex>`).

    The driver advances `bar_index` and calls `apply_pending_fills` at each new bar
    before invoking decide() again.
    """

    def __init__(
        self,
        venue: str,
        prices: pd.DataFrame,
        funding: pd.DataFrame | None,
        slippage_bps: float = 1.0,
        fee_bps: float = 4.5,
        min_order_usd: float = 10.0,
        sz_decimals: dict[str, int] | None = None,
    ):
        if not isinstance(prices.index, pd.DatetimeIndex):
            raise TypeError("prices must have a DatetimeIndex")
        self.venue = venue
        self._index = prices.index
        self._symbols = list(prices.columns)
        self._sym_to_col = {s: i for i, s in enumerate(self._symbols)}
        self._prices_arr: np.ndarray = prices.to_numpy(dtype=float, copy=False)
        if funding is not None:
            funding = (
                funding.reindex(index=self._index, columns=self._symbols)
                .ffill()
                .fillna(0.0)
            )
            self._funding_arr: np.ndarray | None = funding.to_numpy(
                dtype=float, copy=False
            )
        else:
            self._funding_arr = None

        self.slippage_bps = float(slippage_bps)
        self.fee_bps = float(fee_bps)
        self.min_order_usd = float(min_order_usd)
        # When provided, place_order rounds size DOWN to 10^-sz_decimals[sym]
        # so backtest mirrors HL's szDecimals truncation. Without this, backtest
        # over-sizes by ≤0.5 step per trade vs live (asymmetric, always favors
        # backtest). Keys missing from the dict → no rounding for that symbol.
        self.sz_decimals: dict[str, int] = dict(sz_decimals or {})

        # Position state — signed sizes in base units.
        self._positions: dict[str, float] = dict.fromkeys(self._symbols, 0.0)
        self._entry_price: dict[str, float] = dict.fromkeys(self._symbols, 0.0)

        # Pending = orders placed during decide() at bar i, fill at bar i+1.
        self._pending: list[dict[str, Any]] = []
        # Intents log (for reporting / recon parity).
        self._intents: list[dict[str, Any]] = []
        # Per-bar realized cashflows from fills at THIS bar.
        self._bar_fees: float = 0.0
        self._bar_funding: float = 0.0
        self._bar_realized_pnl: float = 0.0
        self._bar_index: int = 0

    # -------- driver hooks --------
    def set_bar(self, i: int) -> None:
        self._bar_index = i
        self._bar_fees = 0.0
        self._bar_funding = 0.0
        self._bar_realized_pnl = 0.0

    def apply_pending_fills(self) -> list[OrderResult]:
        """Fill all queued orders at the current bar's price (treated as the bar open)."""
        results: list[OrderResult] = []
        if not self._pending:
            return results
        i = self._bar_index
        for order in self._pending:
            sym = order["symbol"]
            side: Side = order["side"]
            size = order["size"]
            order_type: OrderType = order["order_type"]
            limit = order.get("limit_price")
            reduce_only = order.get("reduce_only", False)
            mid = float(self._prices_arr[i, self._sym_to_col[sym]])
            slip = self.slippage_bps / 1e4
            fill_price = mid * (1 + slip) if side == "buy" else mid * (1 - slip)

            if order_type in ("limit", "ioc_limit") and limit is not None:
                if side == "buy" and fill_price > limit:
                    results.append(
                        OrderResult(
                            ok=False,
                            venue=self.venue,
                            symbol=sym,
                            side=side,
                            size=size,
                            order_type=order_type,
                            limit_price=limit,
                            fill_price=None,
                            fill_size=0.0,
                            error="limit not crossed",
                            timestamp=self._index[i].to_pydatetime(),
                        )
                    )
                    continue
                if side == "sell" and fill_price < limit:
                    results.append(
                        OrderResult(
                            ok=False,
                            venue=self.venue,
                            symbol=sym,
                            side=side,
                            size=size,
                            order_type=order_type,
                            limit_price=limit,
                            fill_price=None,
                            fill_size=0.0,
                            error="limit not crossed",
                            timestamp=self._index[i].to_pydatetime(),
                        )
                    )
                    continue

            signed = size if side == "buy" else -size
            if reduce_only:
                cur = self._positions[sym]
                if cur * signed >= 0:
                    # Reduce-only and not reducing → reject.
                    results.append(
                        OrderResult(
                            ok=False,
                            venue=self.venue,
                            symbol=sym,
                            side=side,
                            size=size,
                            order_type=order_type,
                            limit_price=limit,
                            fill_price=None,
                            fill_size=0.0,
                            error="reduce-only would not reduce",
                            timestamp=self._index[i].to_pydatetime(),
                            reduce_only=True,
                        )
                    )
                    continue
                if abs(signed) > abs(cur):
                    signed = -cur

            notional = abs(signed) * fill_price
            fee = notional * (self.fee_bps / 1e4)
            self._bar_fees += fee

            # Realized PnL on the closed portion.
            cur = self._positions[sym]
            new = cur + signed
            entry = self._entry_price[sym]
            if cur * new < 0:  # crossing through zero
                closed = cur
                realized = closed * (fill_price - entry)
                # Reset entry for the leftover opening side.
                self._entry_price[sym] = fill_price
                self._bar_realized_pnl += realized
            elif abs(new) < abs(cur):  # partial close
                closed = -signed
                realized = closed * (fill_price - entry)
                self._bar_realized_pnl += realized
                # entry unchanged on partial close
            elif cur == 0:  # opening from flat
                self._entry_price[sym] = fill_price
            else:  # adding to existing
                self._entry_price[sym] = (
                    (entry * cur + fill_price * signed) / new if new != 0 else 0.0
                )

            self._positions[sym] = new

            results.append(
                OrderResult(
                    ok=True,
                    venue=self.venue,
                    symbol=sym,
                    side=side,
                    size=size,
                    order_type=order_type,
                    limit_price=limit,
                    fill_price=fill_price,
                    fill_size=size,
                    fee_paid=fee,
                    reduce_only=reduce_only,
                    timestamp=self._index[i].to_pydatetime(),
                    order_id=order["id"],
                )
            )

        self._pending.clear()
        return results

    def accrue_funding(self) -> float:
        """Apply funding for the bar and return the venue-level funding cashflow.

        Convention: positive funding => longs pay shorts.
        cashflow = -position * mid * funding_rate.  Negative cashflow = paid out.
        """
        if self._funding_arr is None:
            return 0.0
        i = self._bar_index
        total = 0.0
        for sym, sz in self._positions.items():
            if sz == 0:
                continue
            r = float(self._funding_arr[i, self._sym_to_col[sym]])
            mid = float(self._prices_arr[i, self._sym_to_col[sym]])
            cash = -sz * mid * r
            total += cash
        self._bar_funding += total
        return total

    def mark_to_market_value(self) -> float:
        """Sum of unrealized + (size * mid) — used for portfolio NAV."""
        i = self._bar_index
        total = 0.0
        for sym, sz in self._positions.items():
            if sz == 0:
                continue
            mid = float(self._prices_arr[i, self._sym_to_col[sym]])
            entry = self._entry_price[sym]
            total += sz * (mid - entry)
        return total

    def gross_notional(self) -> float:
        i = self._bar_index
        return sum(
            abs(sz) * float(self._prices_arr[i, self._sym_to_col[s]])
            for s, sz in self._positions.items()
            if sz != 0
        )

    def consume_bar_costs(self) -> tuple[float, float, float]:
        """Return (fees, funding, realized_pnl) for the bar and reset accumulators."""
        out = (self._bar_fees, self._bar_funding, self._bar_realized_pnl)
        self._bar_fees = 0.0
        self._bar_funding = 0.0
        self._bar_realized_pnl = 0.0
        return out

    def pending_orders_view(self) -> list[dict[str, Any]]:
        """Return a snapshot of pending orders annotated with current size + mid.

        Used by the driver's atomic-trade-scale step to compute margin needs
        across all venues before fills are applied.
        """
        i = self._bar_index
        out = []
        for o in self._pending:
            sym = o["symbol"]
            mid = float(self._prices_arr[i, self._sym_to_col[sym]])
            cur = self._positions[sym]
            signed = o["size"] if o["side"] == "buy" else -o["size"]
            out.append(
                {
                    "venue": self.venue,
                    "id": o["id"],
                    "sym": sym,
                    "side": o["side"],
                    "size": o["size"],
                    "mid": mid,
                    "current_size": cur,
                    "signed_delta": signed,
                    "new_size": cur + signed,
                }
            )
        return out

    def scale_pending(self, scale: float) -> int:
        """Multiply each pending order's size by `scale`. Drop sub-min-order intents.

        Returns the number of dropped orders.
        """
        if scale >= 1.0:
            return 0
        kept: list[dict[str, Any]] = []
        dropped = 0
        i = self._bar_index
        for o in self._pending:
            new_size = o["size"] * scale
            mid = float(self._prices_arr[i, self._sym_to_col[o["symbol"]]])
            if new_size <= 0 or new_size * mid < self.min_order_usd:
                dropped += 1
                continue
            o["size"] = new_size
            kept.append(o)
        self._pending = kept
        return dropped

    def drain_intents(self) -> list[dict[str, Any]]:
        out, self._intents = self._intents, []
        return out

    # -------- protocol surface --------
    async def place_order(
        self,
        symbol: str,
        side: Side,
        size: float,
        order_type: OrderType,
        limit_price: float | None = None,
        reduce_only: bool = False,
    ) -> OrderResult:
        if symbol not in self._sym_to_col:
            return OrderResult(
                ok=False,
                venue=self.venue,
                symbol=symbol,
                side=side,
                size=size,
                order_type=order_type,
                error=f"unknown symbol on venue {self.venue}",
            )
        mid = float(self._prices_arr[self._bar_index, self._sym_to_col[symbol]])
        decimals = self.sz_decimals.get(symbol)
        if decimals is not None:
            # ROUND_DOWN to step = 10^-decimals — mirrors live HL's
            # `round_size_for_asset` semantics so backtest never trades a finer
            # step than the exchange allows.
            step = 10.0 ** (-int(decimals))
            size = float(int(abs(size) / step)) * step
            if size <= 0:
                return OrderResult(
                    ok=False,
                    venue=self.venue,
                    symbol=symbol,
                    side=side,
                    size=0.0,
                    order_type=order_type,
                    limit_price=limit_price,
                    error=f"below szDecimals step ({step})",
                )
        notional = abs(size) * mid
        if notional < self.min_order_usd:
            return OrderResult(
                ok=False,
                venue=self.venue,
                symbol=symbol,
                side=side,
                size=size,
                order_type=order_type,
                limit_price=limit_price,
                error=f"below min_order_usd ({notional:.2f} < {self.min_order_usd})",
            )
        oid = f"bt-{self.venue}-{self._bar_index}-{len(self._intents)}"
        intent = {
            "id": oid,
            "symbol": symbol,
            "side": side,
            "size": size,
            "order_type": order_type,
            "limit_price": limit_price,
            "reduce_only": reduce_only,
            "placed_at_bar": self._bar_index,
            "placed_at_t": self._index[self._bar_index],
        }
        self._intents.append(intent)
        self._pending.append(intent)
        return OrderResult(
            ok=True,
            venue=self.venue,
            symbol=symbol,
            side=side,
            size=size,
            order_type=order_type,
            limit_price=limit_price,
            reduce_only=reduce_only,
            order_id=oid,
            fill_size=0.0,  # fills at next bar
            timestamp=self._index[self._bar_index].to_pydatetime(),
        )

    async def cancel(self, order_id: str) -> bool:
        before = len(self._pending)
        self._pending = [o for o in self._pending if o["id"] != order_id]
        return len(self._pending) < before

    async def get_positions(self) -> dict[str, Position]:
        i = self._bar_index
        out: dict[str, Position] = {}
        for sym, sz in self._positions.items():
            if sz == 0:
                continue
            mid = float(self._prices_arr[i, self._sym_to_col[sym]])
            entry = self._entry_price[sym]
            out[sym] = Position(
                symbol=sym,
                size=sz,
                entry_price=entry,
                mark_price=mid,
                notional=abs(sz) * mid,
                unrealized_pnl=sz * (mid - entry),
            )
        return out

    async def get_open_orders(self) -> list[Order]:
        out = []
        for o in self._pending:
            out.append(
                Order(
                    order_id=o["id"],
                    symbol=o["symbol"],
                    side=o["side"],
                    size=o["size"],
                    order_type=o["order_type"],
                    limit_price=o["limit_price"],
                    placed_at=o["placed_at_t"].to_pydatetime(),
                    venue=self.venue,
                    reduce_only=o["reduce_only"],
                )
            )
        return out

    def mid(self, symbol: str) -> float:
        return float(self._prices_arr[self._bar_index, self._sym_to_col[symbol]])

    def funding(self, symbol: str) -> float:
        if self._funding_arr is None:
            return 0.0
        return float(self._funding_arr[self._bar_index, self._sym_to_col[symbol]])

    async def orderbook(self, symbol: str, depth: int = 10) -> OrderBook:
        m = self.mid(symbol)
        slip = self.slippage_bps / 1e4
        # idealized — single level at mid ± slippage with effectively infinite depth
        return OrderBook(
            symbol=symbol,
            bids=[(m * (1 - slip), 1e9)],
            asks=[(m * (1 + slip), 1e9)],
            timestamp=self._index[self._bar_index].to_pydatetime(),
            venue=self.venue,
        )

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
        if requested_size <= 0 or free_margin <= 0 or symbol not in self._sym_to_col:
            return 0.0
        mid = self.mid(symbol)
        if mid <= 0:
            return 0.0
        # Sequential FIFO budget: incorporate already-queued pending orders for this symbol.
        cur = self._positions.get(symbol, 0.0)
        for o in self._pending:
            if o["symbol"] == symbol:
                cur += o["size"] if o["side"] == "buy" else -o["size"]
        signed_dir = 1.0 if side == "buy" else -1.0
        cost_rate = float(cost_bps) / 1e4
        lev = float(leverage) if leverage > 0 else 1.0

        def margin_for(sz: float) -> float:
            new = cur + signed_dir * sz
            gross_inc = max(0.0, abs(new) - abs(cur)) * mid
            fee = sz * mid * cost_rate
            return gross_inc / lev + fee

        full = margin_for(requested_size)
        if full <= free_margin:
            return requested_size
        lo, hi = 0.0, requested_size
        for _ in range(40):
            mid_size = 0.5 * (lo + hi)
            if margin_for(mid_size) <= free_margin:
                lo = mid_size
            else:
                hi = mid_size
        return lo

    async def quantity_at_price(
        self, symbol: str, side: Side, target_price: float
    ) -> float:
        m = self.mid(symbol)
        slip = self.slippage_bps / 1e4
        edge = m * (1 + slip) if side == "buy" else m * (1 - slip)
        if (side == "buy" and target_price >= edge) or (
            side == "sell" and target_price <= edge
        ):
            return 1e9  # idealized
        return 0.0

    async def price_for_quantity(self, symbol: str, side: Side, qty: float) -> float:
        m = self.mid(symbol)
        slip = self.slippage_bps / 1e4
        return m * (1 + slip) if side == "buy" else m * (1 - slip)

    async def recent_prices(
        self, symbols: list[str], lookback_bars: int
    ) -> pd.DataFrame:
        i = self._bar_index
        lo = max(0, i - lookback_bars + 1)
        cols = [self._sym_to_col[s] for s in symbols]
        return pd.DataFrame(
            self._prices_arr[lo : i + 1, cols],
            index=self._index[lo : i + 1],
            columns=symbols,
        )

    async def recent_funding(
        self, symbols: list[str], lookback_bars: int
    ) -> pd.DataFrame:
        if self._funding_arr is None:
            return pd.DataFrame(columns=symbols)
        i = self._bar_index
        lo = max(0, i - lookback_bars + 1)
        cols = [self._sym_to_col[s] for s in symbols]
        return pd.DataFrame(
            self._funding_arr[lo : i + 1, cols],
            index=self._index[lo : i + 1],
            columns=symbols,
        )

    async def get_margin_balance(self) -> float:
        # Driver tracks portfolio NAV; handler-level balance is informational only.
        return 0.0

    async def transfer_in(self, amount: float) -> OrderResult:
        return OrderResult(
            ok=True,
            venue=self.venue,
            symbol="USDC",
            side="buy",
            size=amount,
            order_type="market",
            fill_size=amount,
            timestamp=self._index[self._bar_index].to_pydatetime(),
            raw={"action": "transfer_in"},
        )

    async def transfer_out(self, amount: float) -> OrderResult:
        return OrderResult(
            ok=True,
            venue=self.venue,
            symbol="USDC",
            side="sell",
            size=amount,
            order_type="market",
            fill_size=amount,
            timestamp=self._index[self._bar_index].to_pydatetime(),
            raw={"action": "transfer_out"},
        )

    def now(self) -> datetime:
        ts = self._index[self._bar_index]
        py = ts.to_pydatetime()
        return py if py.tzinfo else py.replace(tzinfo=UTC)

    @property
    def index(self) -> pd.DatetimeIndex:
        return self._index

    @property
    def symbols(self) -> list[str]:
        return list(self._symbols)
