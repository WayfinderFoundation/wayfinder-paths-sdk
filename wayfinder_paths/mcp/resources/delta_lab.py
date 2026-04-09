from __future__ import annotations

import logging
from typing import Any

from wayfinder_paths.core.clients.DeltaLabClient import DELTA_LAB_CLIENT
from wayfinder_paths.core.constants.chains import CHAIN_CODE_TO_ID

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


async def get_basis_apy_sources(
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
    try:
        lookback_int = int(lookback_days)
        lookback_int = max(1, lookback_int)  # Enforce min 1 day
        limit_int = int(limit)
        limit_int = min(1000, max(1, limit_int))  # Enforce 1-1000 range

        resolved = await _resolve_basis_symbol(basis_symbol.upper())
        result = await DELTA_LAB_CLIENT.get_basis_apy_sources(
            basis_symbol=resolved,
            lookback_days=lookback_int,
            limit=limit_int,
        )
        return result
    except Exception as exc:
        return {"error": str(exc)}


async def get_best_delta_neutral_pairs(
    basis_symbol: str, lookback_days: str = "7", limit: str = "5"
) -> dict[str, Any]:
    """Get top delta-neutral pair candidates for an asset.

    Args:
        basis_symbol: Root symbol (e.g., "BTC", "ETH", "HYPE")
        lookback_days: Days to look back for averaging (default: "7", min: "1")
        limit: Max pairs to return (default: "5", max: "100")

    Returns:
        Dict with candidates sorted by net APY and Pareto frontier
    """
    try:
        lookback_int = int(lookback_days)
        lookback_int = max(1, lookback_int)  # Enforce min 1 day
        limit_int = int(limit)
        limit_int = min(100, max(1, limit_int))  # Enforce 1-100 range

        resolved = await _resolve_basis_symbol(basis_symbol.upper())
        result = await DELTA_LAB_CLIENT.get_best_delta_neutral_pairs(
            basis_symbol=resolved,
            lookback_days=lookback_int,
            limit=limit_int,
        )
        return result
    except Exception as exc:
        return {"error": str(exc)}


async def get_delta_lab_asset(asset_id: str) -> dict[str, Any]:
    """Look up asset metadata by internal asset_id.

    Args:
        asset_id: Internal asset ID

    Returns:
        Dict with symbol, name, decimals, chain_id, address, coingecko_id
    """
    try:
        result = await DELTA_LAB_CLIENT.get_asset(asset_id=int(asset_id))
        return result
    except Exception as exc:
        return {"error": str(exc)}


async def get_basis_symbols() -> dict[str, Any]:
    """Get list of available basis symbols.

    Returns all available basis symbols in Delta Lab.

    Returns:
        Dict with symbols list and total count
    """
    try:
        # Get all symbols (no limit) for MCP access
        result = await DELTA_LAB_CLIENT.get_basis_symbols(get_all=True)
        return result
    except Exception as exc:
        return {"error": str(exc)}


async def get_assets_by_address(address: str, chain_id: str = "all") -> dict[str, Any]:
    """Get assets by contract address.

    Args:
        address: Contract address to search for
        chain_id: Optional chain filter (chain ID like "8453" or chain code like "base").
                 Use "all" for no filter.

    Returns:
        Dict with assets list
    """
    try:
        chain_id_param = None
        chain_value = chain_id.strip().lower()
        if chain_value not in ("all", "_"):
            if chain_value.isdigit():
                chain_id_param = int(chain_value)
            else:
                chain_id_param = CHAIN_CODE_TO_ID.get(chain_value)
                if chain_id_param is None:
                    return {"error": f"unknown chain filter: {chain_id!r}"}
        result = await DELTA_LAB_CLIENT.get_assets_by_address(
            address=address,
            chain_id=chain_id_param,
        )
        return result
    except Exception as exc:
        return {"error": str(exc)}


async def get_asset_basis_info(symbol: str) -> dict[str, Any]:
    """Get basis group information for an asset.

    Args:
        symbol: Asset symbol (e.g., "ETH", "BTC")

    Returns:
        Dict with asset_id, symbol, and basis group information
    """
    try:
        result = await DELTA_LAB_CLIENT.get_asset_basis(symbol=symbol.upper())
        return result
    except Exception as exc:
        return {"error": str(exc)}


async def search_delta_lab_assets(
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
    try:
        chain_id_param = None
        chain_value = chain.strip().lower()
        if chain_value not in ("all", "_"):
            if chain_value.isdigit():
                chain_id_param = int(chain_value)
            else:
                chain_id_param = CHAIN_CODE_TO_ID.get(chain_value)
                if chain_id_param is None:
                    return {"error": f"unknown chain filter: {chain!r}"}
        limit_int = int(limit)
        return await DELTA_LAB_CLIENT.search_assets(
            query=query.strip(),
            chain_id=chain_id_param,
            limit=limit_int,
        )
    except Exception as exc:
        return {"error": str(exc)}


async def get_top_apy(lookback_days: str = "7", limit: str = "50") -> dict[str, Any]:
    """Get top APY opportunities across all basis symbols.

    Returns top N LONG opportunities by APY across all protocols: perps,
    Pendle PTs, Boros IRS, yield-bearing tokens, and lending.

    Args:
        lookback_days: Days to average over (default: "7", min: "1")
        limit: Max opportunities to return (default: "50", max: "500")

    Returns:
        Dict with top opportunities sorted by APY
    """
    try:
        lookback_int = int(lookback_days)
        lookback_int = max(1, lookback_int)  # Enforce min 1 day
        limit_int = int(limit)
        limit_int = min(500, max(1, limit_int))  # Enforce 1-500 range

        result = await DELTA_LAB_CLIENT.get_top_apy(
            lookback_days=lookback_int,
            limit=limit_int,
        )
        return result
    except Exception as exc:
        return {"error": str(exc)}


async def _get_asset_timeseries_impl(
    symbol: str,
    series: str,
    lookback_days: str,
    limit: str,
    venue: str = "_",
    basis: bool = False,
) -> dict[str, Any]:
    """Shared implementation for timeseries MCP resources."""
    try:
        lookback_int = int(lookback_days)
        limit_int = int(limit)
        limit_int = min(10000, max(1, limit_int))  # Enforce 1-10000 range
        series_param = series if series else None  # Empty string -> None (all series)
        venue_param = venue.strip() if venue.strip() not in ("_", "") else None

        dataframes = await DELTA_LAB_CLIENT.get_asset_timeseries(
            symbol=symbol.upper(),
            lookback_days=lookback_int,
            limit=limit_int,
            series=series_param,
            venue=venue_param,
            basis=basis,
        )

        result: dict[str, Any] = {}
        for series_name, df in dataframes.items():
            df_reset = df.reset_index()
            result[series_name] = df_reset.to_dict("records")

        return result
    except Exception as exc:
        return {"error": str(exc)}


async def get_asset_timeseries_data(
    symbol: str,
    series: str = "price",
    lookback_days: str = "7",
    limit: str = "100",
) -> dict[str, Any]:
    """Get timeseries data for an asset (exact symbol, no basis expansion).

    Args:
        symbol: Asset symbol (e.g., "USDC", "ETH")
        series: Data series - "price" (default), "funding", "lending", "rates", etc.
        lookback_days: Number of days to look back (default: "7")
        limit: Maximum data points per series (default: "100", max: "10000")
    """
    return await _get_asset_timeseries_impl(symbol, series, lookback_days, limit)


async def get_asset_timeseries_with_venue(
    symbol: str,
    series: str,
    lookback_days: str,
    limit: str,
    venue: str,
) -> dict[str, Any]:
    """Get timeseries data for an asset filtered by venue (exact symbol, no basis expansion).

    Args:
        symbol: Asset symbol (e.g., "USDC", "ETH")
        series: Data series - "price", "funding", "lending", "rates", etc.
        lookback_days: Number of days to look back
        limit: Maximum data points per series (max: "10000")
        venue: Venue name prefix (e.g. "moonwell", "hyperliquid"). Use "_" for no filter.
    """
    return await _get_asset_timeseries_impl(
        symbol, series, lookback_days, limit, venue=venue
    )


async def get_basis_timeseries_data(
    symbol: str,
    series: str,
    lookback_days: str,
    limit: str,
) -> dict[str, Any]:
    """Get timeseries data expanded to all basis group members.

    Use this when you want data for all related assets — e.g. "USDC" returns
    USDC + sUSDC + aUSDC etc., "ETH" returns ETH + wstETH + cbETH etc.

    Args:
        symbol: Basis symbol (e.g., "USDC", "ETH")
        series: Data series - "price", "funding", "lending", "rates", etc.
        lookback_days: Number of days to look back
        limit: Maximum data points per series (max: "10000")
    """
    return await _get_asset_timeseries_impl(
        symbol, series, lookback_days, limit, basis=True
    )


async def get_basis_timeseries_with_venue(
    symbol: str,
    series: str,
    lookback_days: str,
    limit: str,
    venue: str,
) -> dict[str, Any]:
    """Get timeseries data expanded to all basis group members, filtered by venue.

    Args:
        symbol: Basis symbol (e.g., "USDC", "ETH")
        series: Data series - "price", "funding", "lending", "rates", etc.
        lookback_days: Number of days to look back
        limit: Maximum data points per series (max: "10000")
        venue: Venue name prefix (e.g. "moonwell", "hyperliquid"). Use "_" for no filter.
    """
    return await _get_asset_timeseries_impl(
        symbol, series, lookback_days, limit, venue=venue, basis=True
    )


async def screen_price(
    sort: str = "price_usd",
    limit: str = "100",
    basis: str = "all",
) -> dict[str, Any]:
    """Screen assets by price features (returns, volatility, drawdowns).

    Args:
        sort: Column to sort by (default: "price_usd"). Options include:
              price_usd, ret_1d, ret_7d, ret_30d, ret_90d,
              vol_7d, vol_30d, vol_90d, mdd_30d, mdd_90d
        limit: Max rows to return (default: "100", max: "1000")
        basis: Basis symbol or asset symbol to filter by (e.g. "ETH", "USDC").
               Asset symbols are auto-resolved to their root basis (USDC -> USD).
               Use "all" for no filter.

    Returns:
        Dict with data (list of price feature rows) and count
    """
    try:
        limit_int = min(1000, max(1, int(limit)))
        basis_param = None
        if basis.strip().lower() != "all":
            basis_param = await _resolve_basis_symbol(basis.strip().upper())
        result = await DELTA_LAB_CLIENT.screen_price(
            sort=sort.strip(),
            limit=limit_int,
            basis=basis_param,
        )
        return result
    except Exception as exc:
        return {"error": str(exc)}


async def screen_price_by_asset_ids(
    sort: str = "price_usd",
    limit: str = "100",
    asset_ids: str = "all",
) -> dict[str, Any]:
    """Screen assets by price features for specific asset IDs.

    Args:
        sort: Column to sort by (default: "price_usd")
        limit: Max rows to return (default: "100", max: "1000")
        asset_ids: Comma-separated asset IDs (e.g. "1,2,3") or "all"

    Returns:
        Dict with data (list of price feature rows) and count
    """
    try:
        limit_int = min(1000, max(1, int(limit)))

        asset_ids_param = None
        asset_ids_value = asset_ids.strip().lower()
        if asset_ids_value not in ("all", "_"):
            try:
                ids = [int(x.strip()) for x in asset_ids.split(",") if x.strip()]
            except ValueError:
                return {"error": f"invalid asset_ids: {asset_ids!r}"}
            if not ids:
                return {"error": "asset_ids must not be empty"}
            asset_ids_param = ids

        result = await DELTA_LAB_CLIENT.screen_price(
            sort=sort.strip(),
            limit=limit_int,
            asset_ids=asset_ids_param,
        )
        return result
    except Exception as exc:
        return {"error": str(exc)}


async def screen_lending(
    sort: str = "net_supply_apr_now",
    limit: str = "100",
    basis: str = "all",
) -> dict[str, Any]:
    """Screen lending markets by surface features (supply/borrow APRs, TVL).

    Args:
        sort: Column to sort by (default: "net_supply_apr_now"). Options include:
              net_supply_apr_now, net_supply_mean_7d, net_supply_mean_30d,
              combined_net_supply_apr_now, net_borrow_apr_now,
              supply_tvl_usd, liquidity_usd, util_now, borrow_spike_score
        limit: Max rows to return (default: "100", max: "1000")
        basis: Basis symbol or asset symbol to filter by (e.g. "ETH", "USDC").
               Asset symbols are auto-resolved to their root basis (USDC -> USD).
               Use "all" for no filter.

    Returns:
        Dict with data (list of lending surface feature rows) and count
    """
    try:
        limit_int = min(1000, max(1, int(limit)))
        basis_param = None
        if basis.strip().lower() != "all":
            basis_param = await _resolve_basis_symbol(basis.strip().upper())
        result = await DELTA_LAB_CLIENT.screen_lending(
            sort=sort.strip(),
            limit=limit_int,
            basis=basis_param,
            exclude_frozen=True,
        )
        return result
    except Exception as exc:
        return {"error": str(exc)}


async def screen_lending_by_asset_ids(
    sort: str = "net_supply_apr_now",
    limit: str = "100",
    asset_ids: str = "all",
) -> dict[str, Any]:
    """Screen lending markets by surface features for specific asset IDs.

    Args:
        sort: Column to sort by (default: "net_supply_apr_now")
        limit: Max rows to return (default: "100", max: "1000")
        asset_ids: Comma-separated asset IDs (e.g. "1,2,3") or "all"

    Returns:
        Dict with data (list of lending surface feature rows) and count
    """
    try:
        limit_int = min(1000, max(1, int(limit)))

        asset_ids_param = None
        asset_ids_value = asset_ids.strip().lower()
        if asset_ids_value not in ("all", "_"):
            try:
                ids = [int(x.strip()) for x in asset_ids.split(",") if x.strip()]
            except ValueError:
                return {"error": f"invalid asset_ids: {asset_ids!r}"}
            if not ids:
                return {"error": "asset_ids must not be empty"}
            asset_ids_param = ids

        result = await DELTA_LAB_CLIENT.screen_lending(
            sort=sort.strip(),
            limit=limit_int,
            asset_ids=asset_ids_param,
            exclude_frozen=True,
        )
        return result
    except Exception as exc:
        return {"error": str(exc)}


async def screen_perp(
    sort: str = "funding_now",
    limit: str = "100",
    basis: str = "all",
) -> dict[str, Any]:
    """Screen perpetual markets by surface features (funding, basis, OI).

    Args:
        sort: Column to sort by (default: "funding_now"). Options include:
              funding_now, funding_mean_7d, funding_mean_30d,
              basis_now, basis_mean_7d, basis_mean_30d,
              oi_now, volume_24h, mark_price
        limit: Max rows to return (default: "100", max: "1000")
        basis: Basis symbol or asset symbol to filter by (e.g. "ETH", "USDC").
               Asset symbols are auto-resolved to their root basis (USDC -> USD).
               Use "all" for no filter.

    Returns:
        Dict with data (list of perp surface feature rows) and count
    """
    try:
        limit_int = min(1000, max(1, int(limit)))
        basis_param = None
        if basis.strip().lower() != "all":
            basis_param = await _resolve_basis_symbol(basis.strip().upper())
        result = await DELTA_LAB_CLIENT.screen_perp(
            sort=sort.strip(),
            limit=limit_int,
            basis=basis_param,
        )
        return result
    except Exception as exc:
        return {"error": str(exc)}


async def screen_perp_by_asset_ids(
    sort: str = "funding_now",
    limit: str = "100",
    asset_ids: str = "all",
) -> dict[str, Any]:
    """Screen perpetual markets by surface features for specific base asset IDs.

    Args:
        sort: Column to sort by (default: "funding_now")
        limit: Max rows to return (default: "100", max: "1000")
        asset_ids: Comma-separated base asset IDs (e.g. "1,2,3") or "all"

    Returns:
        Dict with data (list of perp surface feature rows) and count
    """
    try:
        limit_int = min(1000, max(1, int(limit)))

        asset_ids_param = None
        asset_ids_value = asset_ids.strip().lower()
        if asset_ids_value not in ("all", "_"):
            try:
                ids = [int(x.strip()) for x in asset_ids.split(",") if x.strip()]
            except ValueError:
                return {"error": f"invalid asset_ids: {asset_ids!r}"}
            if not ids:
                return {"error": "asset_ids must not be empty"}
            asset_ids_param = ids

        result = await DELTA_LAB_CLIENT.screen_perp(
            sort=sort.strip(),
            limit=limit_int,
            asset_ids=asset_ids_param,
        )
        return result
    except Exception as exc:
        return {"error": str(exc)}


async def screen_borrow_routes(
    sort: str = "ltv_max",
    limit: str = "100",
    basis: str = "all",
    borrow_basis: str = "all",
    chain_id: str = "all",
) -> dict[str, Any]:
    """Screen borrow routes (collateral → borrow) by route configuration.

    Args:
        sort: Column to sort by (default: "ltv_max"). Options include:
              ltv_max, liq_threshold, liquidation_penalty, debt_ceiling_usd,
              venue_name, market_label, created_at
        limit: Max rows to return (default: "100", max: "1000")
        basis: Collateral basis symbol to filter by (e.g. "ETH"). Use "all" for no filter.
        borrow_basis: Borrow basis symbol to filter by (e.g. "USD"). Use "all" for no filter.
        chain_id: Optional chain filter (chain ID like "8453" or chain code like "base").
                 Use "all" for no filter.

    Returns:
        Dict with data (list of borrow route rows) and count
    """
    try:
        limit_int = min(1000, max(1, int(limit)))
        basis_param = None
        if basis.strip().lower() != "all":
            basis_param = await _resolve_basis_symbol(basis.strip().upper())
        borrow_basis_param = None
        if borrow_basis.strip().lower() != "all":
            borrow_basis_param = await _resolve_basis_symbol(
                borrow_basis.strip().upper()
            )
        chain_id_param = None
        chain_value = chain_id.strip().lower()
        if chain_value not in ("all", "_"):
            if chain_value.isdigit():
                chain_id_param = int(chain_value)
            else:
                chain_id_param = CHAIN_CODE_TO_ID.get(chain_value)
                if chain_id_param is None:
                    return {"error": f"unknown chain filter: {chain_id!r}"}
        result = await DELTA_LAB_CLIENT.screen_borrow_routes(
            sort=sort.strip(),
            limit=limit_int,
            basis=basis_param,
            borrow_basis=borrow_basis_param,
            chain_id=chain_id_param,
        )
        return result
    except Exception as exc:
        return {"error": str(exc)}


async def screen_borrow_routes_by_asset_ids(
    sort: str = "ltv_max",
    limit: str = "100",
    asset_ids: str = "all",
    borrow_asset_ids: str = "all",
    chain_id: str = "all",
) -> dict[str, Any]:
    """Screen borrow routes by exact collateral/borrow asset IDs.

    Args:
        sort: Column to sort by (default: "ltv_max")
        limit: Max rows to return (default: "100", max: "1000")
        asset_ids: Comma-separated collateral asset IDs (e.g. "1,2,3") or "all"
        borrow_asset_ids: Comma-separated borrow asset IDs (e.g. "3,4") or "all"
        chain_id: Optional chain filter (chain ID like "8453" or chain code like "base").
                 Use "all" for no filter.

    Returns:
        Dict with data (list of borrow route rows) and count
    """
    try:
        limit_int = min(1000, max(1, int(limit)))

        asset_ids_param = None
        asset_ids_value = asset_ids.strip().lower()
        if asset_ids_value not in ("all", "_"):
            try:
                ids = [int(x.strip()) for x in asset_ids.split(",") if x.strip()]
            except ValueError:
                return {"error": f"invalid asset_ids: {asset_ids!r}"}
            if not ids:
                return {"error": "asset_ids must not be empty"}
            asset_ids_param = ids

        borrow_asset_ids_param = None
        borrow_asset_ids_value = borrow_asset_ids.strip().lower()
        if borrow_asset_ids_value not in ("all", "_"):
            try:
                ids = [int(x.strip()) for x in borrow_asset_ids.split(",") if x.strip()]
            except ValueError:
                return {"error": f"invalid borrow_asset_ids: {borrow_asset_ids!r}"}
            if not ids:
                return {"error": "borrow_asset_ids must not be empty"}
            borrow_asset_ids_param = ids

        chain_id_param = None
        chain_value = chain_id.strip().lower()
        if chain_value not in ("all", "_"):
            if chain_value.isdigit():
                chain_id_param = int(chain_value)
            else:
                chain_id_param = CHAIN_CODE_TO_ID.get(chain_value)
                if chain_id_param is None:
                    return {"error": f"unknown chain filter: {chain_id!r}"}

        result = await DELTA_LAB_CLIENT.screen_borrow_routes(
            sort=sort.strip(),
            limit=limit_int,
            asset_ids=asset_ids_param,
            borrow_asset_ids=borrow_asset_ids_param,
            chain_id=chain_id_param,
        )
        return result
    except Exception as exc:
        return {"error": str(exc)}
