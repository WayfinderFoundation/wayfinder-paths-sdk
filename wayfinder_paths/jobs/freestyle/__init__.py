"""Freestyle jobs: an author-written ``tick(ctx)`` that may read anything and
may trade, but only through ``ctx.act`` — the one seam paper mode intercepts.

Public surface for script authors::

    from wayfinder_paths.jobs.freestyle import FreestyleSpec

    SPEC = FreestyleSpec(venues=("polymarket", "hyperliquid"), max_loss_usd=50)

    def tick(ctx):
        odds = ctx.quote("polymarket", "polymarket:<market>:YES")
        if odds > 0.6 and "BTC" not in ctx.positions:
            ctx.act({"venue": "hyperliquid", "kind": "market", "symbol": "BTC",
                     "side": "long", "notional": 100, "max_loss": 10})

Reads: ``ctx.quote(venue, symbol)`` (latest completed-bar close),
``ctx.funding(venue, symbol)`` (a perp's latest settled hourly funding rate as a
decimal, Hyperliquid only), ``ctx.token_value(token_id, amount=1.0)`` (USD
value of an on-chain token by token id, a read only), ``ctx.defi_yield(name,
window=None)`` (a DeFi yield by feed name — ``lend_supply_apr:<venue>:<symbol>``,
``yield_apy:<symbol>``, … — as a decimal per year; with a window the trailing
mean over it), ``ctx.bars(venue, symbol, n=50, interval=None)`` (the last
``n`` completed bars, oldest first), ``ctx.positions``, ``ctx.realized_pnl``,
``ctx.state``. The validation dry run answers the reads from stub marks
(``<venue>:<symbol>``, ``funding:<venue>:<symbol>``, ``token:<token_id>``,
``yield:<name>``; bars are flat at the mark).
"""

from wayfinder_paths.jobs.freestyle.contract import (
    ActionResult,
    FreestyleSpec,
    normalize_action,
)

__all__ = ["ActionResult", "FreestyleSpec", "normalize_action"]
