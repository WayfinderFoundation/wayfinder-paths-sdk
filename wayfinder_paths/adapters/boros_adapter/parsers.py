"""Parsing helpers for BorosAdapter response payloads."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any


def extract_symbol(market: dict[str, Any]) -> str:
    im = market.get("imData") or {}
    return im.get("symbol") or market.get("symbol") or market.get("name") or ""


def extract_underlying(market: dict[str, Any]) -> str:
    im = market.get("imData") or {}
    meta = market.get("metadata") or {}
    return (
        im.get("underlying")
        or meta.get("assetSymbol")
        or meta.get("underlyingSymbol")
        or market.get("underlyingSymbol")
        or market.get("underlying")
        or ""
    )


def extract_collateral(market: dict[str, Any]) -> str:
    im = market.get("imData") or {}
    return (
        im.get("collateral")
        or market.get("collateral")
        or market.get("collateralAddress")
        or ""
    )


def extract_maturity_ts(market: dict[str, Any]) -> int | None:
    im = market.get("imData") or {}
    maturity = im.get("maturity") or market.get("maturity")
    if maturity:
        return int(maturity)
    return None


def time_to_maturity_days(maturity_ts: int) -> float:
    now = datetime.now(UTC).timestamp()
    return max(0.0, (maturity_ts - now) / 86400.0)


def parse_market_position(
    mkt_pos: dict[str, Any], token_id: int | None, *, is_cross: bool
) -> dict[str, Any] | None:
    """Parse a market position from collaterals response."""
    size_wei = int(
        mkt_pos.get("notionalSize")
        or mkt_pos.get("size")
        or mkt_pos.get("sizeWei")
        or 0
    )
    if size_wei == 0:
        return None

    pnl = mkt_pos.get("pnl", {})
    unrealized_pnl_wei = int(pnl.get("unrealisedPnl", 0) or 0)
    settled_pnl_wei = int(pnl.get("rateSettlementPnl", 0) or 0)

    return {
        "marketId": mkt_pos.get("marketId"),
        "marketAddress": mkt_pos.get("marketAddress"),
        "side": mkt_pos.get("side"),
        "sizeWei": size_wei,
        "size": float(Decimal(str(size_wei)) / Decimal(1e18)),
        "notionalSizeFloat": abs(float(Decimal(str(size_wei)) / Decimal(1e18))),
        "entryPrice": mkt_pos.get("entryPrice"),
        "tokenId": token_id,
        "isCross": is_cross,
        # APR fields (locked and current market rate)
        "fixedApr": mkt_pos.get("fixedApr"),
        "markApr": mkt_pos.get("markApr"),
        "impliedApr": mkt_pos.get("impliedApr"),
        "lastTradedApr": mkt_pos.get("lastTradedApr"),
        "unrealizedPnl": float(Decimal(str(unrealized_pnl_wei)) / Decimal(1e18)),
        "settledPnl": float(Decimal(str(settled_pnl_wei)) / Decimal(1e18)),
        "settledProgressPercentage": mkt_pos.get("settledProgressPercentage"),
    }
