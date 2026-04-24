from __future__ import annotations

import asyncio
import warnings
from datetime import datetime
from typing import Any

import httpx
import pandas as pd

from wayfinder_paths.core.clients.delta_lab_types import (
    AssetInfo,
    BacktestBundle,
    BorosLatest,
    DeltaLabAPIError,
    FundingLatest,
    InstrumentInfo,
    LendingLatest,
    MarketInfo,
    PendleLatest,
    PriceLatest,
    VenueInfo,
    YieldLatest,
)
from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url


def _extract_error(response: httpx.Response) -> tuple[str, str]:
    """Pull (code, message) from a Delta Lab error envelope.

    Falls back to HTTP status / response text when the body isn't the expected
    `{error, message}` shape (e.g. upstream proxy 502).
    """
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        code = body.get("error") or body.get("code") or "http_error"
        message = body.get("message") or body.get("detail") or response.reason_phrase
        return str(code), str(message)
    return "http_error", response.text[:200] or response.reason_phrase or "unknown"


class DeltaLabClient(WayfinderClient):
    """Client for Delta Lab basis APY and delta-neutral strategy discovery."""

    def _dl_url(self, path: str) -> str:
        # Delta Lab routes require a trailing slash (301 otherwise eats bodies
        # on non-redirect-following httpx clients). Normalise once here so
        # callers never need to think about it.
        base = get_api_base_url().rstrip("/")
        suffix = path if path.startswith("/") else f"/{path}"
        if not suffix.endswith("/") and "?" not in suffix:
            suffix = f"{suffix}/"
        return f"{base}/delta-lab{suffix}"

    async def _dl_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        soft_not_found: bool = False,
    ) -> Any:
        """Send a Delta Lab request, parsing the typed error envelope.

        The server returns `{"error": "<code>", "message": "..."}` with a
        mapped HTTP status for every error. This helper raises
        `DeltaLabAPIError(code, message, status, url)` so callers can branch on
        the code. When `soft_not_found=True`, a 404 with code `not_found`
        returns `None` instead (idiomatic for `*/latest/` endpoints where a
        missing snapshot is a normal state, not an exception).
        """
        url = self._dl_url(path)
        clean_params = (
            {k: v for k, v in params.items() if v is not None}
            if params is not None
            else None
        )
        try:
            response = await self._authed_request(
                method, url, params=clean_params, json=json
            )
        except httpx.HTTPStatusError as exc:
            code, message = _extract_error(exc.response)
            if soft_not_found and code == "not_found":
                return None
            raise DeltaLabAPIError(
                code, message, status=exc.response.status_code, url=url
            ) from exc
        return response.json()

    @staticmethod
    def _unwrap_items(payload: Any) -> list[dict[str, Any]]:
        """Return `items` from a `{items, count, ...}` envelope, or [] if empty."""
        if payload is None:
            return []
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            return payload["items"]
        return []

    @staticmethod
    def _to_df(rows: list[dict[str, Any]], *, ts_col: str = "ts") -> pd.DataFrame:
        """Convert a list of TS rows to a DataFrame indexed on `ts_col`."""
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        if ts_col in df.columns:
            df[ts_col] = pd.to_datetime(df[ts_col], format="ISO8601")
            df.set_index(ts_col, inplace=True)
        return df

    @staticmethod
    def _normalize_series_param(
        series: str | list[str] | tuple[str, ...] | None,
    ) -> str | None:
        if series is None:
            return None
        if isinstance(series, str):
            normalized = series.strip()
            return normalized or None

        normalized_parts = [part.strip() for part in series if str(part).strip()]
        if not normalized_parts:
            return None
        return ",".join(normalized_parts)

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

    async def search_assets(
        self,
        *,
        query: str,
        chain_id: int | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        """
        Search assets by symbol/name/address/coingecko_id.

        Args:
            query: Search term
            chain_id: Optional chain ID filter
            limit: Max results (default: 25, max: 200)

        Returns:
            {"assets": [AssetResponse, ...], "total_count": N}
        """
        url = f"{get_api_base_url()}/delta-lab/assets/search"
        params: dict[str, str | int] = {"query": query, "limit": limit}
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
        series: str | list[str] | tuple[str, ...] | None = None,
        venue: str | None = None,
        basis: bool = False,
    ) -> dict[str, pd.DataFrame]:
        """
        Get timeseries data for an asset.

        Args:
            symbol: Asset symbol (e.g., "ETH", "BTC")
            lookback_days: Number of days to look back (default: 30)
            limit: Maximum number of data points per series (default: 500, max: 10000)
            as_of: Query timestamp (default: now)
            series: Comma-separated list of series to fetch, or a list/tuple of
                   series names (price, yield, lending, funding, pendle, boros).
                   The alias "rates" requests all rate series. If None, returns
                   all series.
            venue: Venue name prefix to filter on. Applied to series that support
                   venue filtering (funding, lending, pendle, boros).
                   E.g. "hyperliquid", "moonwell". None means no filter.
            basis: Whether to expand the symbol to all basis group members for
                   lending series (default: False). Set to True to expand — e.g.
                   USDC with basis=True returns sUSDC, aUSDC etc. in addition
                   to USDC pools.

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
        normalized_series = self._normalize_series_param(series)
        if normalized_series is not None:
            params["series"] = normalized_series
        if venue is not None:
            params["venue"] = venue
        if not basis:
            params["basis"] = "false"

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
                    df["ts"] = pd.to_datetime(df["ts"], format="ISO8601")
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

    async def screen_price(
        self,
        *,
        sort: str = "price_usd",
        order: str = "desc",
        limit: int = 100,
        asset_ids: list[int] | None = None,
        basis: str | None = None,
    ) -> dict[str, Any]:
        """
        Screen assets by price features (returns, volatility, drawdowns).

        Args:
            sort: Column to sort by (default: "price_usd")
            order: "asc" or "desc" (default: "desc")
            limit: Max rows, 1-1000 (default: 100)
            asset_ids: Filter to specific asset IDs
            basis: Basis symbol to filter by (e.g. "ETH") — overrides asset_ids

        Returns:
            ScreenResponse: {"data": [ScreenPriceRow, ...], "count": N}
        """
        url = f"{get_api_base_url()}/delta-lab/screen/price"
        params: dict[str, str | int] = {
            "sort": sort,
            "order": order,
            "limit": limit,
        }
        if basis:
            params["basis"] = basis
        elif asset_ids:
            params["asset_ids"] = ",".join(str(a) for a in asset_ids)
        response = await self._authed_request("GET", url, params=params)
        return response.json()

    async def screen_lending(
        self,
        *,
        sort: str = "net_supply_apr_now",
        order: str = "desc",
        limit: int = 100,
        asset_ids: list[int] | None = None,
        basis: str | None = None,
        venue: str | None = None,
        min_tvl: float | None = None,
        exclude_frozen: bool = False,
    ) -> dict[str, Any]:
        """
        Screen lending markets by surface features (supply/borrow APRs, TVL, utilization).

        Args:
            sort: Column to sort by (default: "net_supply_apr_now")
            order: "asc" or "desc" (default: "desc")
            limit: Max rows, 1-1000 (default: 100)
            asset_ids: Filter to specific asset IDs
            basis: Basis symbol to filter by (e.g. "ETH") — overrides asset_ids
            venue: Filter by venue name (e.g. "aave", "morpho")
            min_tvl: Minimum supply TVL in USD
            exclude_frozen: Exclude frozen and paused markets (default: False)

        Returns:
            ScreenResponse: {"data": [ScreenLendingRow, ...], "count": N}
        """
        url = f"{get_api_base_url()}/delta-lab/screen/lending"
        params: dict[str, str | int] = {
            "sort": sort,
            "order": order,
            "limit": limit,
        }
        if basis:
            params["basis"] = basis
        elif asset_ids:
            params["asset_ids"] = ",".join(str(a) for a in asset_ids)
        if venue:
            params["venue"] = venue
        if min_tvl is not None:
            params["min_tvl"] = min_tvl
        if exclude_frozen:
            params["exclude_frozen"] = "true"
        response = await self._authed_request("GET", url, params=params)
        return response.json()

    async def screen_perp(
        self,
        *,
        sort: str = "funding_now",
        order: str = "desc",
        limit: int = 100,
        asset_ids: list[int] | None = None,
        basis: str | None = None,
        venue: str | None = None,
    ) -> dict[str, Any]:
        """
        Screen perpetual markets by surface features (funding, basis, OI, volume).

        Args:
            sort: Column to sort by (default: "funding_now")
            order: "asc" or "desc" (default: "desc")
            limit: Max rows, 1-1000 (default: 100)
            asset_ids: Filter to specific base asset IDs
            basis: Basis symbol to filter by (e.g. "ETH") — overrides asset_ids
            venue: Filter by venue name (e.g. "hyperliquid", "binance")

        Returns:
            ScreenResponse: {"data": [ScreenPerpRow, ...], "count": N}
        """
        url = f"{get_api_base_url()}/delta-lab/screen/perp"
        params: dict[str, str | int] = {
            "sort": sort,
            "order": order,
            "limit": limit,
        }
        if basis:
            params["basis"] = basis
        elif asset_ids:
            params["asset_ids"] = ",".join(str(a) for a in asset_ids)
        if venue:
            params["venue"] = venue
        response = await self._authed_request("GET", url, params=params)
        return response.json()

    async def screen_borrow_routes(
        self,
        *,
        sort: str = "ltv_max",
        order: str = "desc",
        limit: int = 100,
        asset_ids: list[int] | None = None,
        basis: str | None = None,
        borrow_asset_ids: list[int] | None = None,
        borrow_basis: str | None = None,
        venue: str | None = None,
        chain_id: int | None = None,
        market_id: int | None = None,
        topology: str | None = None,
        mode_type: str | None = None,
    ) -> dict[str, Any]:
        """
        Screen lending borrow routes (collateral → borrow).

        Args:
            sort: Column to sort by (default: "ltv_max")
            order: "asc" or "desc" (default: "desc")
            limit: Max rows, 1-1000 (default: 100)
            asset_ids: Filter to specific collateral asset IDs
            basis: Collateral basis symbol (e.g. "ETH") — overrides asset_ids
            borrow_asset_ids: Filter to specific borrow asset IDs
            borrow_basis: Borrow basis symbol (e.g. "USD") — overrides borrow_asset_ids
            venue: Filter by venue name
            chain_id: Filter by chain ID
            market_id: Filter by market ID
            topology: Filter by route topology (e.g. "POOLED", "ISOLATED_PAIR")
            mode_type: Filter by route mode type (e.g. "BASE", "EMODE", "ISOLATION")

        Returns:
            ScreenResponse: {"data": [ScreenBorrowRouteRow, ...], "count": N}
        """
        url = f"{get_api_base_url()}/delta-lab/screen/borrow-routes"
        params: dict[str, str | int] = {
            "sort": sort,
            "order": order,
            "limit": limit,
        }

        if basis:
            params["basis"] = basis
        elif asset_ids:
            params["asset_ids"] = ",".join(str(a) for a in asset_ids)

        if borrow_basis:
            params["borrow_basis"] = borrow_basis
        elif borrow_asset_ids:
            params["borrow_asset_ids"] = ",".join(str(a) for a in borrow_asset_ids)

        if venue:
            params["venue"] = venue
        if chain_id is not None:
            params["chain_id"] = chain_id
        if market_id is not None:
            params["market_id"] = market_id
        if topology:
            params["topology"] = topology
        if mode_type:
            params["mode_type"] = mode_type

        response = await self._authed_request("GET", url, params=params)
        return response.json()

    # ------------------------------------------------------------------
    # Pass 2: Entity lookups
    # ------------------------------------------------------------------

    async def get_asset_by_id(self, *, asset_id: int) -> AssetInfo:
        """Fetch a single asset by its numeric Delta Lab ID."""
        data = await self._dl_request("GET", f"/assets/id/{asset_id}/")
        return AssetInfo.from_dict(data)

    async def get_asset_markets(
        self,
        *,
        symbol: str,
        chain_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """List all (asset, market, role) triples a symbol participates in.

        Returns one row per (market_id, role). Role values observed:
        BASE, LENDING_ASSET, etc. Chain filter is optional.
        """
        payload = await self._dl_request(
            "GET",
            f"/assets/{symbol}/markets/",
            params={"chain_id": chain_id},
        )
        return self._unwrap_items(payload)

    async def get_venue_by_id(self, *, venue_id: int) -> VenueInfo:
        data = await self._dl_request("GET", f"/venues/id/{venue_id}/")
        return VenueInfo.from_dict(data)

    async def get_venue_by_name(self, *, name: str) -> VenueInfo:
        data = await self._dl_request("GET", f"/venues/{name}/")
        return VenueInfo.from_dict(data)

    async def get_market_by_id(self, *, market_id: int) -> MarketInfo:
        data = await self._dl_request("GET", f"/markets/id/{market_id}/")
        return MarketInfo.from_dict(data)

    async def get_market_by_venue_external(
        self,
        *,
        venue: str,
        external_id: str,
    ) -> MarketInfo:
        """Look up a market by (venue name, external id).

        Useful when you have a protocol-native market address or identifier
        (e.g. Aave pool address, Pendle market symbol) and need the Delta Lab
        `market_id` to drive TS/lending/latest lookups.
        """
        data = await self._dl_request("GET", f"/markets/{venue}/{external_id}/")
        return MarketInfo.from_dict(data)

    async def get_instrument_by_id(self, *, instrument_id: int) -> InstrumentInfo:
        data = await self._dl_request("GET", f"/instruments/id/{instrument_id}/")
        return InstrumentInfo.from_dict(data)

    # ------------------------------------------------------------------
    # Pass 2: Catalog listings
    # ------------------------------------------------------------------

    async def list_basis_roots(
        self,
        *,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List basis-root symbols ordered by symbol.

        Returns the full envelope (`items, count, total_count`) so callers can
        page via `iter_list` or build their own loop. Default limit is 25 —
        the full catalog is ~3,900 roots.
        """
        return await self._dl_request(
            "GET",
            "/list/basis-roots/",
            params={"limit": limit, "offset": offset},
        )

    async def list_basis_members(
        self,
        *,
        root_symbol: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Members of a basis group (e.g. USDC → [USDC, sUSDC, aUSDC, ...])."""
        payload = await self._dl_request(
            "GET",
            f"/list/basis-members/{root_symbol}/",
            params={"limit": limit},
        )
        return self._unwrap_items(payload)

    async def list_venues(
        self,
        *,
        venue_type: str | None = None,
        chain_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """List all venues, optionally filtered by type (LENDING, PERP, RATE, ...) and chain."""
        payload = await self._dl_request(
            "GET",
            "/list/venues/",
            params={"venue_type": venue_type, "chain_id": chain_id},
        )
        return self._unwrap_items(payload)

    async def list_chains(self) -> list[dict[str, Any]]:
        payload = await self._dl_request("GET", "/list/chains/")
        return self._unwrap_items(payload)

    async def list_instrument_types(self) -> list[dict[str, Any]]:
        payload = await self._dl_request("GET", "/list/instrument-types/")
        return self._unwrap_items(payload)

    async def iter_list(
        self,
        path: str,
        *,
        batch: int = 100,
        extra_params: dict[str, Any] | None = None,
    ):
        """Generator that walks `offset` over any `list/*` endpoint.

        Stops when fewer than `batch` items come back or when `total_count` is
        exhausted. Yields the parsed items one at a time.
        """
        offset = 0
        params = dict(extra_params or {})
        while True:
            page = await self._dl_request(
                "GET",
                path,
                params={**params, "limit": batch, "offset": offset},
            )
            items = self._unwrap_items(page)
            for item in items:
                yield item
            if len(items) < batch:
                return
            offset += batch
            total = page.get("total_count") if isinstance(page, dict) else None
            if total is not None and offset >= total:
                return

    # ------------------------------------------------------------------
    # Pass 2: Graph
    # ------------------------------------------------------------------

    async def get_asset_relations(
        self,
        *,
        asset_id: int,
        direction: str = "both",
        depth: int = 1,
        relation_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Walk the asset-relation graph outward from a single asset.

        `direction` ∈ {"forward", "backward", "both"}; `depth` ∈ 1..3.
        Default `depth=1` keeps the payload small — bump to 2/3 only when you
        need multi-hop paths (see `get_graph_paths` for targeted shortest-path
        queries). Symbols are inlined on each row for readability.
        """
        if depth < 1 or depth > 3:
            raise ValueError(f"depth must be between 1 and 3, got {depth}")
        payload = await self._dl_request(
            "GET",
            f"/assets/id/{asset_id}/relations/",
            params={
                "direction": direction,
                "depth": depth,
                "relation_type": relation_type,
            },
        )
        return self._unwrap_items(payload)

    async def get_graph_paths(
        self,
        *,
        from_asset_id: int,
        to_asset_id: int,
        max_hops: int = 3,
        relation_types: list[str] | tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """Find paths between two assets in the relation graph.

        `max_hops` ∈ 1..4; `relation_types` restricts which edge kinds to
        traverse (e.g. ["WRAPS", "REBASING_TO_BASE"]). Returns a list of path
        objects with symbols inlined on each hop.
        """
        if max_hops < 1 or max_hops > 4:
            raise ValueError(f"max_hops must be between 1 and 4, got {max_hops}")
        relation_csv = ",".join(relation_types) if relation_types else None
        payload = await self._dl_request(
            "GET",
            "/graph/paths/",
            params={
                "from_asset_id": from_asset_id,
                "to_asset_id": to_asset_id,
                "max_hops": max_hops,
                "relation_types": relation_csv,
            },
        )
        return self._unwrap_items(payload)

    # ------------------------------------------------------------------
    # Pass 3: Search
    # ------------------------------------------------------------------
    #
    # The /search/* endpoints are a distinct surface from the legacy
    # /assets/search/ raw-SQL endpoint (`search_assets`). They return
    # `{items, count, has_more, offset}`, support offset pagination, and
    # accept a `fields=full` projection toggle (no-op on endpoints that
    # already return their minimal useful set).
    #
    # Default `limit=25` chosen for agent context-safety; callers doing
    # scripted walks should use `search_all()` below.

    async def search_assets_v2(
        self,
        *,
        q: str | None = None,
        chain_id: int | None = None,
        basis_root: str | None = None,
        has_address: bool | None = None,
        limit: int = 25,
        offset: int = 0,
        fields: str | None = None,
    ) -> dict[str, Any]:
        """Search the Delta Lab asset catalog.

        Distinct from the legacy `search_assets(...)` (which hits
        `/assets/search/` — a Django raw-SQL variant). This method targets
        `/search/assets/` which supports projection, basis filtering, and
        `has_address` filtering.

        Returns the full envelope `{items, count, has_more, offset}` so
        callers can paginate explicitly.
        """
        return await self._dl_request(
            "GET",
            "/search/assets/",
            params={
                "q": q,
                "chain_id": chain_id,
                "basis_root": basis_root,
                "has_address": has_address,
                "limit": limit,
                "offset": offset,
                "fields": fields,
            },
        )

    async def search_markets(
        self,
        *,
        venue: str | None = None,
        chain_id: int | None = None,
        market_type: str | None = None,
        asset_id: int | None = None,
        basis_root: str | None = None,
        limit: int = 25,
        offset: int = 0,
        fields: str | None = None,
    ) -> dict[str, Any]:
        return await self._dl_request(
            "GET",
            "/search/markets/",
            params={
                "venue": venue,
                "chain_id": chain_id,
                "market_type": market_type,
                "asset_id": asset_id,
                "basis_root": basis_root,
                "limit": limit,
                "offset": offset,
                "fields": fields,
            },
        )

    async def search_instruments(
        self,
        *,
        instrument_type: str | None = None,
        basis_root: str | None = None,
        venue: str | None = None,
        chain_id: int | None = None,
        quote_asset_id: int | None = None,
        maturity_after: datetime | str | None = None,
        maturity_before: datetime | str | None = None,
        limit: int = 25,
        offset: int = 0,
        fields: str | None = None,
    ) -> dict[str, Any]:
        return await self._dl_request(
            "GET",
            "/search/instruments/",
            params={
                "instrument_type": instrument_type,
                "basis_root": basis_root,
                "venue": venue,
                "chain_id": chain_id,
                "quote_asset_id": quote_asset_id,
                "maturity_after": _maybe_iso(maturity_after),
                "maturity_before": _maybe_iso(maturity_before),
                "limit": limit,
                "offset": offset,
                "fields": fields,
            },
        )

    async def search_opportunities(
        self,
        *,
        basis_root: str | None = None,
        side: str | None = None,
        venue: str | None = None,
        chain_id: int | None = None,
        instrument_type: str | None = None,
        limit: int = 25,
        offset: int = 0,
        fields: str | None = None,
    ) -> dict[str, Any]:
        """Discovery-shape opportunity search.

        Returns the trimmed opportunity shape (~14 fields: side, venue,
        chain_id, market_id, maturity_ts, basis_symbol, instrument_id,
        instrument_type, basis/deposit/receipt/exposure asset ids+symbols).
        For the full analytic payload (APY, risk, summary) use
        `get_basis_apy_sources(...)` on a specific basis symbol.
        """
        if side is not None and side not in ("LONG", "SHORT"):
            raise ValueError(f"side must be LONG or SHORT, got {side!r}")
        return await self._dl_request(
            "GET",
            "/search/opportunities/",
            params={
                "basis_root": basis_root,
                "side": side,
                "venue": venue,
                "chain_id": chain_id,
                "instrument_type": instrument_type,
                "limit": limit,
                "offset": offset,
                "fields": fields,
            },
        )

    async def search_venues(
        self,
        *,
        q: str | None = None,
        venue_type: str | None = None,
        chain_id: int | None = None,
        limit: int = 25,
        offset: int = 0,
        fields: str | None = None,
    ) -> dict[str, Any]:
        return await self._dl_request(
            "GET",
            "/search/venues/",
            params={
                "q": q,
                "venue_type": venue_type,
                "chain_id": chain_id,
                "limit": limit,
                "offset": offset,
                "fields": fields,
            },
        )

    async def search_all(
        self,
        search_fn,
        *,
        batch: int = 50,
        max_items: int | None = None,
        **kwargs: Any,
    ):
        """Walk a search method using `offset` + `has_more` pagination.

        Example:
            async for item in client.search_all(client.search_assets_v2, q="ETH"):
                ...

        `batch` controls the per-call `limit`. `max_items` caps the total
        number of items yielded (useful safety rail — default is unbounded).
        """
        yielded = 0
        offset = kwargs.pop("offset", 0)
        while True:
            page = await search_fn(limit=batch, offset=offset, **kwargs)
            items = self._unwrap_items(page)
            for item in items:
                if max_items is not None and yielded >= max_items:
                    return
                yield item
                yielded += 1
            if not page.get("has_more") or not items:
                return
            offset += len(items)

    # ------------------------------------------------------------------
    # Pass 4: ID-keyed point timeseries + latest snapshots
    # ------------------------------------------------------------------
    #
    # TS methods return a DataFrame indexed on `ts` (or empty frame).
    # Latest methods return a typed dataclass, or `None` when the server
    # reports `not_found` (common for sparse series — e.g. boros markets
    # have no perp funding surface, base ETH has no yield snapshot).

    async def get_asset_price_ts(
        self,
        *,
        asset_id: int,
        lookback_days: int | None = 30,
        limit: int | None = 500,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> pd.DataFrame:
        payload = await self._dl_request(
            "GET",
            f"/assets/id/{asset_id}/price/",
            params=_ts_params(
                lookback_days=lookback_days, limit=limit, start=start, end=end
            ),
        )
        return self._to_df(self._unwrap_items(payload))

    async def get_asset_price_latest(self, *, asset_id: int) -> PriceLatest | None:
        data = await self._dl_request(
            "GET",
            f"/assets/id/{asset_id}/price/latest/",
            soft_not_found=True,
        )
        return PriceLatest.from_dict(data) if data else None

    async def get_asset_yield_ts(
        self,
        *,
        asset_id: int,
        lookback_days: int | None = 30,
        limit: int | None = 500,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> pd.DataFrame:
        payload = await self._dl_request(
            "GET",
            f"/assets/id/{asset_id}/yield/",
            params=_ts_params(
                lookback_days=lookback_days, limit=limit, start=start, end=end
            ),
        )
        return self._to_df(self._unwrap_items(payload))

    async def get_asset_yield_latest(self, *, asset_id: int) -> YieldLatest | None:
        data = await self._dl_request(
            "GET",
            f"/assets/id/{asset_id}/yield/latest/",
            soft_not_found=True,
        )
        return YieldLatest.from_dict(data) if data else None

    async def get_market_lending_ts(
        self,
        *,
        market_id: int,
        asset_id: int,
        lookback_days: int | None = 30,
        limit: int | None = 500,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> pd.DataFrame:
        """Lending TS for a (market_id, asset_id) pair.

        `asset_id` is REQUIRED by the server — a lending market tracks
        per-asset supply/borrow rates and must be disambiguated.
        """
        payload = await self._dl_request(
            "GET",
            f"/markets/id/{market_id}/lending/",
            params=_ts_params(
                lookback_days=lookback_days,
                limit=limit,
                start=start,
                end=end,
                extra={"asset_id": asset_id},
            ),
        )
        return self._to_df(self._unwrap_items(payload))

    async def get_market_lending_latest(
        self, *, market_id: int, asset_id: int
    ) -> LendingLatest | None:
        data = await self._dl_request(
            "GET",
            f"/markets/id/{market_id}/lending/latest/",
            params={"asset_id": asset_id},
            soft_not_found=True,
        )
        return LendingLatest.from_dict(data) if data else None

    async def get_market_pendle_ts(
        self,
        *,
        market_id: int,
        lookback_days: int | None = 30,
        limit: int | None = 500,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> pd.DataFrame:
        payload = await self._dl_request(
            "GET",
            f"/markets/id/{market_id}/pendle/",
            params=_ts_params(
                lookback_days=lookback_days, limit=limit, start=start, end=end
            ),
        )
        return self._to_df(self._unwrap_items(payload))

    async def get_market_pendle_latest(self, *, market_id: int) -> PendleLatest | None:
        data = await self._dl_request(
            "GET",
            f"/markets/id/{market_id}/pendle/latest/",
            soft_not_found=True,
        )
        return PendleLatest.from_dict(data) if data else None

    async def get_market_boros_ts(
        self,
        *,
        market_id: int,
        lookback_days: int | None = 30,
        limit: int | None = 500,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> pd.DataFrame:
        payload = await self._dl_request(
            "GET",
            f"/markets/id/{market_id}/boros/",
            params=_ts_params(
                lookback_days=lookback_days, limit=limit, start=start, end=end
            ),
        )
        return self._to_df(self._unwrap_items(payload))

    async def get_market_boros_latest(self, *, market_id: int) -> BorosLatest | None:
        data = await self._dl_request(
            "GET",
            f"/markets/id/{market_id}/boros/latest/",
            soft_not_found=True,
        )
        return BorosLatest.from_dict(data) if data else None

    async def get_instrument_funding_ts(
        self,
        *,
        instrument_id: int,
        lookback_days: int | None = 30,
        limit: int | None = 500,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> pd.DataFrame:
        payload = await self._dl_request(
            "GET",
            f"/instruments/id/{instrument_id}/funding/",
            params=_ts_params(
                lookback_days=lookback_days, limit=limit, start=start, end=end
            ),
        )
        return self._to_df(self._unwrap_items(payload))

    async def get_instrument_funding_latest(
        self, *, instrument_id: int
    ) -> FundingLatest | None:
        data = await self._dl_request(
            "GET",
            f"/instruments/id/{instrument_id}/funding/latest/",
            soft_not_found=True,
        )
        return FundingLatest.from_dict(data) if data else None

    # ------------------------------------------------------------------
    # Pass 5: Bulk TS + latest (auto-chunked) and orchestration
    # ------------------------------------------------------------------
    #
    # Server caps each bulk request at 100 ids (or 100 pairs for lending).
    # `_bulk_chunked` / `_bulk_pairs_chunked` transparently split larger
    # input lists into concurrent sub-requests, with a semaphore capping
    # in-flight bulk calls at _BULK_CONCURRENCY.
    #
    # Bulk TS methods return `dict[int, pd.DataFrame]` (or
    # `dict[tuple[int, int], pd.DataFrame]` for lending). Missing ids
    # simply don't appear in the dict.
    #
    # Bulk latest methods return `dict[int, <TypedRecord> | None]`; the
    # server sends `null` for missing snapshots (sparse series).

    _BULK_CAP: int = 100
    _BULK_CONCURRENCY: int = 5

    async def _bulk_chunked(
        self,
        path: str,
        ids: list[int] | tuple[int, ...],
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> dict[str, Any]:
        """Fetch `path` in 100-id chunks concurrently, merged into one dict.

        The server returns `{"<id>": payload, ...}`; chunks are merged by
        key. Duplicate ids in the input are deduplicated (first-seen order
        preserved) to avoid redundant requests.
        """
        seen: set[int] = set()
        deduped: list[int] = []
        for i in ids:
            if i not in seen:
                seen.add(i)
                deduped.append(i)
        if not deduped:
            return {}

        chunks = [
            deduped[i : i + self._BULK_CAP]
            for i in range(0, len(deduped), self._BULK_CAP)
        ]
        semaphore = asyncio.Semaphore(self._BULK_CONCURRENCY)

        async def _fetch(chunk: list[int]) -> dict[str, Any]:
            async with semaphore:
                chunk_params = dict(params or {})
                chunk_params["ids"] = ",".join(str(i) for i in chunk)
                return await self._dl_request(method, path, params=chunk_params)

        results = await asyncio.gather(*(_fetch(c) for c in chunks))
        merged: dict[str, Any] = {}
        for r in results:
            if isinstance(r, dict):
                merged.update(r)
        return merged

    async def _bulk_pairs_chunked(
        self,
        path: str,
        pairs: list[tuple[int, int]] | tuple[tuple[int, int], ...],
        *,
        extra_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fetch lending bulk endpoints by (market_id, asset_id) pairs.

        POSTs the pairs as JSON body when >20 pairs (URL-length safety);
        GETs with `pairs=a:b,c:d` otherwise. Auto-chunked at 100 pairs.
        """
        seen: set[tuple[int, int]] = set()
        deduped: list[tuple[int, int]] = []
        for p in pairs:
            tp = (int(p[0]), int(p[1]))
            if tp not in seen:
                seen.add(tp)
                deduped.append(tp)
        if not deduped:
            return {}

        chunks = [
            deduped[i : i + self._BULK_CAP]
            for i in range(0, len(deduped), self._BULK_CAP)
        ]
        semaphore = asyncio.Semaphore(self._BULK_CONCURRENCY)
        base_params = dict(extra_params or {})

        async def _fetch(chunk: list[tuple[int, int]]) -> dict[str, Any]:
            async with semaphore:
                if len(chunk) > 20:
                    body = {"pairs": [list(p) for p in chunk]}
                    body.update(base_params)
                    return await self._dl_request("POST", path, json=body)
                params = dict(base_params)
                params["pairs"] = ",".join(f"{m}:{a}" for m, a in chunk)
                return await self._dl_request("GET", path, params=params)

        results = await asyncio.gather(*(_fetch(c) for c in chunks))
        merged: dict[str, Any] = {}
        for r in results:
            if isinstance(r, dict):
                merged.update(r)
        return merged

    @staticmethod
    def _rows_map_to_df_map(
        result: dict[str, Any],
    ) -> dict[int, pd.DataFrame]:
        out: dict[int, pd.DataFrame] = {}
        for key, rows in result.items():
            if not isinstance(rows, list):
                continue
            out[int(key)] = DeltaLabClient._to_df(rows)
        return out

    @staticmethod
    def _pair_rows_to_df_map(
        result: dict[str, Any],
    ) -> dict[tuple[int, int], pd.DataFrame]:
        out: dict[tuple[int, int], pd.DataFrame] = {}
        for key, rows in result.items():
            if not isinstance(rows, list) or ":" not in str(key):
                continue
            mkt, asset = str(key).split(":", 1)
            out[(int(mkt), int(asset))] = DeltaLabClient._to_df(rows)
        return out

    # ---- Bulk TS ----

    async def bulk_prices(
        self,
        *,
        asset_ids: list[int] | tuple[int, ...],
        lookback_days: int | None = 30,
        limit_per_key: int | None = 500,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> dict[int, pd.DataFrame]:
        raw = await self._bulk_chunked(
            "/bulk/prices/",
            asset_ids,
            params=_bulk_ts_params(
                lookback_days=lookback_days,
                limit_per_key=limit_per_key,
                start=start,
                end=end,
            ),
        )
        return self._rows_map_to_df_map(raw)

    async def bulk_yields(
        self,
        *,
        asset_ids: list[int] | tuple[int, ...],
        lookback_days: int | None = 30,
        limit_per_key: int | None = 500,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> dict[int, pd.DataFrame]:
        raw = await self._bulk_chunked(
            "/bulk/yields/",
            asset_ids,
            params=_bulk_ts_params(
                lookback_days=lookback_days,
                limit_per_key=limit_per_key,
                start=start,
                end=end,
            ),
        )
        return self._rows_map_to_df_map(raw)

    async def bulk_funding(
        self,
        *,
        instrument_ids: list[int] | tuple[int, ...],
        lookback_days: int | None = 30,
        limit_per_key: int | None = 500,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> dict[int, pd.DataFrame]:
        raw = await self._bulk_chunked(
            "/bulk/funding/",
            instrument_ids,
            params=_bulk_ts_params(
                lookback_days=lookback_days,
                limit_per_key=limit_per_key,
                start=start,
                end=end,
            ),
        )
        return self._rows_map_to_df_map(raw)

    async def bulk_pendle(
        self,
        *,
        market_ids: list[int] | tuple[int, ...],
        lookback_days: int | None = 30,
        limit_per_key: int | None = 500,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> dict[int, pd.DataFrame]:
        raw = await self._bulk_chunked(
            "/bulk/pendle/",
            market_ids,
            params=_bulk_ts_params(
                lookback_days=lookback_days,
                limit_per_key=limit_per_key,
                start=start,
                end=end,
            ),
        )
        return self._rows_map_to_df_map(raw)

    async def bulk_boros(
        self,
        *,
        market_ids: list[int] | tuple[int, ...],
        lookback_days: int | None = 30,
        limit_per_key: int | None = 500,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> dict[int, pd.DataFrame]:
        raw = await self._bulk_chunked(
            "/bulk/boros/",
            market_ids,
            params=_bulk_ts_params(
                lookback_days=lookback_days,
                limit_per_key=limit_per_key,
                start=start,
                end=end,
            ),
        )
        return self._rows_map_to_df_map(raw)

    async def bulk_lending(
        self,
        *,
        pairs: list[tuple[int, int]] | tuple[tuple[int, int], ...],
        lookback_days: int | None = 30,
        limit_per_key: int | None = 500,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> dict[tuple[int, int], pd.DataFrame]:
        raw = await self._bulk_pairs_chunked(
            "/bulk/lending/",
            pairs,
            extra_params=_bulk_ts_params(
                lookback_days=lookback_days,
                limit_per_key=limit_per_key,
                start=start,
                end=end,
            ),
        )
        return self._pair_rows_to_df_map(raw)

    # ---- Bulk latest ----

    async def bulk_latest_prices(
        self, *, asset_ids: list[int] | tuple[int, ...]
    ) -> dict[int, PriceLatest | None]:
        raw = await self._bulk_chunked("/bulk/latest/prices/", asset_ids)
        return {
            int(k): (PriceLatest.from_dict(v) if v else None) for k, v in raw.items()
        }

    async def bulk_latest_yields(
        self, *, asset_ids: list[int] | tuple[int, ...]
    ) -> dict[int, YieldLatest | None]:
        raw = await self._bulk_chunked("/bulk/latest/yields/", asset_ids)
        return {
            int(k): (YieldLatest.from_dict(v) if v else None) for k, v in raw.items()
        }

    async def bulk_latest_funding(
        self, *, instrument_ids: list[int] | tuple[int, ...]
    ) -> dict[int, FundingLatest | None]:
        raw = await self._bulk_chunked("/bulk/latest/funding/", instrument_ids)
        return {
            int(k): (FundingLatest.from_dict(v) if v else None) for k, v in raw.items()
        }

    async def bulk_latest_pendle(
        self, *, market_ids: list[int] | tuple[int, ...]
    ) -> dict[int, PendleLatest | None]:
        raw = await self._bulk_chunked("/bulk/latest/pendle/", market_ids)
        return {
            int(k): (PendleLatest.from_dict(v) if v else None) for k, v in raw.items()
        }

    async def bulk_latest_boros(
        self, *, market_ids: list[int] | tuple[int, ...]
    ) -> dict[int, BorosLatest | None]:
        raw = await self._bulk_chunked("/bulk/latest/boros/", market_ids)
        return {
            int(k): (BorosLatest.from_dict(v) if v else None) for k, v in raw.items()
        }

    async def bulk_latest_lending(
        self, *, pairs: list[tuple[int, int]] | tuple[tuple[int, int], ...]
    ) -> dict[tuple[int, int], LendingLatest | None]:
        raw = await self._bulk_pairs_chunked("/bulk/latest/lending/", pairs)
        out: dict[tuple[int, int], LendingLatest | None] = {}
        for key, value in raw.items():
            if ":" not in str(key):
                continue
            mkt, asset = str(key).split(":", 1)
            out[(int(mkt), int(asset))] = (
                LendingLatest.from_dict(value) if value else None
            )
        return out

    # ---- Orchestration ----

    async def explore(
        self,
        *,
        symbol: str,
        chain_id: int | None = None,
        relations_depth: int = 1,
    ) -> dict[str, Any]:
        """One-shot discovery bundle: asset + markets + relations + latest price/yield.

        Returns the merged server payload:
        `{query, asset, matches, relations, markets, price_latest, yield_latest}`

        Default `relations_depth=1` keeps the payload agent-friendly
        (~20 KB); raising to 2–3 is fine in scripts but unsuitable for
        an agent context (100 KB+ on common symbols).
        """
        if relations_depth < 1 or relations_depth > 3:
            raise ValueError(
                f"relations_depth must be between 1 and 3, got {relations_depth}"
            )
        if relations_depth >= 2:
            warnings.warn(
                f"explore(relations_depth={relations_depth}) can return large payloads "
                "(>100 KB on common symbols); prefer depth=1 for agent-facing calls.",
                stacklevel=2,
            )
        return await self._dl_request(
            "GET",
            f"/explore/{symbol}/",
            params={"chain_id": chain_id, "relations_depth": relations_depth},
        )

    async def fetch_backtest_bundle(
        self,
        *,
        basis_root: str,
        side: str | None = None,
        lookback_days: int = 30,
        limit_per_key: int = 500,
        instrument_limit: int | None = None,
    ) -> BacktestBundle:
        """Single-call backtest hydration.

        Returns a `BacktestBundle` with:
        - `opportunities`: DataFrame of discovery-shape opportunity rows
        - `funding_ts`: `{instrument_id: DataFrame}`
        - `lending_ts`: `{(market_id, asset_id): DataFrame}`
        - scalars: `basis_root, side, lookback_days, start, end`

        Equivalent to fanning out opportunities + per-instrument
        funding TS + per-(market, asset) lending TS on the client, but in
        one server-side call. Use for backtests / structured analysis —
        not MCP (payloads are typically 50–500 KB).
        """
        if side is not None and side not in ("LONG", "SHORT"):
            raise ValueError(f"side must be LONG or SHORT, got {side!r}")
        body: dict[str, Any] = {
            "basis_root": basis_root,
            "lookback_days": lookback_days,
            "limit_per_key": limit_per_key,
        }
        if side is not None:
            body["side"] = side
        if instrument_limit is not None:
            body["instrument_limit"] = instrument_limit

        payload = await self._dl_request("POST", "/backtest/fetch/", json=body)

        opps_df = self._to_df(payload.get("opportunities") or [], ts_col="")
        # The opportunities list doesn't have a `ts` column — _to_df returns
        # an un-indexed DataFrame when ts_col is absent, which is what we want.
        funding_raw = payload.get("funding_ts") or {}
        lending_raw = payload.get("lending_ts") or {}

        funding_ts = {
            int(k): self._to_df(v)
            for k, v in funding_raw.items()
            if isinstance(v, list)
        }
        lending_ts: dict[tuple[int, int], pd.DataFrame] = {}
        for key, rows in lending_raw.items():
            if not isinstance(rows, list) or ":" not in str(key):
                continue
            mkt, asset = str(key).split(":", 1)
            lending_ts[(int(mkt), int(asset))] = self._to_df(rows)

        start = _maybe_parse_iso(payload.get("start"))
        end = _maybe_parse_iso(payload.get("end"))

        return BacktestBundle(
            basis_root=payload.get("basis_root") or basis_root,
            side=payload.get("side"),
            lookback_days=payload.get("lookback_days") or lookback_days,
            start=start,
            end=end,
            opportunities=opps_df,
            lending_ts=lending_ts,
            funding_ts=funding_ts,
            raw=payload,
        )


def _maybe_iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _ts_params(
    *,
    lookback_days: int | None,
    limit: int | None,
    start: datetime | str | None,
    end: datetime | str | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Common query params for all ID-keyed point-TS endpoints."""
    params: dict[str, Any] = {
        "lookback_days": lookback_days,
        "limit": limit,
        "start": _maybe_iso(start),
        "end": _maybe_iso(end),
    }
    if extra:
        params.update(extra)
    return params


def _bulk_ts_params(
    *,
    lookback_days: int | None,
    limit_per_key: int | None,
    start: datetime | str | None,
    end: datetime | str | None,
) -> dict[str, Any]:
    """Common query params for bulk TS endpoints (cap 100 ids / pairs)."""
    return {
        "lookback_days": lookback_days,
        "limit_per_key": limit_per_key,
        "start": _maybe_iso(start),
        "end": _maybe_iso(end),
    }


def _maybe_parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


DELTA_LAB_CLIENT = DeltaLabClient()
