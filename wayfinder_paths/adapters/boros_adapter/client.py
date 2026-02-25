"""Boros HTTP Client for interacting with Boros API."""

from __future__ import annotations

from typing import Any

import aiohttp
from aiocache import Cache
from loguru import logger

# Default Boros API endpoints
# Open API endpoints (public, no auth required)
OPEN_API_ENDPOINTS = {
    "assets": "/open-api/v1/assets/all",
    "markets_open": "/open-api/v1/markets",
    "market_chart": "/open-api/v1/markets/chart",
    "limit_orders_open": "/open-api/v1/accounts/limit-orders",
}

# Core API endpoints (authenticated/internal)
CORE_API_ENDPOINTS = {
    "markets": "/core/v1/markets",
    "market": "/core/v1/markets",
    "orderbook": "/core/v1/order-books",
    "active_positions": "/core/v1/pnl/positions/active",
    "closed_positions": "/core/v1/pnl/positions/closed",
    "collaterals": "/core/v1/collaterals/summary",
    "collateral_summary": "/core/v1/collaterals/summary",
    "build_deposit_calldata": "/core/v2/calldata/deposit",
    "build_withdraw_calldata": "/core/v1/calldata/withdraw/request",
    "build_finalize_withdrawal_calldata": "/core/v1/calldata/withdraw/finalize",
    "build_place_order_calldata": "/core/v4/calldata/place-order",
    "build_close_position_calldata": "/core/v4/calldata/close-active-position",
    "build_cancel_order_calldata": "/core/v3/calldata/cancel-order",
    "build_cash_transfer_calldata": "/core/v3/calldata/cash-transfer",
    "limit_orders": "/core/v1/pnl/limit-orders",
    "amm_summary": "/core/v1/amm/summary",
    "amm_info": "/core/v2/amm",
    "amm_chart": "/core/v2/amm/chart",
    "simulate_add_liquidity": "/core/v1/simulations/add-liquidity-single-cash",
    "simulate_remove_liquidity": "/core/v1/simulations/remove-liquidity-single-cash",
    "build_add_liquidity_calldata": "/core/v4/calldata/add-liquidity-single-cash-to-amm",
    "build_remove_liquidity_calldata": "/core/v4/calldata/remove-liquidity-single-cash-from-amm",
    "amm_rewards": "/core/v1/amm/rewards",
    "amm_rewards_proof": "/core/v1/merkels/amm_lp_rewards/user",
}

DEFAULT_ENDPOINTS = {**OPEN_API_ENDPOINTS, **CORE_API_ENDPOINTS}


