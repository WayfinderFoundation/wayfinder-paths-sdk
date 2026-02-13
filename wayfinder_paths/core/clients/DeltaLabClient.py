from __future__ import annotations

from datetime import datetime
from typing import Any

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url


class DeltaLabClient(WayfinderClient):
    """Client for Delta Lab basis APY and delta-neutral strategy discovery."""

    async def get_basis_apy_sources(
        self,
        *,
        basis_symbol: str,
        lookback_days: int = 7,
        limit: int = 500,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Get basis APY sources for a given symbol.

        Args:
            basis_symbol: Basis symbol (e.g., "BTC", "ETH")
            lookback_days: Number of days to look back (default: 7, min: 1)
            limit: Maximum number of opportunities (default: 500, max: 1000)
            as_of: Query timestamp (default: now)

        Returns:
            BasisApySourcesResponse with opportunities grouped by LONG/SHORT direction

        Raises:
            httpx.HTTPStatusError: For 400 (invalid params/unknown symbol) or 500 (server error)
        """
        url = f"{get_api_base_url()}/delta-lab/basis/{basis_symbol}/apy-sources"
        params: dict[str, str | int] = {
            "lookback_days": lookback_days,
            "limit": limit,
        }
        if as_of:
            params["as_of"] = as_of.isoformat()
        response = await self._authed_request("GET", url, params=params)
        return response.json()

    async def get_asset(self, *, asset_id: int) -> dict[str, Any]:
        """
        Get asset information by ID.

        Args:
            asset_id: Asset ID

        Returns:
            AssetResponse with symbol, name, decimals, chain_id, address, coingecko_id

        Raises:
            httpx.HTTPStatusError: For 404 (not found) or 500 (server error)
        """
        url = f"{get_api_base_url()}/delta-lab/assets/{asset_id}"
        response = await self._authed_request("GET", url)
        return response.json()

    async def get_basis_symbols(
        self,
        *,
        limit: int | None = None,
        get_all: bool = False,
    ) -> dict[str, Any]:
        """
        Get list of available basis symbols.

        Args:
            limit: Maximum number of symbols to return (optional)
            get_all: Set to True to return all symbols (ignores limit)

        Returns:
            Response with symbols list and total_count:
            {
                "symbols": [{"symbol": "BTC", "asset_id": 1, ...}, ...],
                "total_count": 10
            }

        Raises:
            httpx.HTTPStatusError: For 400 (invalid params) or 500 (server error)
        """
        url = f"{get_api_base_url()}/delta-lab/basis-symbols/"
        params: dict[str, str | int] = {}
        if get_all:
            params["all"] = "true"
        elif limit is not None:
            params["limit"] = limit
        response = await self._authed_request("GET", url, params=params)
        return response.json()

    async def get_best_delta_neutral_pairs(
        self,
        *,
        basis_symbol: str,
        lookback_days: int = 7,
        limit: int = 20,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Get best delta-neutral pair candidates for a given symbol.

        Args:
            basis_symbol: Basis symbol (e.g., "BTC", "ETH")
            lookback_days: Number of days to look back (default: 7, min: 1)
            limit: Maximum number of candidates (default: 20, max: 100)
            as_of: Query timestamp (default: now)

        Returns:
            BestDeltaNeutralResponse with carry/hedge legs and net APY

        Raises:
            httpx.HTTPStatusError: For 400 (invalid params/unknown symbol) or 500 (server error)
        """
        url = f"{get_api_base_url()}/delta-lab/basis/{basis_symbol}/best-delta-neutral"
        params: dict[str, str | int] = {
            "lookback_days": lookback_days,
            "limit": limit,
        }
        if as_of:
            params["as_of"] = as_of.isoformat()
        response = await self._authed_request("GET", url, params=params)
        return response.json()


DELTA_LAB_CLIENT = DeltaLabClient()
