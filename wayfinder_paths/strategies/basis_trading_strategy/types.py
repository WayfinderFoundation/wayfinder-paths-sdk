from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class BasisCandidate:
    coin: str
    spot_pair: str
    spot_asset_id: int
    perp_asset_id: int
    mark_price: float
    target_leverage: int
    ctx: dict[str, Any]
    spot_book: dict[str, Any]
    open_interest_base: float
    open_interest_usd: float
    day_notional_usd: float
    order_usd: float
    depth_checks: dict[str, dict[str, Any]]
    margin_table_id: int | None = None


@dataclass
class BasisPosition:
    coin: str
    spot_asset_id: int
    perp_asset_id: int
    spot_amount: float
    perp_amount: float
    entry_price: float
    leverage: int
    entry_timestamp: int
    funding_collected: float = 0.0
