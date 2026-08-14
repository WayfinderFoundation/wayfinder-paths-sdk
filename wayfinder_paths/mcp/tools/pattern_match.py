"""Pattern Match MCP surface over the historical-analogue engine."""

from __future__ import annotations

from typing import Any, Literal

from wayfinder_paths.mcp.utils import catch_errors, ok
from wayfinder_paths.quant.pattern_match_context import create_pattern_match_request
from wayfinder_paths.quant.pattern_match_pipeline import (
    compact_pattern_match_result,
    run_pattern_match,
    run_pattern_match_ccxt_proxy,
)


@catch_errors
async def quant_pattern_match(
    kind: Literal["hyperliquid", "onchain"],
    interval: str,
    start_ms: int,
    end_ms: int,
    display_symbol: str,
    market_id: str,
    chart_id: str,
    request_id: str | None = None,
    selected_price_min: float | None = None,
    selected_price_max: float | None = None,
    hl_coin: str | None = None,
    chain_id: int | None = None,
    token_address: str | None = None,
) -> dict[str, Any]:
    """Run a deterministic exact-market Pattern Match analysis.

    The result is the same-market baseline. The quant agent decides whether a
    broader comparison is useful and selects any additional data itself.

    Hyperliquid requests also require hl_coin; onchain requests require chain_id and
    an exact token_address. Identical requests reuse a short-lived result cache.
    """
    request = create_pattern_match_request(
        kind=kind,
        interval=interval,
        start_ms=start_ms,
        end_ms=end_ms,
        display_symbol=display_symbol,
        market_id=market_id,
        chart_id=chart_id,
        request_id=request_id,
        selected_price_min=selected_price_min,
        selected_price_max=selected_price_max,
        hl_coin=hl_coin,
        chain_id=chain_id,
        token_address=token_address,
    )
    result = await run_pattern_match(request=request)
    return ok(compact_pattern_match_result(result))


@catch_errors
async def quant_pattern_match_ccxt_proxy(
    match_id: str,
    symbol: str,
) -> dict[str, Any]:
    """Compare a cached Pattern Match selection with a same-asset perp proxy.

    Use only after ``quant_pattern_match`` when the exact-market baseline is
    thin and the selected asset has a defensible perpetual analogue. The first
    healthy supported perp venue is used; spot is never substituted. Proxy
    evidence remains separate from the exact-market baseline.
    """
    result = await run_pattern_match_ccxt_proxy(match_id=match_id, symbol=symbol)
    return ok(compact_pattern_match_result(result))
