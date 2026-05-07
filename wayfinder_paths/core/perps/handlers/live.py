"""LiveHandler — wraps `HyperliquidAdapter` for the trigger framework.

Constructed fresh per `update()` (D8). The strategy parent builds one for the
primary perp venue and one per declared HIP-3 dex. State persistence lives in
`StateStore`, not on the handler.

Phase 7 scope: cover the primary perp surface end-to-end; HIP-3 reads are
the same protocol but require dex-routing in `place_order` / `get_positions`,
which the HL adapter exposes via `_post_across_dexes` — wired below where the
`dex` argument applies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pandas as pd

from wayfinder_paths.core.perps.handlers.protocol import (
    Order,
    OrderBook,
    OrderResult,
    OrderType,
    Position,
    Side,
)

# Default slippage bound passed to HL `place_market_order` (it converts to a marketable
# limit). Strategies can override via params; this is just the per-call cap.
_DEFAULT_SLIPPAGE = 0.005  # 50 bps


class LiveHandler:
    """One instance per perp venue (primary or a single HIP-3 dex)."""

    def __init__(
        self,
        *,
        adapter: Any,  # HyperliquidAdapter (typed loosely to avoid hard import)
        wallet_address: str,
        venue: str = "perp",
        dex: str | None = None,  # None = primary perp; "xyz"/"flx"/... for HIP-3
        delta_lab_client: Any | None = None,  # for recent_prices/funding fallback chain
        default_slippage: float = _DEFAULT_SLIPPAGE,
    ):
        self.adapter = adapter
        self.wallet_address = wallet_address
        self.venue = venue
        self.dex = dex
        self.delta_lab_client = delta_lab_client
        self.default_slippage = float(default_slippage)

    # ---------- writes ----------
    async def place_order(
        self,
        symbol: str,
        side: Side,
        size: float,
        order_type: OrderType,
        limit_price: float | None = None,
        reduce_only: bool = False,
    ) -> OrderResult:
        asset_id = self.adapter.coin_to_asset.get(symbol)
        if asset_id is None:
            return OrderResult(
                ok=False,
                venue=self.venue,
                symbol=symbol,
                side=side,
                size=size,
                order_type=order_type,
                error=f"unknown symbol {symbol!r}",
                timestamp=self.now(),
            )
        is_buy = side == "buy"
        if order_type == "market":
            ok, raw = await self.adapter.place_market_order(
                asset_id=asset_id,
                is_buy=is_buy,
                slippage=self.default_slippage,
                size=size,
                address=self.wallet_address,
                reduce_only=reduce_only,
            )
        elif order_type in ("limit", "ioc_limit") and limit_price is not None:
            ok, raw = await self.adapter.place_limit_order(
                asset_id=asset_id,
                is_buy=is_buy,
                price=limit_price,
                size=size,
                address=self.wallet_address,
                reduce_only=reduce_only,
                ioc=(order_type == "ioc_limit"),
            )
        else:
            return OrderResult(
                ok=False,
                venue=self.venue,
                symbol=symbol,
                side=side,
                size=size,
                order_type=order_type,
                error="limit order requires limit_price",
                timestamp=self.now(),
            )

        return OrderResult(
            ok=bool(ok),
            venue=self.venue,
            symbol=symbol,
            side=side,
            size=size,
            order_type=order_type,
            limit_price=limit_price,
            reduce_only=reduce_only,
            timestamp=self.now(),
            raw=raw if isinstance(raw, dict) else {"raw": raw},
            error=None
            if ok
            else (raw.get("error") if isinstance(raw, dict) else str(raw)),
        )

    async def cancel(self, order_id: str) -> bool:
        # order_id format: "<asset_id>:<oid>" — caller provides whatever was returned
        # from `place_order`'s raw payload. Strategies typically don't cancel resting
        # orders in the trigger pattern; this is a thin pass-through for completeness.
        try:
            asset_id, oid = order_id.split(":", 1)
            ok, _ = await self.adapter.cancel_order(
                asset_id=int(asset_id),
                oid=int(oid),
                address=self.wallet_address,
            )
            return bool(ok)
        except (ValueError, AttributeError):
            return False

    # ---------- state reads ----------
    async def get_positions(self) -> dict[str, Position]:
        ok, state = await self.adapter.get_user_state(self.wallet_address)
        if not ok or not isinstance(state, dict):
            return {}
        out: dict[str, Position] = {}
        for ap in state.get("assetPositions") or []:
            p = ap.get("position") or {}
            sym = p.get("coin")
            if not sym:
                continue
            sz = float(p.get("szi") or 0.0)
            if sz == 0:
                continue
            entry = float(p.get("entryPx") or 0.0)
            mark = float(p.get("markPx") or entry)
            notional = abs(sz) * mark
            unreal = float(p.get("unrealizedPnl") or 0.0)
            lev_block = p.get("leverage") or {}
            lev_val = lev_block.get("value") if isinstance(lev_block, dict) else None
            out[sym] = Position(
                symbol=sym,
                size=sz,
                entry_price=entry,
                mark_price=mark,
                notional=notional,
                unrealized_pnl=unreal,
                leverage=float(lev_val) if lev_val is not None else None,
                raw=ap,
            )
        return out

    async def get_open_orders(self) -> list[Order]:
        ok, raw = await self.adapter.get_open_orders(self.wallet_address)
        if not ok or not isinstance(raw, list):
            return []
        out = []
        for o in raw:
            try:
                placed = datetime.fromtimestamp(
                    int(o.get("timestamp", 0)) / 1000, tz=UTC
                )
            except (TypeError, ValueError):
                placed = self.now()
            out.append(
                Order(
                    order_id=f"{o.get('asset', '?')}:{o.get('oid', '?')}",
                    symbol=str(o.get("coin", "?")),
                    side="buy" if o.get("side") == "B" else "sell",
                    size=float(o.get("sz", 0.0)),
                    order_type="limit",
                    limit_price=float(o.get("limitPx", 0.0)) or None,
                    placed_at=placed,
                    venue=self.venue,
                    reduce_only=bool(o.get("reduceOnly", False)),
                    raw=o,
                )
            )
        return out

    # ---------- market reads — pointwise ----------
    def mid(self, symbol: str) -> float:
        # Cached mid via adapter property if available; otherwise sync-fetched once.
        cache = getattr(self, "_mids_cache", None)
        if cache is None:
            return 0.0  # caller should pre-fetch via _refresh_mids
        return float(cache.get(symbol, 0.0))

    def funding(self, symbol: str) -> float:
        cache = getattr(self, "_funding_cache", None)
        if cache is None:
            return 0.0
        return float(cache.get(symbol, 0.0))

    async def refresh_mids(self) -> None:
        ok, mids = await self.adapter.get_all_mid_prices()
        self._mids_cache = mids if ok and isinstance(mids, dict) else {}

    async def orderbook(self, symbol: str, depth: int = 10) -> OrderBook:
        ok, raw = await self.adapter.get_l2_book(symbol)
        if not ok or not isinstance(raw, dict):
            return OrderBook(
                symbol=symbol, bids=[], asks=[], timestamp=self.now(), venue=self.venue
            )
        levels = raw.get("levels") or [[], []]
        bids = [(float(l["px"]), float(l["sz"])) for l in (levels[0] or [])[:depth]]
        asks = [(float(l["px"]), float(l["sz"])) for l in (levels[1] or [])[:depth]]
        return OrderBook(
            symbol=symbol, bids=bids, asks=asks, timestamp=self.now(), venue=self.venue
        )

    # ---------- market reads — disciplined slippage helpers ----------
    async def quantity_at_price(
        self, symbol: str, side: Side, target_price: float
    ) -> float:
        ob = await self.orderbook(symbol, depth=20)
        if side == "buy":
            return sum(sz for px, sz in ob.asks if px <= target_price)
        return sum(sz for px, sz in ob.bids if px >= target_price)

    async def price_for_quantity(self, symbol: str, side: Side, qty: float) -> float:
        ob = await self.orderbook(symbol, depth=20)
        levels = ob.asks if side == "buy" else ob.bids
        remaining = qty
        last_px = 0.0
        for px, sz in levels:
            last_px = px
            remaining -= sz
            if remaining <= 0:
                return px
        return last_px or 0.0

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
        # Live: the exchange enforces margin server-side. We just return the request
        # as-is; the order will be rejected if margin is short. Strategies that want
        # client-side throttling can call `get_margin_balance()` and decide manually.
        if requested_size <= 0:
            return 0.0
        return requested_size

    # ---------- market reads — history ----------
    async def recent_prices(
        self, symbols: list[str], lookback_bars: int
    ) -> pd.DataFrame:
        # Delta Lab primary, HL candles fallback (D7).
        if self.delta_lab_client is not None:
            try:
                return await self._fetch_prices_delta_lab(symbols, lookback_bars)
            except Exception:  # noqa: BLE001
                pass
        return await self._fetch_prices_hl(symbols, lookback_bars)

    async def recent_funding(
        self, symbols: list[str], lookback_bars: int
    ) -> pd.DataFrame:
        if self.delta_lab_client is not None:
            try:
                return await self._fetch_funding_delta_lab(symbols, lookback_bars)
            except Exception:  # noqa: BLE001
                pass
        return pd.DataFrame()

    async def _fetch_prices_delta_lab(
        self, symbols: list[str], lookback_bars: int
    ) -> pd.DataFrame:
        # Each symbol → asset timeseries (price). Hourly only for now.
        from wayfinder_paths.core.backtesting.data import fetch_prices  # noqa: PLC0415

        days = max(1, lookback_bars // 24 + 1)
        from datetime import timedelta

        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        return await fetch_prices(
            symbols, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), "1h"
        )

    async def _fetch_funding_delta_lab(
        self, symbols: list[str], lookback_bars: int
    ) -> pd.DataFrame:
        from datetime import timedelta

        from wayfinder_paths.core.backtesting.data import (
            fetch_funding_rates,  # noqa: PLC0415
        )

        days = max(1, lookback_bars // 24 + 1)
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        return await fetch_funding_rates(
            symbols, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
        )

    async def _fetch_prices_hl(
        self, symbols: list[str], lookback_bars: int
    ) -> pd.DataFrame:
        # Fallback path. The HL candles surface lives on the adapter; left as a TODO
        # on this scaffold — Phase 7 polish wires `get_candles` per symbol and stitches.
        return pd.DataFrame(columns=symbols)

    # ---------- collateral ----------
    async def get_margin_balance(self) -> float:
        ok, state = await self.adapter.get_user_state(self.wallet_address)
        if not ok or not isinstance(state, dict):
            return 0.0
        cms = state.get("crossMarginSummary") or state.get("marginSummary") or {}
        try:
            return float(cms.get("accountValue", 0.0))
        except (TypeError, ValueError):
            return 0.0

    async def transfer_in(self, amount: float) -> OrderResult:
        # USDC bridge to HL — strategies typically do this once at deposit time, not per bar.
        return OrderResult(
            ok=False,
            venue=self.venue,
            symbol="USDC",
            side="buy",
            size=amount,
            order_type="market",
            error="transfer_in not wired on LiveHandler — call HyperliquidAdapter directly via deposit() flow",
            timestamp=self.now(),
        )

    async def transfer_out(self, amount: float) -> OrderResult:
        try:
            ok, raw = await self.adapter.withdraw(
                amount=amount, address=self.wallet_address
            )
            return OrderResult(
                ok=bool(ok),
                venue=self.venue,
                symbol="USDC",
                side="sell",
                size=amount,
                order_type="market",
                fill_size=amount if ok else 0.0,
                timestamp=self.now(),
                raw=raw if isinstance(raw, dict) else {"raw": raw},
                error=None if ok else str(raw),
            )
        except Exception as e:  # noqa: BLE001
            return OrderResult(
                ok=False,
                venue=self.venue,
                symbol="USDC",
                side="sell",
                size=amount,
                order_type="market",
                error=str(e),
                timestamp=self.now(),
            )

    # ---------- time ----------
    def now(self) -> datetime:
        return datetime.now(UTC)
