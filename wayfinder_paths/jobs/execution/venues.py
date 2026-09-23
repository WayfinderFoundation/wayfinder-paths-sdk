from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    FillEvent,
    OrderIntent,
    PositionRecord,
    RestingOrder,
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


@dataclass
class NativeProtectionResult:
    """Outcome of installing or canceling a venue-native reduce-only stop."""

    status: Literal["confirmed", "rejected", "ambiguous"]
    symbol: str
    client_order_id: str
    order_id: str | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def confirmed(self) -> bool:
        return self.status == "confirmed"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FundingSnapshot:
    """A perp's funding as the venue last settled it: `rate` is the latest
    hourly rate as a decimal (0.0001 = 0.01% per hour, positive = longs pay),
    `history` the settled (time_ms, rate) pairs in the lookback, oldest first."""

    symbol: str
    rate: float
    time_ms: int
    history: tuple[tuple[int, float], ...] = ()


@runtime_checkable
class FundingFeed(Protocol):
    """Optional feed extension for venues that settle funding (perps)."""

    async def get_funding(
        self, symbol: str, *, lookback_hours: int = 24
    ) -> FundingSnapshot: ...


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
class NativeProtectionBroker(Protocol):
    """Optional live-broker extension used only for native stop policies."""

    async def place_stop_loss(
        self,
        *,
        symbol: str,
        side: str,
        size: float,
        trigger_price: float,
        client_order_id: str,
    ) -> NativeProtectionResult: ...

    async def cancel_stop_loss(
        self, *, symbol: str, client_order_id: str
    ) -> NativeProtectionResult: ...


NativeStopPolicy = Literal["opted_out", "unsupported"]


def native_stop_policy(
    params: Mapping[str, Any], supports_native: bool
) -> NativeStopPolicy | None:
    """The job-level answer to "does a live entry get a venue-side stop?":
    None when it does (the default wherever the venue can place one, or when
    ``execution_params.native_stop_required`` pins it), ``"opted_out"`` when
    the job sets it to False, ``"unsupported"`` when the venue cannot place
    one and nothing pins it. The engine and the risk flags both ask this."""
    pinned = params.get("native_stop_required")
    if pinned is not None and not pinned:
        return "opted_out"
    if pinned is None and not supports_native:
        return "unsupported"
    return None


@runtime_checkable
class RestingOrderCancelBroker(Protocol):
    """Optional live-broker extension with the context needed to cancel.

    The base Broker API only carries a client order id. Some venues also need
    the asset or exchange order id, both of which live in RestingOrder.
    """

    async def cancel_resting_order(self, order: RestingOrder) -> FillEvent: ...


@runtime_checkable
class VenueAdapter(Protocol):
    name: str
    capabilities: VenueCapabilities
    feed: MarketDataFeed
    broker: Broker


@runtime_checkable
class HistoryProvenanceFeed(Protocol):
    """A feed that can say how far back its source's history goes, per symbol
    (`earliest_available`, `requested_start` as ISO stamps or None). The
    dataset fetch records it so the evidence gate can tell a young token
    from a short fetch."""

    def history_provenance(self) -> dict[str, dict[str, Any]]: ...


VENUE_REGISTRY: dict[str, Callable[..., VenueAdapter]] = {}
VENUE_CAPABILITIES: dict[str, VenueCapabilities] = {}
# Costs a venue charges when a job pins none. Backtest, paper and preflight
# all read these, so an unpriced venue never trades for free anywhere.
DEFAULT_TAKER_FEE_BPS: dict[str, float] = {
    "hyperliquid": 4.5,
    "hl": 4.5,
    "onchain": 30.0,
    "hyperliquid_spot": 7.0,
}
DEFAULT_MAKER_FEE_BPS: dict[str, float] = {"hyperliquid": 1.5, "hl": 1.5}


def register_venue(
    name: str,
    factory: Callable[..., VenueAdapter],
    *,
    capabilities: VenueCapabilities | None = None,
) -> None:
    VENUE_REGISTRY[name] = factory
    if capabilities is not None:
        VENUE_CAPABILITIES[name] = capabilities


def venue_capabilities(name: str) -> VenueCapabilities:
    """The registered venue's contract, for engines that must reject what it
    cannot honor without building an adapter."""
    capabilities = VENUE_CAPABILITIES.get(name)
    if capabilities is None:
        raise ValueError(
            f"unknown venue {name!r}; registered: {sorted(VENUE_CAPABILITIES)}"
        )
    return capabilities


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
