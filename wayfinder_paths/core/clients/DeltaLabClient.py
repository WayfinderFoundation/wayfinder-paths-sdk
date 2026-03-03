from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

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

    async def get_assets_by_address(
        self,
        *,
        address: str,
        chain_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Get assets by contract address.

        Args:
            address: Contract address to search for
            chain_id: Optional chain ID to filter results

        Returns:
            Response with assets list:
            {
                "assets": [{"asset_id": 1, "symbol": "WETH", ...}, ...]
            }

        Raises:
            httpx.HTTPStatusError: For 400 (invalid params) or 500 (server error)
        """
        url = f"{get_api_base_url()}/delta-lab/assets/by-address"
        params: dict[str, str | int] = {"address": address}
        if chain_id is not None:
            params["chain_id"] = chain_id
        response = await self._authed_request("GET", url, params=params)
        return response.json()

    async def get_asset_basis(self, *, symbol: str) -> dict[str, Any]:
        """
        Get basis group information for an asset.

        Args:
            symbol: Asset symbol (e.g., "ETH", "BTC")

        Returns:
            AssetBasisResponse with basis group information:
            {
                "asset_id": 1,
                "symbol": "ETH",
                "basis": {
                    "basis_group_id": 1,
                    "root_asset_id": 1,
                    "root_symbol": "ETH",
                    "role": "ROOT"
                } or None if not in a basis group
            }

        Raises:
            httpx.HTTPStatusError: For 404 (not found) or 500 (server error)
        """
        url = f"{get_api_base_url()}/delta-lab/assets/{symbol}/basis"
        response = await self._authed_request("GET", url)
        return response.json()

    async def get_asset_timeseries(
        self,
        *,
        symbol: str,
        lookback_days: int = 30,
        limit: int = 500,
        as_of: datetime | None = None,
        series: str | None = None,
    ) -> dict[str, pd.DataFrame]:
        """
        Get timeseries data for an asset.

        Args:
            symbol: Asset symbol (e.g., "ETH", "BTC")
            lookback_days: Number of days to look back (default: 30)
            limit: Maximum number of data points per series (default: 500, max: 10000)
            as_of: Query timestamp (default: now)
            series: Comma-separated list of series to fetch (price, yield, lending,
                   funding, pendle, boros) or alias "rates" for all rate series.
                   If None, returns all series.

        Returns:
            Dict mapping series names to DataFrames:
            {
                "price": DataFrame(columns=[price_usd], index=DatetimeIndex),
                "lending": DataFrame(columns=[market_id, venue, supply_apr, ...], index=DatetimeIndex),
                ...
            }
            Each DataFrame has 'ts' as the index (DatetimeIndex).
            Note: The backend returns 'yield_' but we normalize it to 'yield' in the returned dict.

        Raises:
            httpx.HTTPStatusError: For 400 (invalid params), 404 (not found), or 500 (server error)
        """
        url = f"{get_api_base_url()}/delta-lab/assets/{symbol}/timeseries"
        params: dict[str, str | int] = {
            "lookback_days": lookback_days,
            "limit": limit,
        }
        if as_of:
            params["as_of"] = as_of.isoformat()
        if series is not None:
            params["series"] = series

        response = await self._authed_request("GET", url, params=params)
        data = response.json()

        # Convert each series to DataFrame
        result: dict[str, pd.DataFrame] = {}
        for key, records in data.items():
            # Skip non-series keys (asset_id, symbol)
            if key in ("asset_id", "symbol"):
                continue
            # Handle yield_ -> yield normalization
            normalized_key = "yield" if key == "yield_" else key
            # Convert to DataFrame if we have data
            if records and isinstance(records, list):
                df = pd.DataFrame(records)
                # Convert ts to datetime and set as index
                if "ts" in df.columns:
                    df["ts"] = pd.to_datetime(df["ts"])
                    df.set_index("ts", inplace=True)
                result[normalized_key] = df

        return result

    async def get_top_apy(
        self,
        *,
        limit: int = 50,
        lookback_days: int = 7,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Get top APY opportunities across all basis symbols.

        Returns top N LONG opportunities by APY across all protocols and venues:
        perps, Pendle PTs, Boros IRS, yield-bearing tokens, and lending.

        Args:
            limit: Maximum number of opportunities (default: 50, max: 500)
            lookback_days: Number of days to look back (default: 7, min: 1)
            as_of: Query timestamp (default: now)

        Returns:
            TopApyResponse with opportunities sorted by APY:
            {
                "opportunities": [...],  # Top N opportunities sorted by APY
                "as_of": "2024-02-20T...",
                "lookback_days": 7,
                "total_count": 50
            }

        Raises:
            httpx.HTTPStatusError: For 400 (invalid params) or 500 (server error)
        """
        url = f"{get_api_base_url()}/delta-lab/top-apy"
        params: dict[str, str | int] = {
            "limit": limit,
            "lookback_days": lookback_days,
        }
        if as_of:
            params["as_of"] = as_of.isoformat()
        response = await self._authed_request("GET", url, params=params)
        return response.json()


DELTA_LAB_CLIENT = DeltaLabClient()
