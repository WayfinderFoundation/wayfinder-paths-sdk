"""ReconcileHandler — replays decide() against historical state snapshots.

Records every order as an *intent* (no fills, no PnL, no position mutation). Reads
positions from `StateStore.snapshot_at(strategy, t)` so decide sees the same state
the live runtime saw at that bar. Market reads (mid/funding/orderbook) reuse the
backtest implementation against historical frames.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from wayfinder_paths.core.perps.handlers.backtest import BacktestHandler
from wayfinder_paths.core.perps.handlers.protocol import (
    Order,
    OrderResult,
    Position,
    Side,
)
from wayfinder_paths.core.perps.state import StateStore


class ReconcileHandler(BacktestHandler):
    """Subclass of BacktestHandler that ignores orders (records intents only)
    and pulls positions from state snapshots instead of an internal ledger.

    A single ReconcileHandler is constructed per venue. The reconciler driver
    advances `set_bar(i)` and calls `set_snapshot(snap)` from
    `StateStore.snapshot_at(strategy, t)` before invoking decide().
    """

    def __init__(
        self,
        *,
        venue: str,
        prices: pd.DataFrame,
        funding: pd.DataFrame | None,
        strategy_name: str,
        slippage_bps: float = 1.0,
        fee_bps: float = 4.5,
        min_order_usd: float = 10.0,
    ):
        super().__init__(
            venue=venue,
            prices=prices,
            funding=funding,
            slippage_bps=slippage_bps,
            fee_bps=fee_bps,
            min_order_usd=min_order_usd,
        )
        self.strategy_name = strategy_name
        self._snapshot_positions: dict[str, float] = {}
        self._snapshot_entry: dict[str, float] = {}
        self._snapshot_mids: dict[str, float] = {}
        self._snapshot_intents: list[dict[str, Any]] = []

    def _bar_interval(self) -> timedelta:
        """Infer one bar interval from the price index. Defaults to 1h if the
        index is too short to compute a delta — that's the canonical cadence
        for Hyperliquid hourly bars used by the reconciler."""
        if len(self._index) < 2:
            return timedelta(hours=1)
        delta = self._index[1] - self._index[0]
        return delta.to_pytimedelta() if hasattr(delta, "to_pytimedelta") else delta

    def load_snapshot_at(self, t: datetime) -> dict[str, Any]:
        """Pull the live-state snapshot for bar `t` and project venue positions,
        mids, and live intents out of it.

        Snapshots are written at trigger time (e.g. T00:08:44Z), not bar-aligned
        (T00:00:00Z). A snapshot belongs to the bar that contains its timestamp,
        so we look up the latest snapshot with ts in `[t, t + bar_interval)`.

        The live runtime stores under `state` (see `ActivePerpsStrategy._run_trigger`):
          - `positions[venue][sym] = {size, entry_price, mark_price}`
          - `orders[venue]         = [intent dicts captured by RecordingHandler]`
          - `mids[venue][sym]      = float`
        """
        snaps = StateStore.snapshots_in_bar(
            self.strategy_name, t, self._bar_interval()
        )
        if not snaps:
            self._snapshot_positions = {}
            self._snapshot_entry = {}
            self._snapshot_mids = {}
            self._snapshot_intents = []
            return {}

        # State (positions/mids/nav) reflects the latest trigger in the bar.
        # Intents are unioned across every trigger so multi-action bars don't
        # silently drop earlier orders (e.g. entry + follow-up reduce in same bar).
        latest = snaps[-1]
        venue_positions = (latest.get("positions") or {}).get(self.venue) or {}
        self._snapshot_positions = {
            sym: float(p.get("size", 0.0)) for sym, p in venue_positions.items()
        }
        self._snapshot_entry = {
            sym: float(p.get("entry_price", 0.0)) for sym, p in venue_positions.items()
        }
        self._snapshot_mids = {
            sym: float(v)
            for sym, v in ((latest.get("mids") or {}).get(self.venue) or {}).items()
        }
        unioned: list[dict[str, Any]] = []
        for s in snaps:
            unioned.extend((s.get("orders") or {}).get(self.venue) or [])
        self._snapshot_intents = unioned
        return latest

    @property
    def recorded_live_intents(self) -> list[dict[str, Any]]:
        """The intents live actually placed at this bar (from the snapshot)."""
        return list(self._snapshot_intents)

    # ---------- protocol surface — overrides ----------
    async def place_order(
        self,
        symbol: str,
        side: Side,
        size: float,
        order_type: Any,
        limit_price: float | None = None,
        reduce_only: bool = False,
    ) -> OrderResult:
        """Record the intent without mutating positions or queueing fills."""
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
        oid = f"recon-{self.venue}-{self._bar_index}-{len(self._intents)}"
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
        # Note: NOT added to self._pending — recon never simulates fills.
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
            fill_size=0.0,
            timestamp=self._index[self._bar_index].to_pydatetime(),
        )

    async def get_positions(self) -> dict[str, Position]:
        i = self._bar_index
        out: dict[str, Position] = {}
        for sym, sz in self._snapshot_positions.items():
            if sz == 0 or sym not in self._sym_to_col:
                continue
            mid = float(self._prices_arr[i, self._sym_to_col[sym]])
            entry = self._snapshot_entry.get(sym, mid)
            out[sym] = Position(
                symbol=sym,
                size=sz,
                entry_price=entry,
                mark_price=mid,
                notional=abs(sz) * mid,
                unrealized_pnl=sz * (mid - entry),
            )
        return out

    def mid(self, symbol: str) -> float:
        # Prefer snapshotted mid (what live decide() saw) for deterministic replay.
        # Fall back to the historical bar price if no snapshot mid is recorded.
        if symbol in self._snapshot_mids:
            return self._snapshot_mids[symbol]
        return super().mid(symbol)

    async def get_open_orders(self) -> list[Order]:
        return []  # recon doesn't track resting orders

    def now(self) -> datetime:
        ts = self._index[self._bar_index]
        py = ts.to_pydatetime()
        return py if py.tzinfo else py.replace(tzinfo=UTC)
