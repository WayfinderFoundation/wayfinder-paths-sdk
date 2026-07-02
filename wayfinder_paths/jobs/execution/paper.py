from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from wayfinder_paths.jobs.execution.primitives import (
    FillEvent,
    OrderIntent,
    TradeCapacity,
)
from wayfinder_paths.jobs.execution.simulator import BacktestBroker
from wayfinder_paths.jobs.execution.venues import VenueCapabilities, VenueState


class PaperBroker:
    """Broker protocol over the backtest fill math, driven by live data.

    Intents queue in EngineState.pending_intents and fill at the next tick at
    the newly completed bar's open — identical to the backtest next_bar_open
    model, so a paper run is a true forward-run of the backtest on live data.
    Positions live purely in the engine ledger; fetch_state never diverges.
    """

    def __init__(
        self,
        *,
        capabilities: VenueCapabilities | None = None,
        fee_bps: float = 0.0,
        slippage_bps: float = 0.0,
    ) -> None:
        self.capabilities = capabilities or BacktestBroker.capabilities
        self._broker = BacktestBroker(fee_bps=fee_bps, slippage_bps=slippage_bps)

    async def place(
        self,
        intent: OrderIntent,
        *,
        timestamp: str,
        price: float | None = None,
    ) -> FillEvent:
        return self._broker.execute(
            intent, price=float(price or 0.0), timestamp=timestamp
        )

    async def fetch_state(self, symbols: Sequence[str] | Any = ()) -> VenueState:
        return VenueState(source="paper")

    async def get_capacity(self, symbol: str, side: str) -> TradeCapacity:
        return TradeCapacity(safe=True, source="paper")

    async def cancel(self, client_order_id: str) -> FillEvent:
        return FillEvent(
            status="rejected",
            venue="paper",
            symbol="",
            side="",
            error="cancel is not supported in paper mode",
            client_order_id=client_order_id,
        )
