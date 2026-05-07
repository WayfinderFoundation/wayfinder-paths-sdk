"""Sizing helpers for trigger-pattern strategies.

These are *opt-in* primitives a `decide()` function can call after queuing its
intended orders. The trigger driver itself does not invoke them — strategies
choose whether margin-aware throttling is appropriate for their venue/regime.
"""

from __future__ import annotations

from wayfinder_paths.core.perps.context import TriggerContext
from wayfinder_paths.core.perps.handlers.backtest import BacktestHandler
from wayfinder_paths.core.perps.handlers.protocol import MarketHandler, Side


def _all_handlers(ctx: TriggerContext) -> list:
    return [ctx.perp, *ctx.hip3.values()]


def _free_margin_from_ctx(ctx: TriggerContext, leverage: float) -> float:
    """Pre-trade free margin = NAV − margin currently in use across all venues."""
    nav = float(ctx.state.get("nav") or 0.0)
    if nav <= 0:
        return 0.0
    gross = 0.0
    for h in _all_handlers(ctx):
        if isinstance(h, BacktestHandler):
            i = h._bar_index  # noqa: SLF001
            for sym, sz in h._positions.items():  # noqa: SLF001
                if sz != 0:
                    gross += abs(sz) * float(h._prices_arr[i, h._sym_to_col[sym]])  # noqa: SLF001
    margin_in_use = gross / leverage if leverage > 0 else gross
    return max(0.0, nav - margin_in_use)


async def reservable_size_for(
    ctx: TriggerContext,
    handler: MarketHandler,
    symbol: str,
    side: Side,
    requested_size: float,
    *,
    leverage: float | None = None,
    cost_bps: float | None = None,
) -> float:
    """Convenience wrapper: compute free-margin from `ctx` and call `handler.reservable_size`.

    Decide function uses this to throttle each order one-at-a-time *before* placing —
    matches live exchange-rejection semantics (FIFO consumption of margin).
    """
    lev = float(leverage if leverage is not None else ctx.params.get("leverage", 1.0))
    if cost_bps is None:
        cost_bps = float(ctx.params.get("fee_bps", 0.0)) + float(ctx.params.get("slippage_bps", 0.0))
    free_margin = _free_margin_from_ctx(ctx, lev)
    return await handler.reservable_size(
        symbol, side, requested_size,
        free_margin=free_margin, leverage=lev, cost_bps=cost_bps,
    )


async def scale_pending_atomically(
    ctx: TriggerContext,
    *,
    leverage: float | None = None,
    cost_bps: float | None = None,
) -> float:
    """Throttle queued orders proportionally so margin + costs fit free cash.

    Mirrors the `get_atomic_trade_scale` step in `run_backtest`. Backtest-only:
    `BacktestHandler` exposes `pending_orders_view` / `scale_pending`. In live
    mode it's a no-op (the exchange enforces margin server-side).

    Args:
        ctx: trigger context.
        leverage: account leverage. Defaults to `ctx.params['leverage']` or 1.0.
        cost_bps: per-trade cost in bps applied as `notional * cost_bps/1e4`.
            Defaults to `(ctx.params['fee_bps'] + ctx.params['slippage_bps']) / 1e4`
            if both are present, else 0.

    Returns:
        The scale applied (1.0 if no throttling was needed).
    """
    handlers = _all_handlers(ctx)
    if not all(isinstance(h, BacktestHandler) for h in handlers):
        return 1.0  # live mode: exchange does this for us

    lev = float(leverage if leverage is not None else ctx.params.get("leverage", 1.0))
    if cost_bps is None:
        fee = float(ctx.params.get("fee_bps", 0.0))
        slip = float(ctx.params.get("slippage_bps", 0.0))
        cost_bps = fee + slip
    cost_rate = float(cost_bps) / 1e4

    nav = float(ctx.state.get("nav") or 0.0)
    if nav <= 0:
        return 1.0

    current_gross = 0.0
    for h in handlers:
        i = h._bar_index  # noqa: SLF001
        for sym, sz in h._positions.items():  # noqa: SLF001
            if sz == 0:
                continue
            current_gross += abs(sz) * float(h._prices_arr[i, h._sym_to_col[sym]])  # noqa: SLF001
    margin_in_use = current_gross / lev if lev > 0 else current_gross
    free_cash = max(0.0, nav - margin_in_use)

    margin_required = 0.0
    fee_required = 0.0
    for h in handlers:
        for o in h.pending_orders_view():
            trade_notional = abs(o["signed_delta"]) * o["mid"]
            old_gross = abs(o["current_size"]) * o["mid"]
            new_gross = abs(o["new_size"]) * o["mid"]
            gross_increase = max(0.0, new_gross - old_gross)
            margin_required += gross_increase / lev if lev > 0 else gross_increase
            fee_required += trade_notional * cost_rate

    total_required = margin_required + fee_required
    if total_required <= 1e-12:
        return 1.0
    scale = max(0.0, min(1.0, free_cash / total_required))
    if scale < 1.0:
        for h in handlers:
            h.scale_pending(scale)
    return scale
