from __future__ import annotations

import json
from datetime import datetime

from wayfinder_paths.core.clients.DeltaLabClient import DELTA_LAB_CLIENT


async def get_basis_apy_sources(basis_symbol: str) -> str:
    """Get all yield opportunities for a given asset across protocols.

    Uses default parameters: lookback_days=7, limit=500

    Args:
        basis_symbol: Root symbol (e.g., "BTC", "ETH", "HYPE")

    Returns:
        JSON with basis info, opportunities grouped by LONG/SHORT, summary stats
    """
    try:
        result = await DELTA_LAB_CLIENT.get_basis_apy_sources(
            basis_symbol=basis_symbol.upper(),
            lookback_days=7,
            limit=500,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def get_best_delta_neutral_pairs(basis_symbol: str) -> str:
    """Get best delta-neutral pair candidates for an asset.

    Uses default parameters: lookback_days=7, limit=20

    Args:
        basis_symbol: Root symbol (e.g., "BTC", "ETH", "HYPE")

    Returns:
        JSON with candidates sorted by net APY and Pareto frontier
    """
    try:
        result = await DELTA_LAB_CLIENT.get_best_delta_neutral_pairs(
            basis_symbol=basis_symbol.upper(),
            lookback_days=7,
            limit=20,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def get_delta_lab_asset(asset_id: str) -> str:
    """Look up asset metadata by internal asset_id.

    Args:
        asset_id: Internal asset ID

    Returns:
        JSON with symbol, name, decimals, chain_id, address, coingecko_id
    """
    try:
        result = await DELTA_LAB_CLIENT.get_asset(asset_id=int(asset_id))
        return json.dumps(result, indent=2)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def get_basis_symbols() -> str:
    """Get list of available basis symbols.

    Returns all available basis symbols in Delta Lab.

    Returns:
        JSON with symbols list and total count
    """
    try:
        # Get all symbols (no limit) for MCP access
        result = await DELTA_LAB_CLIENT.get_basis_symbols(get_all=True)
        return json.dumps(result, indent=2)
    except Exception as exc:
        return json.dumps({"error": str(exc)})
