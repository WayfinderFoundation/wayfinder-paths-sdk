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
decimal, Hyperliquid only), ``ctx.positions``, ``ctx.realized_pnl``,
``ctx.state``. The validation dry run answers both reads from stub marks
(``<venue>:<symbol>`` and ``funding:<venue>:<symbol>``).
"""

from wayfinder_paths.jobs.freestyle.contract import (
    ActionResult,
    FreestyleSpec,
    normalize_action,
)

__all__ = ["ActionResult", "FreestyleSpec", "normalize_action"]
