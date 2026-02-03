"""Types for BorosAdapter (dataclasses)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class BorosMarketQuote:
    """Quote data for a Boros market."""

    market_id: int
    market_address: str
    symbol: str
    underlying: str
    tenor_days: float
    maturity_ts: int
    collateral_address: str
    collateral_token_id: int | None
    tick_step: int | None
    mid_apr: float | None
    best_bid_apr: float | None
    best_ask_apr: float | None
    # Optional market-data fields (when present on the /markets endpoint).
    mark_apr: float | None = None
    floating_apr: float | None = None
    long_yield_apr: float | None = None
    funding_7d_ma_apr: float | None = None
    funding_30d_ma_apr: float | None = None
    volume_24h: float | None = None
    notional_oi: float | None = None
    asset_mark_price: float | None = None
    next_settlement_time: int | None = None
    last_traded_apr: float | None = None
    amm_implied_apr: float | None = None


@dataclass
class BorosTenorQuote:
    """Tenor curve data for a Boros market."""

    market_id: int
    address: str
    symbol: str
    underlying_symbol: str
    maturity: int
    tenor_days: float
    mid_apr: float | None
    mark_apr: float | None
    floating_apr: float | None
    long_yield_apr: float | None
    volume_24h: float | None
    notional_oi: float | None


@dataclass
class BorosLimitOrder:
    """Represents an open limit order on Boros."""

    order_id: str
    market_id: int
    side: str  # "long" or "short"
    size: float  # Size in YU
    limit_tick: int  # APR in bps
    limit_apr: float  # APR as decimal (e.g., 0.05 = 5%)
    filled_size: float
    remaining_size: float
    status: str  # "open", "partially_filled", etc.
    created_at: datetime | None = None
    raw: dict[str, Any] | None = field(default=None, repr=False)


@dataclass
class MarginHealth:
    """Margin health metrics for a Boros account."""

    margin_ratio: float
    maint_margin: float
    net_balance: float
    positions: list[dict[str, Any]]
