from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    FillEvent,
    OrderIntent,
    PositionRecord,
    TradeCapacity,
)


@dataclass(frozen=True)
class VenueCapabilities:
    """What a venue can express. The engine rejects intents a venue cannot
    honor instead of silently reshaping them, so a strategy validated in
    backtest cannot emit orders live that mean something different."""

    market_kind: str = "perp"  # "perp" | "spot" | "prediction"
    supports_brackets: bool = False  # venue-native trigger orders
    supports_shorts: bool = False
    supports_notional_sizing: bool = True
    supports_limit_orders: bool = False
    position_model: str = "netting"  # "netting" | "outcome_tokens"
    settlement: str = "continuous"  # "continuous" | "resolution"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MarketEvent:
    """Non-bar market occurrences: prediction-market resolutions, funding
    payments, halts. Resolutions become engine-synthesized settle fills so
    outcome-token positions close without any bracket machinery."""

    kind: str  # "resolution" | "funding" | "halt"
    symbol: str
    timestamp: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VenueState:
    """Ground truth pulled from the venue each live tick; the driver reconciles
    the recorded ledger against this before deciding."""

    positions: dict[str, PositionRecord] = field(default_factory=dict)
    open_orders: list[dict[str, Any]] = field(default_factory=list)
    balances: dict[str, float] = field(default_factory=dict)
    source: str = "unknown"
    fetched_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "positions": {
                symbol: record.to_dict() for symbol, record in self.positions.items()
            },
            "open_orders": list(self.open_orders),
            "balances": dict(self.balances),
            "source": self.source,
            "fetched_at": self.fetched_at,
        }


@runtime_checkable
class MarketDataFeed(Protocol):
    async def get_completed_bars(
        self,
        symbols: Sequence[str],
        interval: str,
        *,
        lookback_bars: int,
        as_of: datetime | None = None,
    ) -> CompletedBarsView: ...

    async def get_events(
        self, symbols: Sequence[str], *, since: datetime | None = None
    ) -> list[MarketEvent]: ...


@runtime_checkable
class Broker(Protocol):
    capabilities: VenueCapabilities

    async def fetch_state(self, symbols: Sequence[str]) -> VenueState: ...

    async def get_capacity(self, symbol: str, side: str) -> TradeCapacity: ...

    async def place(
        self,
        intent: OrderIntent,
        *,
        timestamp: str,
        price: float | None = None,
    ) -> FillEvent: ...

    async def cancel(self, client_order_id: str) -> FillEvent: ...


@runtime_checkable
class VenueAdapter(Protocol):
    name: str
    capabilities: VenueCapabilities
    feed: MarketDataFeed
    broker: Broker


VENUE_REGISTRY: dict[str, Callable[..., VenueAdapter]] = {}


def register_venue(name: str, factory: Callable[..., VenueAdapter]) -> None:
    VENUE_REGISTRY[name] = factory


def build_adapter(
    venue: str,
    *,
    mode: str,
    spec: Any = None,
    params: Mapping[str, Any] | None = None,
) -> VenueAdapter:
    factory = VENUE_REGISTRY.get(venue)
    if factory is None:
        raise ValueError(
            f"unknown venue {venue!r}; registered: {sorted(VENUE_REGISTRY)}"
        )
    return factory(mode=mode, spec=spec, params=dict(params or {}))