class BorosClient:
    """HTTP client for Boros API.

    Provides low-level HTTP methods for interacting with the Boros API.
    """

    def __init__(
        self,
        base_url: str = "https://api.boros.finance",
        endpoints: dict[str, str] | None = None,
        user_address: str | None = None,
        account_id: int = 0,
        timeout: int = 30,
    ) -> None:
        """Initialize Boros client.

        Args:
            base_url: Base URL for Boros API.
            endpoints: Custom endpoint paths (optional).
            user_address: User wallet address for authenticated requests.
            account_id: Boros account ID (0 = cross margin).
            timeout: Request timeout in seconds.
        """
        self.base_url = base_url.rstrip("/")
        self.endpoints = {**DEFAULT_ENDPOINTS, **(endpoints or {})}
        self.user_address = user_address
        self.account_id = account_id
        self.timeout = timeout
        self._cache = Cache(Cache.MEMORY)

    async def _http(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> Any:
        """Make HTTP request to Boros API.

        Args:
            method: HTTP method (GET, POST, etc.).
            path: API endpoint path.
            params: Query parameters.
            json_body: JSON body for POST requests.
            timeout: Request timeout override.

        Returns:
            Parsed JSON response.
        """
        url = f"{self.base_url}{path}"
        timeout_val = timeout or self.timeout

        async with aiohttp.ClientSession() as session:
            async with session.request(
                method,
                url,
                params=params,
                json=json_body,
                timeout=aiohttp.ClientTimeout(total=timeout_val),
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise aiohttp.ClientResponseError(
                        resp.request_info,
                        resp.history,
                        status=resp.status,
                        message=f"{resp.reason}: {body}",
                    )
                return await resp.json()

    async def get_assets(self) -> list[dict[str, Any]]:
        """Get all Boros assets (collateral tokens with addresses).

        Returns:
            List of asset dicts with tokenId, address, symbol, decimals, isCollateral.
        """
        cache_key = f"boros:assets:{self.base_url}"
        cached = await self._cache.get(cache_key)
        if cached:
            return cached

        path = self.endpoints["assets"]
        data = await self._http("GET", path)
        assets: list[dict[str, Any]] = data.get("assets") or []
        await self._cache.set(cache_key, assets, ttl=3600)  # Cache for 1 hour
        return assets

    async def list_markets(
        self,
        *,
        is_whitelisted: bool | None = True,
        skip: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List available Boros markets.

        Args:
            is_whitelisted: Filter by whitelisted markets.
            skip: Number of markets to skip.
            limit: Maximum markets to return.

        Returns:
            List of market dictionaries.
        """
        # Boros API enforces: 1 <= limit <= 100.
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 100
        if limit <= 0:
            limit = 100
        if limit > 100:
            limit = 100

        try:
            skip = int(skip)
        except (TypeError, ValueError):
            skip = 0
        if skip < 0:
            skip = 0

        cache_key = f"boros:markets:{self.base_url}:{is_whitelisted}:{skip}:{limit}"
        cached = await self._cache.get(cache_key)
        if cached:
            return cached

        path = self.endpoints["markets"]
        params: dict[str, Any] = {"skip": skip, "limit": limit}
        if is_whitelisted is not None:
            params["isWhitelisted"] = "true" if is_whitelisted else "false"

        data = await self._http("GET", path, params=params)
        markets: list[dict[str, Any]] = (
            data.get("markets") or data.get("results") or data.get("data") or data
        )
        await self._cache.set(cache_key, markets, ttl=300)
        return markets

    @staticmethod
    def _extract_market_from_payload(
        payload: Any, market_id: int
    ) -> dict[str, Any] | None:
        """Extract a single market from various Boros API payload shapes."""
        try:
            target_id = int(market_id)
        except (TypeError, ValueError):
            return None

        def _matches(obj: Any) -> bool:
            if not isinstance(obj, dict):
                return False
            try:
                mid = int(obj.get("marketId") or obj.get("id") or 0)
            except (TypeError, ValueError):
                return False
            return mid == target_id

        # Direct market dict.
        if _matches(payload):
            return payload  # type: ignore[return-value]

        # Common wrappers.
        if isinstance(payload, dict):
            candidates = (
                payload.get("market")
                or payload.get("markets")
                or payload.get("results")
                or payload.get("data")
            )
            if isinstance(candidates, dict) and _matches(candidates):
                return candidates
            if isinstance(candidates, list):
                return next((m for m in candidates if _matches(m)), None)

        if isinstance(payload, list):
            return next((m for m in payload if _matches(m)), None)

        return None

    async def get_market(self, market_id: int) -> dict[str, Any]:
        """Fetch a single market by ID.

        Args:
            market_id: Boros market ID.

        Returns:
            Market dictionary.
        """
        cache_key = f"boros:market:{self.base_url}:{int(market_id)}"
        cached = await self._cache.get(cache_key)
        if cached:
            return cached

        path = self.endpoints["market"]
        market: dict[str, Any] | None = None
        try:
            payload = await self._http("GET", path, params={"marketId": market_id})
            market = self._extract_market_from_payload(payload, market_id)
        except Exception:
            market = None

        # Fallback: paginate markets (Boros enforces limit<=100).
        if not market:
            skip = 0
            limit = 100
            while True:
                markets = await self.list_markets(
                    skip=skip, limit=limit, is_whitelisted=None
                )
                if not markets:
                    break
                try:
                    target_id = int(market_id)
                except (TypeError, ValueError):
                    target_id = 0

                def _as_id(obj: dict[str, Any]) -> int:
                    try:
                        return int(obj.get("marketId") or obj.get("id") or 0)
                    except (TypeError, ValueError):
                        return 0

                market = next(
                    (m for m in markets if _as_id(m) == target_id),
                    None,
                )
                if market:
                    break
                if len(markets) < limit:
                    break
                skip += limit

        if not market:
            raise ValueError(f"Market {int(market_id)} not found")

        await self._cache.set(cache_key, market, ttl=300)
        return market

    async def get_order_book(
        self,
        market_id: int,
        *,
        tick_size: float = 0.001,
    ) -> dict[str, Any]:
        """Get order book for a market.

        Args:
            market_id: Boros market ID.
            tick_size: Tick size for aggregation.

        Returns:
            Order book with long/short sides.
        """
        path = f"{self.endpoints['orderbook']}/{int(market_id)}"
        params = {"tickSize": tick_size}
        return await self._http("GET", path, params=params)

    async def get_market_history(
        self,
        market_id: int,
        *,
        time_frame: str = "1h",
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get historical rate data for a market.

        Args:
            market_id: Boros market ID.
            time_frame: Time frame for candles (5m, 1h, 1d, 1w).
            start_ts: Start timestamp (Unix seconds).
            end_ts: End timestamp (Unix seconds).

        Returns:
            List of OHLCV + rate data dicts with keys like:
            o, h, l, c, v, mr (markRate), ofr (oracleFloatingRate), etc.
        """
        path = self.endpoints["market_chart"]
        params: dict[str, Any] = {
            "marketId": int(market_id),
            "timeFrame": time_frame,
        }
        if start_ts is not None:
            params["startTimestamp"] = int(start_ts)
        if end_ts is not None:
            params["endTimestamp"] = int(end_ts)

        data = await self._http("GET", path, params=params)
        # API may return {"chart": [...]} or just [...]
        if isinstance(data, dict):
            return data.get("chart") or data.get("data") or data.get("results") or []
        return data if isinstance(data, list) else []

    async def get_collaterals(
        self,
        user_address: str | None = None,
        account_id: int | None = None,
    ) -> dict[str, Any]:
        """Get collateral summary for user.

        Args:
            user_address: User wallet address (defaults to client's user_address).
            account_id: Account ID (defaults to client's account_id).

        Returns:
            Collateral summary with positions.
        """
        path = self.endpoints["collaterals"]
        params = {
            "userAddress": user_address or self.user_address,
            "accountId": int(account_id if account_id is not None else self.account_id),
        }
        return await self._http("GET", path, params=params)

    async def get_open_orders(
        self,
        user_address: str | None = None,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Get open limit orders.

        Args:
            user_address: User wallet address.
            limit: Maximum orders to return.

        Returns:
            List of open orders.
        """
        path = self.endpoints["limit_orders"]
        params = {
            "userAddress": user_address or self.user_address,
            "limit": limit,
        }
        try:
            data = await self._http("GET", path, params=params)
            return data.get("orders") or data.get("results") or []
        except Exception as e:
            logger.warning(f"Failed to fetch open orders: {e}")
            return []

    async def build_deposit_calldata(
        self,
        *,
        token_id: int,
        amount_wei: int,
        market_id: int,
        user_address: str | None = None,
        account_id: int | None = None,
    ) -> dict[str, Any]:
        """Build calldata for deposit.

        Args:
            token_id: Boros token ID (e.g., 3 for USDT).
            amount_wei: Amount in NATIVE token decimals (despite the param name).
                Example: USDT has 6 decimals, so 1 USDT = 1_000_000.
        market_id: Target market ID.
        user_address: User wallet address.
        account_id: Boros account ID (0 = cross margin).

        Returns:
            Calldata dictionary with 'to', 'data', 'value' fields.
        """
        path = self.endpoints["build_deposit_calldata"]
        return await self._http(
            "GET",
            path,
            params={
                "userAddress": user_address or self.user_address,
                "accountId": account_id if account_id is not None else self.account_id,
                "tokenId": int(token_id),
                "amount": str(amount_wei),
                "marketId": int(market_id),
            },
        )

    async def build_withdraw_calldata(
        self,
        *,
        token_id: int,
        amount_wei: int,
        user_address: str | None = None,
        account_id: int | None = None,
    ) -> dict[str, Any]:
        """Build calldata for withdrawal.

        Args:
            token_id: Boros token ID.
            amount_wei: Amount in NATIVE token decimals (despite the param name).
                Example: USDT has 6 decimals, so 1 USDT = 1_000_000.
            user_address: User wallet address.
            account_id: Account ID.

        Returns:
            Calldata dictionary.
        """
        path = self.endpoints["build_withdraw_calldata"]
        return await self._http(
            "GET",
            path,
            params={
                "userAddress": user_address or self.user_address,
                "accountId": int(
                    account_id if account_id is not None else self.account_id
                ),
                "tokenId": int(token_id),
                "amount": str(int(amount_wei)),
            },
        )

    async def build_place_order_calldata(
        self,
        *,
        market_acc: str,
        market_id: int,
        side: int,
        size_wei: int,
        limit_tick: int,
        tif: int = 0,
        slippage: float = 0.05,
    ) -> dict[str, Any]:
        """Build calldata for placing an order.

        Args:
            market_acc: Packed marketAcc bytes.
            market_id: Boros market ID.
            side: 0 = long, 1 = short.
            size_wei: Position size in YU wei.
            limit_tick: Limit tick (APR in bps).
            tif: Time in force (0=GTC, 1=IOC, 2=FOK).
            slippage: Slippage tolerance.

        Returns:
            Calldata dictionary.
        """
        path = self.endpoints["build_place_order_calldata"]
        return await self._http(
            "GET",
            path,
            params={
                "marketAcc": market_acc,
                "marketId": market_id,
                "side": side,
                "size": str(size_wei),
                "limitTick": int(limit_tick),
                "tif": tif,
                "slippage": slippage,
            },
        )

    async def build_close_position_calldata(
        self,
        *,
        market_acc: str,
        market_id: int,
        side: int,
        size_wei: int,
        limit_tick: int,
        tif: int = 1,  # IOC for market-like close
    ) -> dict[str, Any]:
        """Build calldata for closing a position.

        Args:
            market_acc: Packed marketAcc bytes.
            market_id: Boros market ID.
            side: Close side (opposite of position side).
            size_wei: Size to close.
            limit_tick: Limit tick.
            tif: Time in force.

        Returns:
            Calldata dictionary.
        """
        path = self.endpoints["build_close_position_calldata"]
        return await self._http(
            "GET",
            path,
            params={
                "marketAcc": market_acc,
                "marketId": int(market_id),
                "side": int(side),
                "size": str(size_wei),
                "limitTick": int(limit_tick),
                "tif": int(tif),
            },
        )

    async def build_cancel_order_calldata(
        self,
        *,
        market_acc: str,
        market_id: int,
        order_ids: list[str] | None = None,
        cancel_all: bool = False,
    ) -> dict[str, Any]:
        """Build calldata for canceling orders.

        Args:
            market_acc: Packed marketAcc bytes.
            market_id: Boros market ID.
            order_ids: Specific order IDs to cancel.
            cancel_all: Cancel all orders on market.

        Returns:
            Calldata dictionary.
        """
        path = self.endpoints["build_cancel_order_calldata"]
        params: dict[str, Any] = {
            "marketAcc": market_acc,
            "marketId": int(market_id),
            "cancelAll": "true" if cancel_all else "false",
        }
        if order_ids and not cancel_all:
            params["orderIds"] = ",".join(str(oid) for oid in order_ids)

        return await self._http("GET", path, params=params)

    async def build_cash_transfer_calldata(
        self,
        *,
        market_id: int,
        amount_wei: int,
        is_deposit: bool = False,
        user_address: str | None = None,
    ) -> dict[str, Any]:
        """Build calldata for transferring cash between isolated and cross margin.

        Matches the upstream behavior:
        - is_deposit=True: cross -> isolated
        - is_deposit=False: isolated -> cross

        Args:
            market_id: Boros market ID.
            amount_wei: Amount in 1e18 cash units.
            is_deposit: True = cross->isolated, False = isolated->cross.
            user_address: User wallet address.

        Notes:
        - Boros uses 1e18 internal "cash" units for this call.
        - This call does NOT send accountId, only userAddress.
        """
        path = self.endpoints["build_cash_transfer_calldata"]
        return await self._http(
            "GET",
            path,
            params={
                "userAddress": user_address or self.user_address,
                "marketId": int(market_id),
                "isDeposit": str(bool(is_deposit)).lower(),
                "amount": str(int(amount_wei)),
            },
        )

    async def build_finalize_withdrawal_calldata(
        self,
        *,
        token_id: int,
        root_address: str,
    ) -> dict[str, Any]:
        """Build calldata for finalizing a vault withdrawal.

        This completes a previously requested withdrawal by transferring
        collateral from MarketHub to the specified root address.

        Args:
            token_id: Boros token ID.
            root_address: Destination wallet address.

        Returns:
            Calldata dictionary with 'to', 'data', 'value' fields.
        """
        path = self.endpoints["build_finalize_withdrawal_calldata"]
        return await self._http(
            "GET",
            path,
            params={
                "rootAddress": root_address,
                "tokenId": int(token_id),
            },
        )
