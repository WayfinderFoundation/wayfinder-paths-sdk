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
    nav = float(ctx.nav or 0.0)
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
        cost_bps = float(ctx.params.get("fee_bps", 0.0)) + float(
            ctx.params.get("slippage_bps", 0.0)
        )
    free_margin = _free_margin_from_ctx(ctx, lev)
    return await handler.reservable_size(
        symbol,
        side,
        requested_size,
        free_margin=free_margin,
        leverage=lev,
        cost_bps=cost_bps,
    )


def compute_atomic_scale(
    pending: list[dict[str, float]],
    *,
    nav: float,
    leverage: float,
    cost_bps: float = 0.0,
    current_gross_override: float | None = None,
) -> float:
    """Pure-function atomic-scale: stage intended trades, compute the [0, 1]
    factor that keeps (margin + fees) within free cash, return it.

    Lives parallel to `scale_pending_atomically`. That one operates on the
    backtest handler's internal pending-order queue (post-`place_order`); this
    one operates on a caller-supplied list of intended trades (pre-`place_order`).
    Live `MarketHandler.place_order` ships orders immediately and the exchange
    trims FIFO under tight margin — so a multi-leg `decide()` MUST compute
    scaling *before* placing the first order, then apply that scale to every
    leg's size. Otherwise the first leg starves the rest. Backtest and live
    converge on identical sizes when both use this helper.

    Math is the same as `scale_pending_atomically`. Calling both — pre-place
    here, post-place `scale_pending_atomically` — is safe: the second pass
    becomes a no-op once the first pass has already trimmed pending notional
    into the free-cash budget.

    Args:
        pending: each entry must contain `current_size` (signed), `new_size`
            (signed post-trade), and `mid` (reference mid for the symbol).
        nav: pre-trade account value.
        leverage: account leverage budget (matches the venue's leverage cap).
        cost_bps: fee + slippage in basis points charged on `|signed_delta|×mid`.
        current_gross_override: optional total gross notional across ALL
            positions (incl. symbols not in `pending`). When omitted, the
            function sums |current_size|×mid across entries in `pending` only.

    Returns:
        Scale ∈ [0, 1] to apply to each signed delta (`new_size - current_size`).
    """
    if nav <= 0 or leverage <= 0:
        return 1.0
    if current_gross_override is not None:
        current_gross = float(current_gross_override)
    else:
        current_gross = sum(
            abs(p["current_size"]) * p["mid"] for p in pending
        )
    margin_in_use = current_gross / leverage
    free_cash = max(0.0, nav - margin_in_use)
    cost_rate = float(cost_bps) / 1e4

    margin_required = 0.0
    fee_required = 0.0
    for p in pending:
        cur = abs(p["current_size"]) * p["mid"]
        new = abs(p["new_size"]) * p["mid"]
        delta = abs(p["new_size"] - p["current_size"]) * p["mid"]
        margin_required += max(0.0, new - cur) / leverage
        fee_required += delta * cost_rate

    total = margin_required + fee_required
    if total <= 1e-12:
        return 1.0
    return max(0.0, min(1.0, free_cash / total))


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

    nav = float(ctx.nav or 0.0)
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
