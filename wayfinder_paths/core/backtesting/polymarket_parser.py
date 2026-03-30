from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass
class TradeSignal:
    woi_address: str
    condition_id: str
    token_id: str  # numeric string — CLOB asset ID ("asset" field)
    outcome: str  # "Yes" / "No" / etc.
    side: Literal["BUY", "SELL"]
    usdc_amount: float  # USDC spent (BUY) or received (SELL)
    share_count: float
    avg_price: float  # USDC per share
    market_slug: str  # human-readable, for logging
    dedupe_key: str  # transactionHash


def parse_activity(woi_address: str, record: dict[str, Any]) -> TradeSignal | None:
    """Convert a raw Data API activity record into a TradeSignal.

    Returns None if the record is not a copyable trade
    (wrong type, missing fields, or zero size).
    """
    if record.get("type") != "TRADE":
        return None

    side = record.get("side")
    if side not in ("BUY", "SELL"):
        return None

    condition_id = record.get("conditionId", "")
    token_id = record.get("asset", "")
    if not condition_id or not token_id:
        return None

    usdc_amount = float(record.get("usdcSize") or 0)
    share_count = float(record.get("size") or 0)
    if usdc_amount <= 0 or share_count <= 0:
        return None

    avg_price = float(record.get("price") or 0)
    outcome = record.get("outcome") or str(record.get("outcomeIndex", ""))
    dedupe_key = (
        record.get("transactionHash") or f"{condition_id}:{record.get('timestamp')}"
    )
    slug = record.get("slug") or condition_id[:12]

    return TradeSignal(
        woi_address=woi_address,
        condition_id=condition_id,
        token_id=token_id,
        outcome=outcome,
        side=side,
        usdc_amount=usdc_amount,
        share_count=share_count,
        avg_price=avg_price,
        market_slug=slug,
        dedupe_key=dedupe_key,
    )
