from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.DeltaLabClient import DELTA_LAB_CLIENT


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

        result = await DELTA_LAB_CLIENT.get_basis_apy_sources(
            basis_symbol=basis_symbol.upper(),
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

        result = await DELTA_LAB_CLIENT.get_best_delta_neutral_pairs(
            basis_symbol=basis_symbol.upper(),
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


async def get_assets_by_address(address: str) -> dict[str, Any]:
    """Get assets by contract address.

    Args:
        address: Contract address to search for

    Returns:
        Dict with assets list (all chains)

    Note:
        This MCP resource returns assets from all chains.
        To filter by chain_id, use the DeltaLabClient directly.
    """
    try:
        result = await DELTA_LAB_CLIENT.get_assets_by_address(
            address=address,
            chain_id=None,  # Return all chains for MCP resource
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


async def get_asset_timeseries_data(
    symbol: str,
    series: str = "price",
    lookback_days: str = "7",
    limit: str = "100",
) -> dict[str, Any]:
    """Get timeseries data for an asset (MCP: quick snapshots only).

    MCP defaults prioritize SHORT, interpretable results. For longer time ranges,
    multi-venue lending data, or DataFrame-based analysis, use the client directly:
    DELTA_LAB_CLIENT.get_asset_timeseries() (see /using-delta-lab skill).

    Args:
        symbol: Asset symbol (e.g., "ETH", "BTC")
        series: Data series - "price" (default), "funding", "lending", "rates", etc.
               Empty string = all series (can be large!)
        lookback_days: Number of days to look back (default: "7" for quick snapshot)
        limit: Maximum number of data points per series (default: "100", max: "10000")

    Returns:
        Dict with series data as JSON arrays (use client for DataFrames)
    """
    try:
        lookback_int = int(lookback_days)
        limit_int = int(limit)
        limit_int = min(10000, max(1, limit_int))  # Enforce 1-10000 range
        series_param = series if series else None  # Empty string -> None (all series)

        # Get DataFrames from client
        dataframes = await DELTA_LAB_CLIENT.get_asset_timeseries(
            symbol=symbol.upper(),
            lookback_days=lookback_int,
            limit=limit_int,
            series=series_param,
        )

        # Convert DataFrames back to JSON arrays for MCP
        result: dict[str, Any] = {}
        for series_name, df in dataframes.items():
            # Reset index to make ts a column again
            df_reset = df.reset_index()
            # Convert to list of dicts
            result[series_name] = df_reset.to_dict("records")

        return result
    except Exception as exc:
        return {"error": str(exc)}
