from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.DeltaLabClient import DELTA_LAB_CLIENT


async def get_basis_apy_sources(basis_symbol: str, limit: str = "10") -> dict[str, Any]:
    """Get top yield opportunities for a given asset across protocols.

    Args:
        basis_symbol: Root symbol (e.g., "BTC", "ETH", "HYPE")
        limit: Max opportunities to return (default: "10", max: "1000")

    Returns:
        Dict with basis info, opportunities grouped by LONG/SHORT, summary stats
    """
    try:
        limit_int = int(limit)
        limit_int = min(1000, max(1, limit_int))  # Enforce 1-1000 range

        result = await DELTA_LAB_CLIENT.get_basis_apy_sources(
            basis_symbol=basis_symbol.upper(),
            lookback_days=7,
            limit=limit_int,
        )
        return result
    except Exception as exc:
        return {"error": str(exc)}


async def get_best_delta_neutral_pairs(basis_symbol: str, limit: str = "5") -> dict[str, Any]:
    """Get top delta-neutral pair candidates for an asset.

    Args:
        basis_symbol: Root symbol (e.g., "BTC", "ETH", "HYPE")
        limit: Max pairs to return (default: "5", max: "100")

    Returns:
        Dict with candidates sorted by net APY and Pareto frontier
    """
    try:
        limit_int = int(limit)
        limit_int = min(100, max(1, limit_int))  # Enforce 1-100 range

        result = await DELTA_LAB_CLIENT.get_best_delta_neutral_pairs(
            basis_symbol=basis_symbol.upper(),
            lookback_days=7,
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
