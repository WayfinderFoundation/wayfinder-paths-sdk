from __future__ import annotations

import logging
from typing import Any

from wayfinder_paths.core.clients.DeltaLabClient import DELTA_LAB_CLIENT
from wayfinder_paths.core.constants.chains import CHAIN_CODE_TO_ID
from wayfinder_paths.mcp.utils import catch_errors, ok

logger = logging.getLogger(__name__)


async def _resolve_basis_symbol(symbol: str) -> str:
    """Resolve an asset symbol to its root basis symbol.

    E.g. "USDC" -> "USD", "wstETH" -> "ETH". Returns the input unchanged
    if it's already a root basis symbol or if resolution fails.
    """
    try:
        result = await DELTA_LAB_CLIENT.get_asset_basis(symbol=symbol)
        basis = result.get("basis")
        if basis and basis.get("root_symbol"):
            root = basis["root_symbol"]
            if root != symbol:
                logger.debug("Resolved basis symbol %s -> %s", symbol, root)
            return root
    except Exception:
        pass
    return symbol


@catch_errors
async def research_get_basis_apy_sources(
    basis_symbol: str, lookback_days: str = "7", limit: str = "10"
) -> dict[str, Any]:
    """Get top yield opportunities for a given asset across protocols.

    Args:
        basis_symbol: Root symbol (e.g., "BTC", "ETH", "HYPE")
        lookback_days: Days to look back for averaging (default: "7", min: "1")
        limit: Max opportunities to return (default: "10", max: "1000")

    Returns:
        Dict with basis info, opportunities grouped by LONG/SHORT, summary stats
    """
    lookback_int = max(1, int(lookback_days))
    limit_int = min(1000, max(1, int(limit)))
    resolved = await _resolve_basis_symbol(basis_symbol.upper())
    return ok(
        await DELTA_LAB_CLIENT.get_basis_apy_sources(
            basis_symbol=resolved,
            lookback_days=lookback_int,
            limit=limit_int,
        )
    )


@catch_errors
async def research_get_basis_symbols() -> dict[str, Any]:
    """Get list of available basis symbols.

    Returns all available basis symbols in Delta Lab.

    Returns:
        Dict with symbols list and total count
    """
    return ok(await DELTA_LAB_CLIENT.get_basis_symbols(get_all=True))


@catch_errors
async def research_get_asset_basis_info(symbol: str) -> dict[str, Any]:
    """Get basis group information for an asset.

    Args:
        symbol: Asset symbol (e.g., "ETH", "BTC")

    Returns:
        Dict with asset_id, symbol, and basis group information
    """
    return ok(await DELTA_LAB_CLIENT.get_asset_basis(symbol=symbol.upper()))


@catch_errors
async def research_search_delta_lab_assets(
    query: str, chain: str = "all", limit: str = "25"
) -> dict[str, Any]:
    """Search Delta Lab assets by symbol/name/address/coingecko_id.

    Args:
        query: Search term (symbol, name, address, coingecko_id, or numeric asset_id)
        chain: Optional chain filter (chain ID like "8453" or chain code like "base").
               Use "all" for no filter.
        limit: Max results (default: "25", max: "200")

    Returns:
        Dict with "assets" list and "total_count"
    """
    chain_id_param: int | None = None
    chain_value = chain.strip().lower()
    if chain_value not in ("all", "_"):
        if chain_value.isdigit():
            chain_id_param = int(chain_value)
        else:
            chain_id_param = CHAIN_CODE_TO_ID.get(chain_value)
            if chain_id_param is None:
                raise ValueError(f"unknown chain filter: {chain!r}")
    return ok(
        await DELTA_LAB_CLIENT.search_assets(
            query=query.strip(),
            chain_id=chain_id_param,
            limit=int(limit),
        )
    )


@catch_errors
async def research_get_top_apy(
    lookback_days: str = "7", limit: str = "25"
) -> dict[str, Any]:
    """Get top APY opportunities across all basis symbols.

    Returns top N LONG opportunities by APY across all protocols: perps,
    Pendle PTs, Boros IRS, yield-bearing tokens, and lending.

    Args:
        lookback_days: Days to average over (default: "7", min: "1")
        limit: Max opportunities to return (default: "25", max: "500").
               Prefer the default for exploratory scans; raise only after
               narrowing.

    Returns:
        Dict with top opportunities sorted by APY
    """
    lookback_int = max(1, int(lookback_days))
    limit_int = min(500, max(1, int(limit)))
    return ok(
        await DELTA_LAB_CLIENT.get_top_apy(
            lookback_days=lookback_int,
            limit=limit_int,
        )
    )


@catch_errors
async def research_search_price(
    sort: str = "price_usd",
    limit: str = "25",
    basis: str = "all",
) -> dict[str, Any]:
    """Screen assets by price features (returns, volatility, drawdowns).

    Args:
        sort: Column to sort by (default: "price_usd"). Options include:
              price_usd, ret_1d, ret_7d, ret_30d, ret_90d,
              vol_7d, vol_30d, vol_90d, mdd_30d, mdd_90d
        limit: Max rows to return (default: "25", max: "1000"). Prefer the
              default for exploratory scans; raise only after narrowing by
              `basis` or another filter.
        basis: Basis symbol or asset symbol to filter by (e.g. "ETH", "USDC").
               Asset symbols are auto-resolved to their root basis (USDC -> USD).
               Use "all" for no filter.

    Returns:
        Dict with data (list of price feature rows) and count
    """
    limit_int = min(1000, max(1, int(limit)))
    basis_param = None
    if basis.strip().lower() != "all":
        basis_param = await _resolve_basis_symbol(basis.strip().upper())
    return ok(
        await DELTA_LAB_CLIENT.screen_price(
            sort=sort.strip(),
            limit=limit_int,
            basis=basis_param,
        )
    )


