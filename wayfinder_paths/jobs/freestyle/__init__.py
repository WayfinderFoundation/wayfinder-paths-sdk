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
"""

from wayfinder_paths.jobs.freestyle.contract import (
    ActionResult,
    FreestyleSpec,
    normalize_action,
)

__all__ = ["ActionResult", "FreestyleSpec", "normalize_action"]
