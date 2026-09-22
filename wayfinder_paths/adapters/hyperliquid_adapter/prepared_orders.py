"""Single-attempt execution of persisted perp IOC orders and native brackets.

The caller owns policy, fresh capacity/positions, builder/account setup and
durable IDs. This boundary must not refresh prices, change terms, enable account
modes, approve fees, or retry an uncertain submission behind that coordinator.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from hyperliquid.exchange import get_timestamp_ms
from hyperliquid.utils.signing import (
    OrderWire,
    float_to_wire,
    get_l1_action_payload,
    order_wires_to_order_action,
)
from hyperliquid.utils.types import OUTCOME_ASSET_OFFSET, BuilderInfo

from wayfinder_paths.core.constants.hyperliquid import DEFAULT_HYPERLIQUID_BUILDER_FEE


@dataclass(frozen=True)
class PerpIocOrder:
    asset_id: int
    signed_size: Decimal
    limit_price: Decimal

    def __post_init__(self) -> None:
        if (
            type(self.asset_id) is not int
            or not (
                0 <= self.asset_id < 10_000
                or 110_000 <= self.asset_id < OUTCOME_ASSET_OFFSET
            )
            or not self.signed_size.is_finite()
            or self.signed_size == 0
            or not self.limit_price.is_finite()
            or self.limit_price <= 0
        ):
            raise ValueError("Invalid perp IOC order")

    def as_dict(self) -> dict[str, int | str]:
        return {
            "asset_id": self.asset_id,
            "signed_size": str(self.signed_size),
            "limit_price": str(self.limit_price),
        }


class IocNotSubmitted(Exception):
    """Validation/signing expired or failed before the exchange was called."""


class BracketNotSubmitted(Exception):
    """No part of the bracket was submitted; validation/signing failed first."""


@dataclass(frozen=True)
class IocFill:
    """Final cumulative IOC receipt. Fees are unavailable in exchange acknowledgements."""

    size: Decimal
    average_price: Decimal | None = None
    order_id: int | None = None
    fee_usd: Decimal | None = None

    def __post_init__(self) -> None:
        if (
            not self.size.is_finite()
            or self.size < 0
            or (self.size > 0 and self.average_price is None)
            or (
                self.average_price is not None
                and (not self.average_price.is_finite() or self.average_price <= 0)
            )
            or (
                self.order_id is not None
                and (type(self.order_id) is not int or self.order_id < 0)
            )
            or (self.fee_usd is not None and not self.fee_usd.is_finite())
        ):
            raise ValueError("Invalid IOC fill")

    def as_dict(self) -> dict[str, int | str | None]:
        result: dict[str, int | str | None] = {
            "size": str(self.size),
            "average_price": str(self.average_price)
            if self.average_price is not None
            else None,
        }
        if self.order_id is not None:
            result["order_id"] = self.order_id
        if self.fee_usd is not None:
            result["fee_usd"] = str(self.fee_usd)
        return result


def ioc_response_fills(
    response: dict[str, Any], count: int
) -> tuple[IocFill | None, ...]:
    """Missing/resting/unrecognized acknowledgements remain unresolved."""
    if response.get("status") == "err":
        return tuple(IocFill(Decimal(0)) for _ in range(count))
    if (
        response.get("status") != "ok"
        or response.get("response", {}).get("type") != "order"
    ):
        return (None,) * count
    statuses = response["response"].get("data", {}).get("statuses", [])
    if len(statuses) != count:
        return (None,) * count
    result: list[IocFill | None] = []
    for status in statuses:
        if isinstance(status, dict) and "filled" in status:
            fill = status["filled"]
            result.append(
                IocFill(Decimal(fill["totalSz"]), Decimal(fill["avgPx"]), fill["oid"])
            )
        elif isinstance(status, dict) and "error" in status:
            result.append(IocFill(Decimal(0)))
        else:
            result.append(None)
    return tuple(result)


def reconcile_ioc_fill(
    status: dict[str, Any],
    fills: Sequence[dict[str, Any]],
    *,
    cloid: str,
    coin: str,
    signed_size: Decimal,
) -> IocFill | None:
    """Require a terminal matching order AND complete fills for its filled size.

    A missing/aged-out order, truncated history or lagging fills is unresolved.
    The caller must fetch history through the order's statusTimestamp.
    """
    if status.get("status") != "order":
        return None
    envelope = status["order"]
    order, state = envelope["order"], envelope["status"]
    observed_cloid = order.get("cloid")
    if (
        not isinstance(observed_cloid, str)
        or observed_cloid.lower() != cloid.lower()
        or order.get("coin") != coin
        or order.get("side") != ("B" if signed_size > 0 else "A")
        or Decimal(order["origSz"]) != abs(signed_size)
    ):
        raise ValueError("Order status does not match the persisted IOC")
    if state == "rejected" or state.endswith("Rejected"):
        return IocFill(Decimal(0), order_id=order["oid"], fee_usd=Decimal(0))
    if state not in {"filled", "canceled", "scheduledCancel"} and not state.endswith(
        "Canceled"
    ):
        return None
    expected = abs(signed_size) - Decimal(order["sz"])
    if not 0 <= expected <= abs(signed_size):
        raise ValueError("Invalid remaining IOC size")
    matching = {fill["tid"]: fill for fill in fills if fill.get("oid") == order["oid"]}
    size = value = fees = Decimal(0)
    for fill in matching.values():
        if (
            fill.get("coin") != coin
            or fill.get("side") != order["side"]
            or fill.get("feeToken") != "USDC"
        ):
            raise ValueError("Unexpected IOC fill attribution")
        amount, price, fee = (
            Decimal(fill["sz"]),
            Decimal(fill["px"]),
            Decimal(fill["fee"]),
        )
        if (
            not all(value.is_finite() for value in (amount, price, fee))
            or min(amount, price) <= 0
        ):
            raise ValueError("Invalid IOC fill values")
        size += amount
        value += amount * price
        fees += (
            fee  # Hyperliquid's fee already includes builderFee; never add it twice.
        )
    if size != expected:
        return None
    return IocFill(size, value / size if size else None, order["oid"], fees)


async def submit_prepared_ioc(
    orders: Sequence[PerpIocOrder],
    *,
    cloids: Sequence[str],
    reduce_only: bool,
    expires_after: int,
    sign: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    post_exchange: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Sign the exact persisted terms with expiry and submit exactly once.

    IOC batches are not atomic. A returned response must be inspected per leg.
    An exception after post_exchange starts is uncertain, NOT IocNotSubmitted.
    """
    try:
        if (
            type(reduce_only) is not bool
            or not 1 <= len(orders) <= 2
            or len({order.asset_id for order in orders}) != len(orders)
        ):
            raise ValueError("Invalid IOC batch")
        _validate_cloids(cloids, len(orders))
        payload = await _sign_prepared_orders(
            [
                _ioc_wire(order, cloid, reduce_only=reduce_only)
                for order, cloid in zip(orders, cloids, strict=True)
            ],
            grouping="na",
            expires_after=expires_after,
            sign=sign,
        )
    except Exception as exc:
        raise IocNotSubmitted("IOC batch was not submitted") from exc
    return await post_exchange(payload)


