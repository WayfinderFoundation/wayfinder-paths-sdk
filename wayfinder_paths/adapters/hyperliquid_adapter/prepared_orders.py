"""Single-attempt execution of persisted core-perp IOC orders.

The caller owns policy, fresh capacity/positions, builder/account setup and
durable IDs. This boundary must not refresh prices, change terms, enable account
modes, approve fees, or retry an uncertain submission behind that coordinator.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from hyperliquid.exchange import get_timestamp_ms
from hyperliquid.utils.signing import (
    OrderWire,
    float_to_wire,
    get_l1_action_payload,
    order_wires_to_order_action,
)
from hyperliquid.utils.types import BuilderInfo

from wayfinder_paths.core.constants.hyperliquid import DEFAULT_HYPERLIQUID_BUILDER_FEE


@dataclass(frozen=True)
class PerpIocOrder:
    asset_id: int
    signed_size: Decimal
    limit_price: Decimal

    def __post_init__(self) -> None:
        if (
            type(self.asset_id) is not int
            or not 0 <= self.asset_id < 10_000
            or not self.signed_size.is_finite()
            or self.signed_size == 0
            or not self.limit_price.is_finite()
            or self.limit_price <= 0
        ):
            raise ValueError("Invalid core-perp IOC order")

    def as_dict(self) -> dict[str, int | str]:
        return {
            "asset_id": self.asset_id,
            "signed_size": str(self.signed_size),
            "limit_price": str(self.limit_price),
        }


class IocNotSubmitted(Exception):
    """Validation/signing expired or failed before the exchange was called."""


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
    if (
        order.get("cloid") != cloid
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
        nonce = get_timestamp_ms()
        if (
            type(expires_after) is not int
            or not nonce < expires_after <= nonce + 30_000
            or type(reduce_only) is not bool
            or not 1 <= len(orders) <= 2
            or len(cloids) != len(orders)
            or len(set(cloids)) != len(cloids)
            or len({order.asset_id for order in orders}) != len(orders)
        ):
            raise ValueError("Invalid IOC batch or expired execution window")
        wires: list[OrderWire] = []
        for order, cloid in zip(orders, cloids, strict=True):
            if not cloid.startswith("0x") or len(cloid) != 34:
                raise ValueError("Invalid client order ID")
            UUID(cloid[2:])
            wires.append(
                {
                    "a": order.asset_id,
                    "b": order.signed_size > 0,
                    "p": float_to_wire(float(order.limit_price)),
                    "s": float_to_wire(float(abs(order.signed_size))),
                    "r": reduce_only,
                    "t": {"limit": {"tif": "Ioc"}},
                    "c": cloid,
                }
            )
        builder = BuilderInfo(
            b=DEFAULT_HYPERLIQUID_BUILDER_FEE["b"].lower(),
            f=DEFAULT_HYPERLIQUID_BUILDER_FEE["f"],
        )
        action = order_wires_to_order_action(wires, builder)
        payload = get_l1_action_payload(action, None, nonce, expires_after, True)
        signature = await sign(payload)
        if not signature or get_timestamp_ms() >= expires_after:
            raise ValueError("Signing failed or the IOC execution window expired")
    except Exception as exc:
        raise IocNotSubmitted("IOC batch was not submitted") from exc
    return await post_exchange(
        {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "expiresAfter": expires_after,
        }
    )
