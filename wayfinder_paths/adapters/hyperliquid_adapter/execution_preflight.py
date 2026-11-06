"""Read-only checks shared by hosted perp brackets and positioning baskets.

Transport is injected. These helpers never approve fees, alter account mode,
change leverage, sign or submit orders.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from wayfinder_paths.adapters.hyperliquid_adapter.prepared_orders import PerpIocOrder
from wayfinder_paths.adapters.hyperliquid_adapter.utils import round_order_price
from wayfinder_paths.core.constants.hyperliquid import (
    DEFAULT_HYPERLIQUID_BUILDER_FEE,
    MIN_ORDER_USD_NOTIONAL,
)

InfoRequest = Callable[[dict[str, Any]], Awaitable[Any]]


def decimal_value(value: Any) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Non-finite Hyperliquid value")
    return number


def perp_markets(
    raw: Any, *, offset: int = 0, dex: str = ""
) -> tuple[dict[str, int], dict[int, Decimal], dict[int, int]]:
    meta, contexts = raw
    universe = meta["universe"]
    if len(universe) != len(contexts) or len(universe) > 10_000:
        raise ValueError("Incomplete perp metadata")
    ids, prices, decimals = {}, {}, {}
    for index, (asset, context) in enumerate(zip(universe, contexts, strict=True)):
        if asset.get("isDelisted"):
            continue
        coin = asset["name"]
        if coin in ids or (coin.partition(":")[0] if ":" in coin else "") != dex:
            raise ValueError("Duplicate or mismatched perp market")
        size_decimals = asset["szDecimals"]
        if type(size_decimals) is not int or not 0 <= size_decimals <= 8:
            raise ValueError("Invalid perp size decimals")
        asset_id = offset + index
        ids[coin] = asset_id
        if context.get("midPx") is not None:
            prices[asset_id] = decimal_value(context["midPx"])
        decimals[asset_id] = size_decimals
    return ids, prices, decimals


def perp_ioc_order(
    asset: int,
    size: Decimal,
    prices: Mapping[int, Decimal],
    decimals: Mapping[int, int],
    slippage_bps: int,
) -> PerpIocOrder:
    if type(slippage_bps) is not int or not 0 <= slippage_bps <= 100:
        raise ValueError("Perp slippage must be between 0 and 100 basis points")
    price = prices[asset]
    if price <= 0:
        raise ValueError("Missing live mid price")
    sign = 1 if size > 0 else -1
    limit = price * (1 + sign * Decimal(slippage_bps) / 10_000)
    rounded = round_order_price(float(limit), 6 - decimals[asset], round_up=size < 0)
    result = PerpIocOrder(asset, size, Decimal(str(rounded)))
    if sign * (result.limit_price - limit) > 0:
        raise ValueError("Price rounding exceeded the slippage bound")
    return result


async def read_perp_entry_preflight(
    info: InfoRequest, *, address: str, coins: Sequence[str], dex: str = ""
) -> tuple[Any, Decimal]:
    builder = DEFAULT_HYPERLIQUID_BUILDER_FEE
    scope = {"dex": dex} if dex else {}
    markets, state, open_orders, abstraction, builder_fee, fees = await asyncio.gather(
        info({"type": "metaAndAssetCtxs", **scope}),
        info({"type": "clearinghouseState", "user": address, **scope}),
        info({"type": "frontendOpenOrders", "user": address, **scope}),
        info({"type": "userAbstraction", "user": address}),
        info(
            {"type": "maxBuilderFee", "user": address, "builder": builder["b"].lower()}
        ),
        info({"type": "userFees", "user": address}),
    )
    if abstraction != "unifiedAccount":
        raise ValueError("Enable the unified Hyperliquid account before this trade")
    if decimal_value(builder_fee) < builder["f"]:
        raise ValueError("Approve the Wayfinder builder fee before this trade")
    if not isinstance(open_orders, list) or not isinstance(
        state["assetPositions"], list
    ):
        raise ValueError("Missing live positions or open orders")
    for item in state["assetPositions"]:
        position = item["position"]
        if position["coin"] in coins and decimal_value(position["szi"]) != 0:
            raise ValueError("Manage the existing position before opening this trade")
    if any(order["coin"] in coins for order in open_orders):
        raise ValueError(
            "Cancel existing orders in these markets before opening this trade"
        )
    state_time = datetime.fromtimestamp(state["time"] / 1000, UTC)
    if not -5 <= (datetime.now(UTC) - state_time).total_seconds() <= 30:
        raise ValueError("Position preflight is stale")
    fee_rate = decimal_value(fees["userCrossRate"])
    if fee_rate < 0:
        raise ValueError("Invalid taker fee rate")
    return markets, fee_rate


async def validate_perp_capacity(
    info: InfoRequest,
    *,
    address: str,
    coins: Sequence[str],
    orders: Sequence[PerpIocOrder],
    fee_rate: Decimal,
) -> None:
    capacities = await asyncio.gather(
        *(
            info({"type": "activeAssetData", "user": address, "coin": name})
            for name in coins
        )
    )
    required_margin = Decimal(0)
    margins = []
    fee_rate += Decimal(DEFAULT_HYPERLIQUID_BUILDER_FEE["f"]) / 100_000
    for name, order, capacity in zip(coins, orders, capacities, strict=True):
        if capacity["coin"] != name or capacity["user"].lower() != address.lower():
            raise ValueError("Trade capacity belongs to another market or wallet")
        side = 0 if order.signed_size > 0 else 1
        leverage = decimal_value(capacity["leverage"]["value"])
        if leverage <= 0 or abs(order.signed_size) > decimal_value(
            capacity["maxTradeSzs"][side]
        ):
            raise ValueError("Insufficient side-specific trade capacity")
        margins.append(decimal_value(capacity["availableToTrade"][side]))
        notional = abs(order.signed_size) * order.limit_price
        if notional < Decimal(str(MIN_ORDER_USD_NOTIONAL)):
            raise ValueError(
                "Increase the amount: each leg must meet the $10 limit-order minimum"
            )
        required_margin += notional / leverage + notional * fee_rate
    # Both basket legs spend the same margin; never budget it twice.
    if required_margin > min(margins):
        raise ValueError("Insufficient combined margin for the trade and entry fees")