async def submit_prepared_bracket(
    entry: PerpIocOrder,
    *,
    take_profit_price: Decimal,
    stop_loss_price: Decimal,
    cloids: Sequence[str],
    expires_after: int,
    sign: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    post_exchange: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Submit one IOC entry with fixed-size, reduce-only market TP/SL children.

    IDs are ordered entry, take profit, stop loss. Persist all three before
    calling. The response is NOT proof of a protected position: inspect each
    order, and reconcile by ID after uncertainty. A partially filled IOC can
    cancel its children; repair only the confirmed remaining exposure, never
    resend the entry. Expiry bounds submission, not the lifetime of the exits.
    """
    try:
        _validate_cloids(cloids, 3)
        if any(
            not price.is_finite() or price <= 0
            for price in (take_profit_price, stop_loss_price)
        ):
            raise ValueError("Invalid bracket trigger price")
        lower, upper = (
            (stop_loss_price, take_profit_price)
            if entry.signed_size > 0
            else (take_profit_price, stop_loss_price)
        )
        if not lower < entry.limit_price < upper:
            raise ValueError("Entry limit must be inside the bracket")
        parent = _ioc_wire(entry, cloids[0], reduce_only=False)
        wires = [parent]
        for price, tpsl, cloid in (
            (take_profit_price, "tp", cloids[1]),
            (stop_loss_price, "sl", cloids[2]),
        ):
            trigger = _exact_wire_decimal(price)
            wires.append(
                {
                    "a": entry.asset_id,
                    "b": not parent["b"],
                    "p": trigger,
                    "s": parent["s"],
                    "r": True,
                    "t": {
                        "trigger": {
                            "isMarket": True,
                            "triggerPx": trigger,
                            "tpsl": tpsl,
                        }
                    },
                    "c": cloid,
                }
            )
        payload = await _sign_prepared_orders(
            wires, grouping="normalTpsl", expires_after=expires_after, sign=sign
        )
    except Exception as exc:
        raise BracketNotSubmitted("Bracket was not submitted") from exc
    return await post_exchange(payload)


def _validate_cloids(cloids: Sequence[str], count: int) -> None:
    if len(cloids) != count or len({cloid.lower() for cloid in cloids}) != count:
        raise ValueError("Each order needs a distinct persisted client ID")
    for cloid in cloids:
        if not cloid.startswith("0x") or len(cloid) != 34:
            raise ValueError("Invalid client order ID")
        UUID(cloid[2:])


def _exact_wire_decimal(value: Decimal) -> str:
    wire = float_to_wire(float(value))
    if Decimal(wire) != value:
        raise ValueError("Order precision would change persisted terms")
    return wire


def _ioc_wire(order: PerpIocOrder, cloid: str, *, reduce_only: bool) -> OrderWire:
    return {
        "a": order.asset_id,
        "b": order.signed_size > 0,
        "p": _exact_wire_decimal(order.limit_price),
        "s": _exact_wire_decimal(abs(order.signed_size)),
        "r": reduce_only,
        "t": {"limit": {"tif": "Ioc"}},
        "c": cloid,
    }


async def _sign_prepared_orders(
    wires: list[OrderWire],
    *,
    grouping: Literal["na", "normalTpsl"],
    expires_after: int,
    sign: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Shared signing only: callers keep dispatch outside their unsent guard."""
    nonce = get_timestamp_ms()
    if type(expires_after) is not int or not nonce < expires_after <= nonce + 30_000:
        raise ValueError("Invalid or expired execution window")
    builder = BuilderInfo(
        b=DEFAULT_HYPERLIQUID_BUILDER_FEE["b"].lower(),
        f=DEFAULT_HYPERLIQUID_BUILDER_FEE["f"],
    )
    action = order_wires_to_order_action(wires, builder, grouping)
    payload = get_l1_action_payload(action, None, nonce, expires_after, True)
    signature = await sign(payload)
    if not signature or get_timestamp_ms() >= expires_after:
        raise ValueError("Signing failed or the execution window expired")
    return {
        "action": action,
        "nonce": nonce,
        "signature": signature,
        "expiresAfter": expires_after,
    }
