from __future__ import annotations

from typing import Any, Literal

from wayfinder_paths.mcp.utils import catch_errors, ok
from wayfinder_paths.quant.fractal_scan_context import create_fractal_scan_request
from wayfinder_paths.quant.fractal_scan_pipeline import run_fractal_scan


@catch_errors
async def quant_fractal_scan(
    scope: Literal["same_market", "adaptive", "broad"] = "same_market",
    scan_id: str | None = None,
    kind: Literal["hyperliquid", "onchain"] | None = None,
    interval: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    display_symbol: str | None = None,
    market_id: str | None = None,
    chart_id: str | None = None,
    selected_price_min: float | None = None,
    selected_price_max: float | None = None,
    hl_coin: str | None = None,
    chain_id: int | None = None,
    token_address: str | None = None,
    include_views: bool = True,
) -> dict[str, Any]:
    """Run or expand a deterministic historical analogue scan.

    Start with ``scope="same_market"`` and the exact chart context. If that
    result has weak same-market coverage, call again with its ``scan_id`` and
    ``scope="adaptive"``. Use ``broad`` only when clearly labelled proxy and
    cross-market evidence would help. Follow-up calls reuse the hydrated scan.

    Initial calls require kind, interval, start_ms, end_ms, display_symbol,
    market_id, and chart_id. Hyperliquid scans also require hl_coin; onchain
    scans require chain_id and an exact token_address. Follow-ups need only
    scan_id, scope, and optionally include_views.
    """
    request = None
    if scan_id is None:
        required = {
            "kind": kind,
            "interval": interval,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "display_symbol": display_symbol,
            "market_id": market_id,
            "chart_id": chart_id,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise ValueError(f"Initial scan missing: {', '.join(missing)}")
        request = create_fractal_scan_request(
            kind=str(kind),
            interval=str(interval),
            start_ms=int(start_ms),
            end_ms=int(end_ms),
            display_symbol=str(display_symbol),
            market_id=str(market_id),
            chart_id=str(chart_id),
            selected_price_min=selected_price_min,
            selected_price_max=selected_price_max,
            hl_coin=hl_coin,
            chain_id=chain_id,
            token_address=token_address,
        )
    result = await run_fractal_scan(
        scope=scope,
        request=request,
        scan_id=scan_id,
        include_views=include_views,
    )
    return ok(result)
