"""Read-only reconciliation of a persisted native bracket's individual exits."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from wayfinder_paths.adapters.hyperliquid_adapter.prepared_orders import (
    IocFill,
    reconcile_ioc_fill,
)


@dataclass(frozen=True)
class NativeExitReceipt:
    state: Literal["open", "triggered", "terminal"]
    order_id: int
    # Only terminal receipts have a final fill, including a confirmed zero.
    fill: IocFill | None = None


def reconcile_bracket_exit(
    status: dict[str, Any],
    fills: Sequence[dict[str, Any]],
    *,
    cloid: str,
    coin: str,
    signed_size: Decimal,
    trigger_price: Decimal,
    tpsl: Literal["tp", "sl"],
) -> NativeExitReceipt | None:
    """Unknown is not absent, and triggered is not filled or safely canceled.

    The host fetches fills through statusTimestamp, exactly as for IOC receipts.
    Only an identity-checked, fixed-size, reduce-only market trigger counts as
    live protection. Completed trigger orders may be reported as Market orders,
    so their terminal identity/fills use the shared IOC reconciliation instead.
    """
    fill = reconcile_ioc_fill(
        status, fills, cloid=cloid, coin=coin, signed_size=signed_size
    )
    if fill is not None:
        return NativeExitReceipt("terminal", status["order"]["order"]["oid"], fill)
    if status.get("status") != "order":
        return None
    envelope = status["order"]
    order, state = envelope["order"], envelope["status"]
    if state == "triggered":
        return NativeExitReceipt("triggered", order["oid"])
    if state != "open":
        return None  # Includes a terminal order with incomplete fill history.
    if (
        order.get("isTrigger") is not True
        or order.get("reduceOnly") is not True
        or order.get("isPositionTpsl") is not False
        or order.get("orderType")
        != ("Take Profit Market" if tpsl == "tp" else "Stop Market")
        or Decimal(order["triggerPx"]) != trigger_price
        or Decimal(order["sz"]) != abs(signed_size)
    ):
        raise ValueError("Live exit does not match the persisted native bracket")
    return NativeExitReceipt("open", order["oid"])
