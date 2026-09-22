"""Core-perp positioning execution reads and account attribution.

Provider transport and wallet policy are injected by the host. No signing,
account-mode changes, leverage changes or fee approvals happen in these reads.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from wayfinder_paths.adapters.hyperliquid_adapter.prepared_orders import (
    IocFill,
    PerpIocOrder,
)
from wayfinder_paths.adapters.hyperliquid_adapter.utils import (
    round_order_price,
    round_size_for_asset,
)
from wayfinder_paths.core.constants.hyperliquid import (
    DEFAULT_HYPERLIQUID_BUILDER_FEE,
    MIN_ORDER_USD_NOTIONAL,
)
from wayfinder_paths.quant.pattern_match_positioning_trade import size_positioning_trade

InfoRequest = Callable[[dict[str, Any]], Awaitable[Any]]


def _decimal(value: Any) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Non-finite Hyperliquid value")
    return number


def _markets(raw: Any) -> tuple[dict[str, int], dict[int, Decimal], dict[int, int]]:
    meta, contexts = raw
    universe = meta["universe"]
    if len(universe) != len(contexts):
        raise ValueError("Incomplete core-perp metadata")
    ids, prices, decimals = {}, {}, {}
    for asset_id, (asset, context) in enumerate(zip(universe, contexts, strict=True)):
        if asset.get("isDelisted"):
            continue
        coin = asset["name"]
        if coin in ids:
            raise ValueError("Duplicate core-perp market")
        ids[coin] = asset_id
        if context.get("midPx") is not None:
            prices[asset_id] = _decimal(context["midPx"])
        decimals[asset_id] = asset["szDecimals"]
    return ids, prices, decimals


def _order(
    asset: int,
    size: Decimal,
    prices: Mapping[int, Decimal],
    decimals: Mapping[int, int],
    slippage_bps: int,
) -> PerpIocOrder:
    if type(slippage_bps) is not int or not 0 <= slippage_bps <= 100:
        raise ValueError("Positioning slippage must be between 0 and 100 basis points")
    price = prices[asset]
    if price <= 0:
        raise ValueError("Missing live mid price")
    sign = 1 if size > 0 else -1
    limit = price * (1 + sign * Decimal(slippage_bps) / 10_000)
    rounded = round_order_price(float(limit), 6 - decimals[asset], round_up=size < 0)
    result = PerpIocOrder(asset, size, Decimal(str(rounded)))
    # Float conversion must never loosen the caller's slippage limit.
    if (size > 0 and result.limit_price > limit) or (
        size < 0 and result.limit_price < limit
    ):
        raise ValueError("Price rounding exceeded the slippage bound")
    return result


async def prepare_positioning_entry(
    info: InfoRequest,
    *,
    address: str,
    coin: str,
    direction: int,
    hedge_beta: float,
    gross_notional: Decimal,
    slippage_bps: int,
) -> tuple[PerpIocOrder, ...]:
    builder = DEFAULT_HYPERLIQUID_BUILDER_FEE
    markets, state, open_orders, abstraction, builder_fee, fees = await asyncio.gather(
        info({"type": "metaAndAssetCtxs"}),
        info({"type": "clearinghouseState", "user": address}),
        info({"type": "frontendOpenOrders", "user": address}),
        info({"type": "userAbstraction", "user": address}),
        info(
            {"type": "maxBuilderFee", "user": address, "builder": builder["b"].lower()}
        ),
        info({"type": "userFees", "user": address}),
    )
    if abstraction != "unifiedAccount":
        raise ValueError("Enable the unified Hyperliquid account before this trade")
    if _decimal(builder_fee) < builder["f"]:
        raise ValueError("Approve the Wayfinder builder fee before this trade")
    if not isinstance(open_orders, list) or not isinstance(
        state["assetPositions"], list
    ):
        raise ValueError("Missing live positions or open orders")
    coins = (coin, "BTC") if hedge_beta else (coin,)
    for item in state["assetPositions"]:
        position = item["position"]
        if position["coin"] in coins and _decimal(position["szi"]) != 0:
            raise ValueError("Manage the existing position before opening this basket")
    if any(order["coin"] in coins for order in open_orders):
        raise ValueError(
            "Cancel existing orders in these markets before opening this basket"
        )
    state_time = datetime.fromtimestamp(state["time"] / 1000, UTC)
    if not -5 <= (datetime.now(UTC) - state_time).total_seconds() <= 30:
        raise ValueError("Position preflight is stale")
    ids, prices, decimals = _markets(markets)
    legs = size_positioning_trade(
        direction=direction,
        hedge_beta=hedge_beta,
        gross_notional=float(gross_notional),
        asset_id=ids[coin],
        asset_price=float(prices[ids[coin]]),
        btc_asset_id=ids["BTC"],
        btc_price=float(prices.get(ids["BTC"], Decimal(0))),
        size_decimals=decimals,
    )
    orders = tuple(
        _order(
            leg.asset_id, Decimal(str(leg.signed_size)), prices, decimals, slippage_bps
        )
        for leg in legs
    )
    capacities = await asyncio.gather(
        *(
            info({"type": "activeAssetData", "user": address, "coin": name})
            for name in coins
        )
    )
    required_margin = Decimal(0)
    margins = []
    fee_rate = _decimal(fees["userCrossRate"]) + Decimal(builder["f"]) / 100_000
    if fee_rate < 0:
        raise ValueError("Invalid taker fee rate")
    for name, order, capacity in zip(coins, orders, capacities, strict=True):
        if capacity["coin"] != name or capacity["user"].lower() != address.lower():
            raise ValueError("Trade capacity belongs to another market or wallet")
        side = 0 if order.signed_size > 0 else 1
        leverage = _decimal(capacity["leverage"]["value"])
        if leverage <= 0 or abs(order.signed_size) > _decimal(
            capacity["maxTradeSzs"][side]
        ):
            raise ValueError("Insufficient side-specific trade capacity")
        margins.append(_decimal(capacity["availableToTrade"][side]))
        notional = abs(order.signed_size) * order.limit_price
        if notional < Decimal(str(MIN_ORDER_USD_NOTIONAL)):
            raise ValueError(
                "Increase the amount: each leg must meet the $10 limit-order minimum"
            )
        required_margin += notional / leverage + notional * fee_rate
    # Both legs draw from the same wallet. Checking each against the full free
    # margin independently would allow the pair to spend it twice.
    if required_margin > min(margins):
        raise ValueError("Insufficient margin for both legs and entry fees")
    return orders


async def prepare_positioning_exit(
    info: InfoRequest,
    *,
    remaining: Mapping[int, Decimal],
    coins: Mapping[int, str],
    slippage_bps: int,
) -> tuple[PerpIocOrder, ...]:
    ids, prices, decimals = _markets(await info({"type": "metaAndAssetCtxs"}))
    result = []
    for asset, size in remaining.items():
        if not size:
            continue
        if ids.get(coins[asset]) != asset:
            raise ValueError("Exit market identity changed")
        rounded = round_size_for_asset(decimals, asset, abs(size))
        close_size = Decimal(str(rounded)) * (-1 if size > 0 else 1)
        result.append(_order(asset, close_size, prices, decimals, slippage_bps))
    return tuple(result)


@dataclass(frozen=True)
class PositioningAccountSnapshot:
    as_of: datetime
    # Attributable remaining sizes, capped to the live position on that side.
    positions: dict[int, Decimal]
    net_return: float | None
    unmanaged_trade: bool = False


def positioning_account_snapshot(
    *,
    state: dict[str, Any],
    coins: Mapping[int, str],
    entries: Sequence[tuple[PerpIocOrder, IocFill]],
    exits: Sequence[tuple[PerpIocOrder, IocFill]],
    fills: Sequence[dict[str, Any]],
    funding: Sequence[dict[str, Any]] | None,
) -> PositioningAccountSnapshot:
    """Attribute inventory from fills, not a wallet's coincidentally equal size.

    External reductions consume tracked inventory first; later additions never
    recreate it. Any outside trade in these markets triggers basket cleanup.
    PnL is only needed while the original entry remains intact, before exits.
    """
    by_coin = {name: asset for asset, name in coins.items()}
    receipts = {
        fill.order_id: (order, fill) for order, fill in (*entries, *exits) if fill.size
    }
    if None in receipts or len(receipts) != sum(
        bool(fill.size) for _, fill in (*entries, *exits)
    ):
        raise ValueError("Missing confirmed venue order identity")
    entry_ids = {fill.order_id for _, fill in entries if fill.size}
    # Trade IDs are identities, not execution sequence numbers. Multiple entry
    # fills in one millisecond are ordered by starting exposure. Process those
    # before external reductions in that millisecond, conservatively consuming
    # tracked inventory even when an outside trade races the initial entry.
    history = sorted(
        {fill["tid"]: fill for fill in fills}.values(),
        key=lambda fill: (
            fill["time"],
            fill["oid"] not in entry_ids,
            abs(_decimal(fill["startPosition"]))
            if fill["oid"] in entry_ids
            else Decimal(0),
            fill["tid"],
        ),
    )
    totals = dict.fromkeys(receipts, Decimal(0))
    owned = dict.fromkeys(coins, Decimal(0))
    entry_times: dict[int, int] = {}
    for fill in history:
        if fill["oid"] in entry_ids:
            entry_asset = receipts[fill["oid"]][0].asset_id
            entry_times.setdefault(entry_asset, fill["time"])
    fees = entry_value = Decimal(0)
    unmanaged = False
    for fill in history:
        asset = by_coin.get(fill["coin"])
        if asset is None:
            continue
        amount = _decimal(fill["sz"])
        if amount <= 0 or fill["side"] not in {"B", "A"}:
            raise ValueError("Invalid wallet fill")
        signed = amount if fill["side"] == "B" else -amount
        receipt = receipts.get(fill["oid"])
        if receipt is not None:
            order, expected = receipt
            if (
                order.asset_id != asset
                or signed * order.signed_size <= 0
                or fill.get("feeToken") != "USDC"
            ):
                raise ValueError("Wrong market, side or fee currency for owned fill")
            totals[fill["oid"]] += amount
            if fill["oid"] in entry_ids:
                price = _decimal(fill["px"])
                if price <= 0:
                    raise ValueError("Invalid entry fill price")
                entry_value += amount * price
            if (
                fill["oid"] in entry_ids
                and _decimal(fill["startPosition"]) != owned[asset]
            ):
                unmanaged = True
            owned[asset] += signed
            fees += _decimal(fill["fee"])
        elif asset in entry_times and fill["time"] >= entry_times[asset]:
            unmanaged = True
            if signed * owned[asset] < 0:
                owned[asset] = (1 if owned[asset] > 0 else -1) * max(
                    Decimal(0), abs(owned[asset]) - amount
                )
    if any(totals[oid] != receipt.size for oid, (_, receipt) in receipts.items()):
        raise ValueError("Fill history does not cover every confirmed order")
    live = {
        item["position"]["coin"]: item["position"] for item in state["assetPositions"]
    }
    positions = {}
    for asset, size in owned.items():
        current = _decimal(live.get(coins[asset], {}).get("szi", "0"))
        positions[asset] = (
            (1 if size > 0 else -1) * min(abs(size), abs(current))
            if size * current > 0
            else Decimal(0)
        )
    as_of = datetime.fromtimestamp(state["time"] / 1000, UTC)
    result = PositioningAccountSnapshot(as_of, positions, None, unmanaged)
    if unmanaged or exits or funding is None or len(funding) >= 500:
        return result
    pnl = Decimal(0)
    for order, entry_receipt in entries:
        if (
            entry_receipt.average_price is None
            or positions[order.asset_id] != order.signed_size
        ):
            return result
        position = live[coins[order.asset_id]]
        pnl += _decimal(position["unrealizedPnl"])
    for event in funding:
        delta = event["delta"]
        asset = by_coin.get(delta.get("coin"))
        if asset is not None and event["time"] > entry_times[asset]:
            if delta.get("type") != "funding" or _decimal(delta["szi"]) != owned[asset]:
                return result
            pnl += _decimal(delta["usdc"])
    if entry_value <= 0:
        raise ValueError("No confirmed entry notional")
    return PositioningAccountSnapshot(
        as_of, positions, float((pnl - fees) / entry_value)
    )
