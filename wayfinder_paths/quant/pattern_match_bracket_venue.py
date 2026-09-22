"""Native bracket preflight for core and HIP-3 perps; never sign or submit.

The host freezes the selected signal's absolute trigger prices. We normalize
them inward to valid perp ticks, reject already-crossed brackets and size from
fresh metadata. No account setup or caller-approved risk is changed implicitly.
"""

import asyncio
from dataclasses import dataclass
from decimal import Decimal

from wayfinder_paths.adapters.hyperliquid_adapter.execution_preflight import (
    InfoRequest,
    decimal_value,
    perp_ioc_order,
    perp_markets,
    read_perp_entry_preflight,
    validate_perp_capacity,
)
from wayfinder_paths.adapters.hyperliquid_adapter.prepared_orders import PerpIocOrder
from wayfinder_paths.adapters.hyperliquid_adapter.utils import (
    round_order_price,
    round_size_for_asset,
)


@dataclass(frozen=True)
class NativeBracketPlan:
    entry: PerpIocOrder
    take_profit_price: Decimal
    stop_loss_price: Decimal


async def _dex_offset(info: InfoRequest, dex: str) -> int:
    if not dex:
        return 0
    dexes = await info({"type": "perpDexs"})
    indices = [
        index for index, item in enumerate(dexes) if item and item["name"] == dex
    ]
    if len(indices) != 1 or indices[0] == 0:
        raise ValueError("Unknown or ambiguous perp dex")
    return 100_000 + indices[0] * 10_000


def _inward_trigger(price: Decimal, decimals: int, *, round_up: bool) -> Decimal:
    if not price.is_finite() or price <= 0:
        raise ValueError("Positive finite bracket prices are required")
    rounded = Decimal(
        str(round_order_price(float(price), 6 - decimals, round_up=round_up))
    )
    if (rounded - price) * (1 if round_up else -1) < 0:
        raise ValueError("Trigger rounding exceeded the reviewed bracket")
    return rounded


async def prepare_native_bracket(
    info: InfoRequest,
    *,
    address: str,
    coin: str,
    direction: int,
    gross_notional: Decimal,
    slippage_bps: int,
    take_profit_price: Decimal,
    stop_loss_price: Decimal,
) -> NativeBracketPlan:
    if (
        type(direction) is not int
        or direction not in (-1, 1)
        or not gross_notional.is_finite()
        or gross_notional <= 0
    ):
        raise ValueError("Invalid bracket direction or amount")
    dex = coin.partition(":")[0] if ":" in coin else ""
    (raw, fee_rate), offset = await asyncio.gather(
        read_perp_entry_preflight(info, address=address, coins=(coin,), dex=dex),
        _dex_offset(info, dex),
    )
    ids, prices, decimals = perp_markets(raw, offset=offset, dex=dex)
    if coin not in ids or ids[coin] not in prices or prices[ids[coin]] <= 0:
        raise ValueError("The bracket market has no live price")
    asset = ids[coin]
    meta, contexts = raw
    market, context = meta["universe"][asset - offset], contexts[asset - offset]
    # Receipts and user-entered amounts are USDC-denominated. Do not silently
    # size an identically named market against another collateral currency.
    if dex and meta["collateralToken"] != 0:
        raise ValueError("This bracket requires a USDC-collateralized perp")
    size = Decimal(
        str(round_size_for_asset(decimals, asset, gross_notional / prices[asset]))
    )
    entry = perp_ioc_order(asset, direction * size, prices, decimals, slippage_bps)
    take = _inward_trigger(take_profit_price, decimals[asset], round_up=direction < 0)
    stop = _inward_trigger(stop_loss_price, decimals[asset], round_up=direction > 0)
    mark = decimal_value(context["markPx"])
    # Native triggers observe mark, whereas IOC execution is bounded by limit.
    if mark <= 0 or any(
        direction * (take - price) <= 0 or direction * (price - stop) <= 0
        for price in (mark, entry.limit_price)
    ):
        raise ValueError("The current market has crossed the reviewed bracket")
    if dex:
        scale = decimal_value(market["deployerFeeScale"])
        if not 0 <= scale <= 3:
            raise ValueError("Invalid HIP-3 deployer fee scale")
        # No growth-mode/referral discounts are assumed for margin admission.
        fee_rate *= 1 + scale if scale < 1 else 2 * scale
    await validate_perp_capacity(
        info, address=address, coins=(coin,), orders=(entry,), fee_rate=fee_rate
    )
    return NativeBracketPlan(entry, take, stop)


async def prepare_native_bracket_exit(
    info: InfoRequest,
    *,
    coin: str,
    asset_id: int,
    remaining: Decimal,
    slippage_bps: int,
) -> tuple[PerpIocOrder, ...]:
    if not remaining:
        return ()
    dex = coin.partition(":")[0] if ":" in coin else ""
    raw, offset = await asyncio.gather(
        info({"type": "metaAndAssetCtxs", **({"dex": dex} if dex else {})}),
        _dex_offset(info, dex),
    )
    ids, prices, decimals = perp_markets(raw, offset=offset, dex=dex)
    if ids.get(coin) != asset_id:
        raise ValueError("Exit market identity changed")
    rounded = Decimal(str(round_size_for_asset(decimals, asset_id, abs(remaining))))
    size = rounded * (-1 if remaining > 0 else 1)
    return (perp_ioc_order(asset_id, size, prices, decimals, slippage_bps),)