@catch_errors
async def research_search_lending(
    sort: str = "net_supply_apr_now",
    limit: str = "25",
    basis: str = "all",
) -> dict[str, Any]:
    """Screen lending markets by surface features (supply/borrow APRs, TVL).

    Args:
        sort: Column to sort by (default: "net_supply_apr_now"). Options include:
              net_supply_apr_now, net_supply_mean_7d, net_supply_mean_30d,
              combined_net_supply_apr_now, net_borrow_apr_now,
              supply_tvl_usd, liquidity_usd, util_now, borrow_spike_score
        limit: Max rows to return (default: "25", max: "1000"). Prefer the
              default for exploratory scans; raise only after narrowing by
              `basis` or another filter.
        basis: Basis symbol or asset symbol to filter by (e.g. "ETH", "USDC").
               Asset symbols are auto-resolved to their root basis (USDC -> USD).
               Use "all" for no filter.

    Returns:
        Dict with data (list of lending surface feature rows) and count
    """
    limit_int = min(1000, max(1, int(limit)))
    basis_param = None
    if basis.strip().lower() != "all":
        basis_param = await _resolve_basis_symbol(basis.strip().upper())
    return ok(
        await DELTA_LAB_CLIENT.screen_lending(
            sort=sort.strip(),
            limit=limit_int,
            basis=basis_param,
            exclude_frozen=True,
        )
    )


@catch_errors
async def research_search_perp(
    sort: str = "funding_now",
    limit: str = "25",
    basis: str = "all",
) -> dict[str, Any]:
    """Screen perpetual markets by surface features (funding, basis, OI).

    Args:
        sort: Column to sort by (default: "funding_now"). Options include:
              funding_now, funding_mean_7d, funding_mean_30d,
              basis_now, basis_mean_7d, basis_mean_30d,
              oi_now, volume_24h, mark_price
        limit: Max rows to return (default: "25", max: "1000"). Prefer the
              default for exploratory scans; raise only after narrowing by
              `basis` or another filter.
        basis: Basis symbol or asset symbol to filter by (e.g. "ETH", "USDC").
               Asset symbols are auto-resolved to their root basis (USDC -> USD).
               Use "all" for no filter.

    Returns:
        Dict with data (list of perp surface feature rows) and count
    """
    limit_int = min(1000, max(1, int(limit)))
    basis_param = None
    if basis.strip().lower() != "all":
        basis_param = await _resolve_basis_symbol(basis.strip().upper())
    return ok(
        await DELTA_LAB_CLIENT.screen_perp(
            sort=sort.strip(),
            limit=limit_int,
            basis=basis_param,
        )
    )


@catch_errors
async def research_search_borrow_routes(
    sort: str = "ltv_max",
    limit: str = "25",
    basis: str = "all",
    borrow_basis: str = "all",
    chain_id: str = "all",
) -> dict[str, Any]:
    """Screen borrow routes (collateral → borrow) by route configuration.

    Args:
        sort: Column to sort by (default: "ltv_max"). Options include:
              ltv_max, liq_threshold, liquidation_penalty, debt_ceiling_usd,
              venue_name, market_label, created_at
        limit: Max rows to return (default: "25", max: "1000"). Prefer the
              default for exploratory scans; raise only after narrowing by
              `basis`, `borrow_basis`, or `chain_id`.
        basis: Collateral basis symbol to filter by (e.g. "ETH"). Use "all" for no filter.
        borrow_basis: Borrow basis symbol to filter by (e.g. "USD"). Use "all" for no filter.
        chain_id: Optional chain filter (chain ID like "8453" or chain code like "base").
                 Use "all" for no filter.

    Returns:
        Dict with data (list of borrow route rows) and count
    """
    limit_int = min(1000, max(1, int(limit)))
    basis_param = None
    if basis.strip().lower() != "all":
        basis_param = await _resolve_basis_symbol(basis.strip().upper())
    borrow_basis_param = None
    if borrow_basis.strip().lower() != "all":
        borrow_basis_param = await _resolve_basis_symbol(borrow_basis.strip().upper())
    chain_id_param: int | None = None
    chain_value = chain_id.strip().lower()
    if chain_value not in ("all", "_"):
        if chain_value.isdigit():
            chain_id_param = int(chain_value)
        else:
            chain_id_param = CHAIN_CODE_TO_ID.get(chain_value)
            if chain_id_param is None:
                raise ValueError(f"unknown chain filter: {chain_id!r}")
    return ok(
        await DELTA_LAB_CLIENT.screen_borrow_routes(
            sort=sort.strip(),
            limit=limit_int,
            basis=basis_param,
            borrow_basis=borrow_basis_param,
            chain_id=chain_id_param,
        )
    )
