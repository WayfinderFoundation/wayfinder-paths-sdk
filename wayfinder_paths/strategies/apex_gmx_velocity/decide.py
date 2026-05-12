"""Execution decisions for APEX/GMX Pair Velocity Strategy.

Signal weights already include `target_leverage` (sum |w| ≤ target_leverage
when entered). decide computes target sizes from current NAV, rounds each
order to the asset's szDecimals so HL signing accepts it, and rebalances
through the threshold.
"""

from __future__ import annotations

from wayfinder_paths.adapters.hyperliquid_adapter.utils import round_size_for_asset
from wayfinder_paths.core.perps.context import TriggerContext
from wayfinder_paths.core.perps.sizing import scale_pending_atomically


def _round_size(handler, symbol: str, raw_size: float) -> float:
    """Round size DOWN to the asset's szDecimals so HL accepts it."""
    adapter = getattr(handler, "adapter", None)
    if adapter is None:
        return raw_size
    asset_id = adapter.coin_to_asset.get(symbol)
    if asset_id is None:
        return raw_size
    return round_size_for_asset(adapter.asset_to_sz_decimals, asset_id, raw_size)


async def decide(ctx: TriggerContext) -> None:
    target_leverage = float(ctx.params.get("target_leverage", 1.0))
    min_order_usd = float(ctx.params.get("min_order_usd", 10.0))
    rebalance_threshold = float(ctx.params.get("rebalance_threshold", 0.02))

    if ctx.signal.targets.empty:
        return
    target_w = ctx.signal_at_now()
    gross = float(target_w.abs().sum())
    if gross > target_leverage and gross > 0:
        target_w = target_w * (target_leverage / gross)

    # In backtest the driver writes pre-trade NAV to state; in live the handler
    # exposes the real margin balance. Prefer state when present.
    nav_state = ctx.state.get("nav")
    nav = float(nav_state) if nav_state else await ctx.perp.get_margin_balance()
    if nav <= 0:
        ctx.state.set("nav", 0.0)
        return
    ctx.state.set("nav", float(nav))
    positions = await ctx.perp.get_positions()

    # Force-rebalance when current gross has drifted above target_leverage due
    # to adverse price moves. Lets reducing-gross trades bypass the threshold
    # so the book gets back inside the leverage budget.
    current_gross = sum(
        abs(positions[s].size * ctx.perp.mid(s))
        for s in positions
        if ctx.perp.mid(s) > 0
    )
    over_leveraged = nav > 0 and (current_gross / nav) > target_leverage + 1e-9

    for sym in target_w.index:
        target_weight = float(target_w[sym])
        mid = ctx.perp.mid(sym)
        if mid <= 0:
            continue

        cur_size = positions[sym].size if sym in positions else 0.0
        cur_notional = cur_size * mid
        cur_weight = cur_notional / nav if nav > 0 else 0.0

        target_size = (target_weight * nav) / mid
        diff = target_size - cur_size
        reducing_gross = abs(target_size * mid) < abs(cur_notional) - 1e-12

        if abs(target_weight - cur_weight) < rebalance_threshold and not (
            over_leveraged and reducing_gross
        ):
            continue

        if diff == 0:
            continue
        if abs(diff) * mid < min_order_usd:
            continue

        order_size = _round_size(ctx.perp, sym, abs(diff))
        if order_size <= 0 or order_size * mid < min_order_usd:
            continue

        side = "buy" if diff > 0 else "sell"
        reduce_only = (
            cur_size != 0
            and (cur_size > 0) != (diff > 0)
            and order_size <= abs(cur_size)
        )
        await ctx.perp.place_order(
            sym, side, order_size, "market", reduce_only=reduce_only
        )

    await scale_pending_atomically(ctx, leverage=target_leverage)
