from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast

from wayfinder_paths.mcp.arg_validation import MCPArgumentError
from wayfinder_paths.mcp.utils import throw_if_not_number


def as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def first_present(source: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = source.get(key)
        if value is not None and value != "":
            return value
    return None


def normalize_pm_side(side: Any) -> Literal["BUY", "SELL"]:
    normalized = str(side or "").strip().upper()
    if normalized not in {"BUY", "SELL"}:
        raise MCPArgumentError(
            "side must be BUY or SELL",
            field="side",
            received=side,
            allowed_values={"BUY", "SELL"},
        )
    return cast(Literal["BUY", "SELL"], normalized)


def validate_pm_market_order_size(
    *,
    side: Literal["BUY", "SELL"],
    buy_amount_pusd: float | None,
    sell_amount_shares: float | None,
) -> dict[str, Any]:
    has_buy = buy_amount_pusd is not None
    has_sell = sell_amount_shares is not None
    if has_buy and has_sell:
        raise MCPArgumentError(
            "pass exactly one sizing field: buy_amount_pusd for BUY or "
            "sell_amount_shares for SELL",
            field="side",
            received={
                "side": side,
                "buy_amount_pusd": buy_amount_pusd,
                "sell_amount_shares": sell_amount_shares,
            },
            suggested_arguments={
                "side": side,
                "buy_amount_pusd": 10.0 if side == "BUY" else None,
                "sell_amount_shares": 10.0 if side == "SELL" else None,
            },
        )
    if side == "BUY":
        if not has_buy:
            if has_sell:
                raise MCPArgumentError(
                    "BUY requires buy_amount_pusd; sell_amount_shares is only for SELL",
                    field="buy_amount_pusd",
                    received=buy_amount_pusd,
                    suggested_arguments={
                        "buy_amount_pusd": sell_amount_shares,
                        "sell_amount_shares": None,
                    },
                )
            raise MCPArgumentError(
                "BUY requires buy_amount_pusd in pUSD collateral units",
                field="buy_amount_pusd",
                received=buy_amount_pusd,
                suggested_arguments={"buy_amount_pusd": 10.0},
            )
        amount = throw_if_not_number(
            "buy_amount_pusd must be a number", buy_amount_pusd
        )
        if amount <= 0:
            raise MCPArgumentError(
                "buy_amount_pusd must be positive",
                field="buy_amount_pusd",
                received=buy_amount_pusd,
            )
        return {
            "sizing_kind": "buy_amount_pusd",
            "buy_amount_pusd": amount,
            "sell_amount_shares": None,
            "adapter_amount": amount,
        }

    if not has_sell:
        if has_buy:
            raise MCPArgumentError(
                "SELL requires sell_amount_shares; buy_amount_pusd is only for BUY",
                field="sell_amount_shares",
                received=sell_amount_shares,
                suggested_arguments={
                    "buy_amount_pusd": None,
                    "sell_amount_shares": buy_amount_pusd,
                },
            )
        raise MCPArgumentError(
            "SELL requires sell_amount_shares in outcome-share units",
            field="sell_amount_shares",
            received=sell_amount_shares,
            suggested_arguments={"sell_amount_shares": 10.0},
        )
    amount = throw_if_not_number(
        "sell_amount_shares must be a number", sell_amount_shares
    )
    if amount <= 0:
        raise MCPArgumentError(
            "sell_amount_shares must be positive",
            field="sell_amount_shares",
            received=sell_amount_shares,
        )
    return {
        "sizing_kind": "sell_amount_shares",
        "buy_amount_pusd": None,
        "sell_amount_shares": amount,
        "adapter_amount": amount,
    }


def safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return float(numerator) / float(denominator)


def normalize_pm_execution_summary(
    *,
    side: Literal["BUY", "SELL"],
    sizing: dict[str, Any],
    quote: dict[str, Any] | None,
    raw: dict[str, Any] | None = None,
    summary_source: str = "adapter_quote",
    failed: bool = False,
) -> dict[str, Any]:
    q = quote if isinstance(quote, dict) else {}
    requested_collateral = sizing.get("buy_amount_pusd")
    requested_shares = sizing.get("sell_amount_shares")
    collateral_spent = as_float(q.get("notional_usdc")) if side == "BUY" else None
    collateral_received = as_float(q.get("notional_usdc")) if side == "SELL" else None
    shares_filled = as_float(q.get("shares"))
    avg_price = as_float(first_present(q, "average_price", "avgPrice", "avg_price"))
    fill_ratio = (
        safe_ratio(collateral_spent, requested_collateral)
        if side == "BUY"
        else safe_ratio(shares_filled, requested_shares)
    )
    has_fill = bool((shares_filled or 0) > 0) or bool(
        (collateral_spent or collateral_received or 0) > 0
    )
    if failed:
        status = "failed"
    elif q.get("fully_fillable") is True:
        status = "filled"
    elif has_fill:
        status = "partial"
    else:
        status = "rejected"

    return {
        "side": side,
        "inputAmountType": "collateral" if side == "BUY" else "shares",
        "requestedCollateral": requested_collateral,
        "requestedShares": requested_shares,
        "collateralSpent": collateral_spent,
        "collateralReceived": collateral_received,
        "sharesFilled": shares_filled,
        "avgPrice": avg_price,
        "fillRatio": fill_ratio,
        "status": status,
        "summarySource": summary_source,
        "rawStatus": raw.get("status") if isinstance(raw, dict) else None,
    }
