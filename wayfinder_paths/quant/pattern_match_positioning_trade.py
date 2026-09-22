"""Positioning execution terms; no signing, providers, scheduling or retries.

The amount is gross notional across both legs, not margin. Protective exits
are user-selected additions to the frozen 24-hour research policy and must not
be reported as the results of that unmodified policy.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from math import isfinite
from typing import Literal

from wayfinder_paths.quant.pattern_match_positioning import positioning_weights


@dataclass(frozen=True)
class PositioningTradeLeg:
    asset_id: int
    signed_size: float
    reference_price: float

    @property
    def notional(self) -> float:
        return abs(self.signed_size) * self.reference_price


def size_positioning_trade(
    *,
    direction: int,
    hedge_beta: float,
    gross_notional: float,
    asset_id: int,
    asset_price: float,
    btc_asset_id: int,
    btc_price: float,
    size_decimals: Mapping[int, int],
) -> tuple[PositioningTradeLeg, ...]:
    """Round each required leg down; never replace a small hedge with no hedge.

    Metadata, prices and available margin must be freshly obtained by the
    caller. This only sizes the two-leg intent, not a capacity or fill promise.
    """
    from wayfinder_paths.adapters.hyperliquid_adapter.utils import round_size_for_asset
    from wayfinder_paths.core.constants.hyperliquid import MIN_ORDER_USD_NOTIONAL

    weights = positioning_weights(direction, hedge_beta)
    if (
        isinstance(gross_notional, bool)
        or not isfinite(gross_notional)
        or gross_notional <= 0
        or asset_id == btc_asset_id
        or any(
            isinstance(market, bool)
            or not isinstance(market, int)
            or not 0 <= market < 10_000
            for market in (asset_id, btc_asset_id)
        )
    ):
        raise ValueError("Invalid positioning amount or duplicate markets")
    result = []
    gross = Decimal(str(gross_notional))
    beta = Decimal(str(hedge_beta))
    asset_notional = gross / (1 + abs(beta))
    for market, price, weight, notional in (
        (asset_id, asset_price, weights[0], asset_notional),
        (btc_asset_id, btc_price, weights[1], asset_notional * abs(beta)),
    ):
        if weight == 0:
            continue
        if not isfinite(price) or price <= 0:
            raise ValueError("A live price is required for every positioning leg")
        decimals = size_decimals.get(market)
        if (
            isinstance(decimals, bool)
            or not isinstance(decimals, int)
            or not 0 <= decimals <= 8
        ):
            raise ValueError(
                "Valid size decimals are required for every positioning leg"
            )
        size = round_size_for_asset(
            size_decimals, market, notional / Decimal(str(price))
        )
        if Decimal(str(size)) * Decimal(str(price)) < Decimal(
            str(MIN_ORDER_USD_NOTIONAL)
        ):
            raise ValueError(
                "Increase the amount: every positioning leg must meet the $10 minimum"
            )
        result.append(PositioningTradeLeg(market, size if weight > 0 else -size, price))
    return tuple(result)


def positioning_exit_reason(
    *,
    net_return: float | None,
    stop_loss_return: float,
    take_profit_return: float,
    holding_period_elapsed: bool,
    leg_reduced: bool,
) -> Literal["leg_reduced", "time_limit", "stop_loss", "take_profit"] | None:
    """Exit the basket, not just the asset; the executor must reduce both legs.

    Missing PnL never means zero. The pre-authorized time exit and removal of
    an orphaned hedge do not require PnL to be available. Bounds are positive
    return fractions on gross entry notional, never leveraged margin returns.
    """
    if not all(
        isfinite(value) and value > 0
        for value in (stop_loss_return, take_profit_return)
    ):
        raise ValueError(
            "Positive finite stop-loss and take-profit returns are required"
        )
    if leg_reduced:
        return "leg_reduced"
    if holding_period_elapsed:
        return "time_limit"
    if net_return is None:
        return None
    if not isfinite(net_return):
        raise ValueError("Positioning return must be finite or unavailable")
    if net_return <= -stop_loss_return:
        return "stop_loss"
    if net_return >= take_profit_return:
        return "take_profit"
    return None
