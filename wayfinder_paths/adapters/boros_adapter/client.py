"""Boros HTTP Client for interacting with Boros API."""

from __future__ import annotations

from typing import Any

import aiohttp
from aiocache import Cache
from loguru import logger

from wayfinder_paths.core.constants.base import DEFAULT_HTTP_HEADERS

from .utils import (
    account_id_from_market_acc,
    build_market_acc_hex,
    is_cross_market_acc,
    market_id_from_market_acc,
    rate_from_tick,
    token_id_from_market_acc,
)

DEFAULT_BASE_URL = "https://api-boros.pendle.finance/apis"
DEFAULT_MARKETS_PAGE_SIZE = 100
MAX_MARKETS_PAGE_SIZE = 200
MAX_ACCOUNT_PAGE_SIZE = 2000
MAX_TRANSFER_LOGS_PAGE_SIZE = 100

# Current Boros Open API mount. The legacy `api.boros.finance/open-api` and
# `/core` mounts are deprecated and scheduled for shutdown by Pendle.
DEFAULT_ENDPOINTS = {
    "assets": "/v1/assets",
    "markets": "/v1/markets",
    "market": "/v1/markets/by-ids",
    "markets_by_ids": "/v1/markets/by-ids",
    "market_chart": "/v1/markets/ohlcv",
    "orderbook": "/v1/markets/order-book",
    "active_positions": "/v1/accounts/active-positions",
    "collaterals": "/v1/accounts/market-acc-infos-by-root",
    "market_acc_infos": "/v1/accounts/market-acc-infos",
    "market_acc_infos_by_root": "/v1/accounts/market-acc-infos-by-root",
    "limit_orders": "/v1/accounts/orders",
    "limit_orders_by_placed_time": "/v1/accounts/orders-by-placed-time",
    "position_update_events": "/v1/accounts/position-update-events",
    "settlement_events": "/v1/accounts/settlement-events",
    "transfer_logs": "/v1/accounts/transfer-logs",
    "gas_balance": "/v1/accounts/gas-balance",
    "gas_consumption_history": "/v1/accounts/gas-consumption-history",
    "agent_expiry_time": "/v1/agents/expiry-time",
    "build_deposit_calldata": "/v1/calldata-builder/user/deposit",
    "build_withdraw_calldata": "/v1/calldata-builder/user/request-withdrawal",
    "build_cancel_withdrawal_calldata": "/v1/calldata-builder/user/cancel-withdrawal",
    "build_finalize_withdrawal_calldata": "https://api.boros.finance/core/v1/calldata/withdraw/finalize",
    "build_approve_agent_calldata": "/v1/calldata-builder/user/approve-agent",
    "build_revoke_agent_calldata": "/v1/calldata-builder/user/revoke-agent",
    "build_vault_pay_treasury_calldata": "/v1/calldata-builder/user/vault-pay-treasury",
    "build_place_order_calldata": "/v1/calldata-builder/agent/place-order",
    "build_place_orders_calldata": "/v1/calldata-builder/agent/place-orders",
    "build_cancel_order_calldata": "/v1/calldata-builder/agent/cancel-orders",
    "build_cash_transfer_calldata": "/v1/calldata-builder/agent/cash-transfer",
    "build_enter_markets_calldata": "/v1/calldata-builder/agent/enter-markets",
    "build_exit_markets_calldata": "/v1/calldata-builder/agent/exit-markets",
    "build_pay_treasury_calldata": "/v1/calldata-builder/agent/pay-treasury",
    "build_add_liquidity_calldata": "/v1/calldata-builder/agent/add-liquidity-to-amm",
    "build_remove_liquidity_calldata": "/v1/calldata-builder/agent/remove-liquidity-from-amm",
    "simulate_place_order": "/v1/simulations/place-order",
    "simulate_deposit": "/v1/simulations/deposit",
    "simulate_withdraw": "/v1/simulations/request-withdrawal",
    "simulate_cash_transfer": "/v1/simulations/cash-transfer",
    "simulate_add_liquidity": "/v1/simulations/add-liquidity-to-amm",
    "simulate_remove_liquidity": "/v1/simulations/remove-liquidity-from-amm",
    "send_txs_bulk_calls": "/v1/send-txs/bulk-calls",
    "send_txs_dedicated_bulk_calls": "/v1/send-txs/dedicated/bulk-calls",
    "send_txs_trace": "/v1/send-txs/trace",
    "send_txs_tx_status": "/v1/send-txs/tx-status",
    "send_txs_tx_status_with_events": "/v1/send-txs/tx-status-with-events",
    # These legacy endpoints do not yet have a one-call replacement in the
    # public OpenAPI. Keep them overrideable for existing vault/rewards flows.
    "amm_summary": "https://api.boros.finance/core/v1/amm/summary",
    "amm_rewards": "https://api.boros.finance/core/v1/amm/rewards",
    "amm_rewards_proof": "https://api.boros.finance/core/v1/merkels/amm_lp_rewards/user",
}


class BorosClient:
    """HTTP client for Boros API.

    Provides low-level HTTP methods for interacting with the Boros API.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
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
        url = (
            path
            if path.startswith(("http://", "https://"))
            else f"{self.base_url}{path}"
        )
        timeout_val = timeout or self.timeout

        async with aiohttp.ClientSession(headers=DEFAULT_HTTP_HEADERS) as session:
            async with session.request(
                method,
                url,
                params=params,
                json=json_body,
                timeout=aiohttp.ClientTimeout(total=timeout_val),
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    message = f"{resp.reason}: {body}"
                    try:
                        err = await resp.json(content_type=None)
                        if isinstance(err, dict):
                            api_msg = err.get("message") or body
                            code = err.get("errorCode") or err.get("statusCode")
                            message = (
                                f"{resp.reason}"
                                f"{f' [{code}]' if code else ''}: {api_msg}"
                            )
                    except Exception:
                        pass
                    raise aiohttp.ClientResponseError(
                        resp.request_info,
                        resp.history,
                        status=resp.status,
                        message=message,
                    )
                if resp.status == 204:
                    return None
                return await resp.json(content_type=None)

    async def get_assets(
        self, *, is_collateral: bool | None = None
    ) -> list[dict[str, Any]]:
        """Get all Boros assets (collateral tokens with addresses).

        Returns:
            List of asset dicts with tokenId, address, symbol, decimals, isCollateral.
        """
        cache_key = f"boros:assets:{self.base_url}:{is_collateral}"
        cached = await self._cache.get(cache_key)
        if cached:
            return cached

        path = self.endpoints["assets"]
        params = None
        if is_collateral is not None:
            params = {"isCollateral": "true" if is_collateral else "false"}
        data = await self._http("GET", path, params=params)
        assets: list[dict[str, Any]] = (
            data.get("assets") or data.get("results") or data.get("data") or []
        )
        await self._cache.set(cache_key, assets, ttl=3600)  # Cache for 1 hour
        return assets

    async def list_markets_page(
        self,
        *,
        is_whitelisted: bool | None = True,
        is_matured: bool | None = None,
        limit: int = DEFAULT_MARKETS_PAGE_SIZE,
        resume_token: str | None = None,
    ) -> dict[str, Any]:
        """Fetch one cursor-paginated page from `/v1/markets`."""
        limit = self._clamp_int(limit, DEFAULT_MARKETS_PAGE_SIZE, MAX_MARKETS_PAGE_SIZE)
        params: dict[str, Any] = {"limit": limit}
        if is_whitelisted is not None:
            params["isUiWhitelisted"] = "true" if is_whitelisted else "false"
        if is_matured is not None:
            params["isMatured"] = "true" if is_matured else "false"
        if resume_token:
            params["resumeToken"] = resume_token

        data = await self._http("GET", self.endpoints["markets"], params=params)
        if isinstance(data, dict):
            return data
        return {"results": data if isinstance(data, list) else [], "resumeToken": None}

    async def list_markets(
        self,
        *,
        is_whitelisted: bool | None = True,
        skip: int = 0,
        limit: int = 100,
        resume_token: str | None = None,
        is_matured: bool | None = None,
    ) -> list[dict[str, Any]]:
        """List available Boros markets.

        Args:
            is_whitelisted: Filter by whitelisted markets.
            skip: Number of markets to skip.
            limit: Maximum markets to return.

        Returns:
            List of market dictionaries.
        """
        limit = self._clamp_int(limit, DEFAULT_MARKETS_PAGE_SIZE, MAX_MARKETS_PAGE_SIZE)
        skip = max(0, self._coerce_int(skip, 0))
        cache_key = (
            "boros:markets:"
            f"{self.base_url}:{is_whitelisted}:{is_matured}:{skip}:{limit}:{resume_token}"
        )
        cached = await self._cache.get(cache_key)
        if cached:
            return cached

        # `skip` is retained for older adapter callers. The current Boros API is
        # cursor-based, so emulate offset pagination by walking pages and filling
        # the requested limit from subsequent cursors when skip cuts through a page.
        remaining_skip = skip
        token = resume_token
        markets: list[dict[str, Any]] = []
        while len(markets) < limit:
            page = await self.list_markets_page(
                is_whitelisted=is_whitelisted,
                is_matured=is_matured,
                limit=limit,
                resume_token=token,
            )
            token = page.get("resumeToken")
            page_markets = (
                page.get("markets") or page.get("results") or page.get("data") or []
            )
            if remaining_skip:
                if remaining_skip >= len(page_markets):
                    remaining_skip -= len(page_markets)
                    if not token:
                        break
                    continue
                page_markets = page_markets[remaining_skip:]
                remaining_skip = 0
            needed = limit - len(markets)
            markets.extend(page_markets[:needed])
            if len(markets) >= limit or not token or not page_markets:
                break
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

        # Raw list.
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

        path = self.endpoints["markets_by_ids"]
        market: dict[str, Any] | None = None
        try:
            payload = await self._http(
                "GET", path, params={"marketIds": str(int(market_id))}
            )
            market = self._extract_market_from_payload(payload, market_id)
        except Exception:
            market = None

        # Fallback: paginate markets (Boros currently enforces limit<=200).
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
        include_amm: bool = True,
    ) -> dict[str, Any]:
        """Get order book for a market.

        Args:
            market_id: Boros market ID.
            tick_size: Tick size for aggregation.

        Returns:
            Order book with long/short sides.
        """
        path = self.endpoints["orderbook"]
        params = {
            "marketId": int(market_id),
            "tickSize": tick_size,
            "includeAmm": "true" if include_amm else "false",
        }
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
        """Get a legacy-shaped collateral summary for user.

        Args:
            user_address: User wallet address (defaults to client's user_address).
            account_id: Account ID (defaults to client's account_id).

        Returns:
            Collateral summary with positions.
        """
        root = user_address or self.user_address
        if not root:
            raise ValueError("user_address is required")

        data = await self.get_market_acc_infos_by_root(root=root)
        return self._market_acc_infos_to_collaterals(
            data,
            account_id=int(account_id if account_id is not None else self.account_id),
        )

    async def get_market_acc_infos_by_root(self, *, root: str) -> dict[str, Any]:
        """Get all market account infos for a root wallet."""
        return await self._http(
            "GET",
            self.endpoints["market_acc_infos_by_root"],
            params={"root": root},
        )

    async def get_market_acc_infos(self, market_accs: list[str]) -> dict[str, Any]:
        """Get market account infos for up to 100 packed marketAcc values."""
        if not market_accs:
            return {"results": [], "syncStatus": None}
        return await self._http(
            "POST",
            self.endpoints["market_acc_infos"],
            json_body={"marketAccs": [str(acc) for acc in market_accs[:100]]},
        )

    async def get_active_positions(
        self,
        user_address: str | None = None,
        account_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get active positions using the current `/v1/accounts/active-positions` endpoint."""
        root = user_address or self.user_address
        if not root:
            raise ValueError("user_address is required")
        data = await self._http(
            "GET",
            self.endpoints["active_positions"],
            params={
                "root": root,
                "accountId": int(
                    account_id if account_id is not None else self.account_id
                ),
            },
        )
        if isinstance(data, dict):
            return data.get("results") or data.get("positions") or []
        return data if isinstance(data, list) else []

    async def get_gas_balance(self, user_address: str | None = None) -> dict[str, Any]:
        """Get current Send Txs Bot gas balance in USD."""
        root = user_address or self.user_address
        if not root:
            raise ValueError("user_address is required")
        return await self._http(
            "GET", self.endpoints["gas_balance"], params={"root": root}
        )

    async def get_gas_consumption_history(
        self,
        user_address: str | None = None,
        *,
        limit: int = 100,
        resume_token: str | None = None,
    ) -> dict[str, Any]:
        """Get cursor-paginated gas usage history."""
        root = user_address or self.user_address
        if not root:
            raise ValueError("user_address is required")
        params: dict[str, Any] = {
            "root": root,
            "limit": self._clamp_int(limit, 100, MAX_TRANSFER_LOGS_PAGE_SIZE),
        }
        if resume_token:
            params["resumeToken"] = resume_token
        return await self._http(
            "GET", self.endpoints["gas_consumption_history"], params=params
        )

    async def get_amm_summary(
        self,
        *,
        account: str | None = None,
    ) -> dict[str, Any]:
        path = self.endpoints["amm_summary"]
        params = {"account": str(account)} if account else None
        return await self._http("GET", path, params=params)

    async def get_amm_rewards(
        self,
        *,
        user_address: str | None = None,
    ) -> dict[str, Any]:
        path = self.endpoints["amm_rewards"]
        params = {"user": user_address or self.user_address}
        return await self._http("GET", path, params=params)

    async def get_amm_rewards_proof(
        self,
        *,
        user_address: str | None = None,
    ) -> dict[str, Any]:
        user = user_address or self.user_address
        if not user:
            raise ValueError("user_address is required")
        path = f"{self.endpoints['amm_rewards_proof']}/{user}"
        return await self._http("GET", path)

    async def get_open_orders(
        self,
        user_address: str | None = None,
        *,
        limit: int = 50,
        account_id: int | None = None,
        market_id: int | None = None,
        resume_token: str | None = None,
        is_active: bool | None = True,
        order_type: int | list[int] | None = None,
        by_placed_time: bool = False,
    ) -> list[dict[str, Any]]:
        """Get open limit orders.

        Args:
            user_address: User wallet address.
            limit: Maximum orders to return.

        Returns:
            List of open orders.
        """
        root = user_address or self.user_address
        if not root:
            raise ValueError("user_address is required")

        path = (
            self.endpoints["limit_orders_by_placed_time"]
            if by_placed_time
            else self.endpoints["limit_orders"]
        )
        params: dict[str, Any] = {
            "root": root,
            "accountId": int(account_id if account_id is not None else self.account_id),
            "limit": self._clamp_int(limit, 50, MAX_ACCOUNT_PAGE_SIZE),
        }
        if market_id is not None and not by_placed_time:
            params["marketId"] = int(market_id)
        if resume_token:
            params["resumeToken"] = resume_token
        if is_active is not None and not by_placed_time:
            params["isActive"] = "true" if is_active else "false"
        if order_type is not None and not by_placed_time:
            if isinstance(order_type, list):
                params["orderType"] = ",".join(str(int(v)) for v in order_type)
            else:
                params["orderType"] = int(order_type)
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
        market_acc: str | None = None,
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
        root = user_address or self.user_address
        if not root and not market_acc:
            raise ValueError("user_address is required")
        market_acc = market_acc or build_market_acc_hex(
            address=str(root),
            account_id=int(account_id if account_id is not None else self.account_id),
            token_id=int(token_id),
            market_id=int(market_id),
        )
        return await self._http(
            "POST",
            self.endpoints["build_deposit_calldata"],
            json_body={
                "marketAcc": market_acc,
                "amount": str(amount_wei),
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
        root = user_address or self.user_address
        if not root:
            raise ValueError("user_address is required")
        return await self._http(
            "POST",
            self.endpoints["build_withdraw_calldata"],
            json_body={
                "root": root,
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
        limit_tick: int | None = None,
        tif: int = 0,
        slippage: float = 0.05,
        rate: float | None = None,
        amm_id: int | None = None,
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
        if rate is None and limit_tick is not None:
            rate = await self._rate_for_limit_tick(market_id, limit_tick)
        body: dict[str, Any] = {
            "marketAcc": market_acc,
            "marketId": int(market_id),
            "side": int(side),
            "size": str(size_wei),
            "tif": int(tif),
            "slippage": float(slippage),
        }
        if rate is not None:
            body["rate"] = float(rate)
        if amm_id is not None:
            body["ammId"] = int(amm_id)
        return await self._http(
            "POST",
            self.endpoints["build_place_order_calldata"],
            json_body=body,
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
        return await self.build_place_order_calldata(
            market_acc=market_acc,
            market_id=market_id,
            side=side,
            size_wei=size_wei,
            limit_tick=limit_tick,
            tif=tif,
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
        body: dict[str, Any] = {
            "markets": [
                {
                    "marketAcc": market_acc,
                    "marketId": int(market_id),
                    "cancelAll": bool(cancel_all),
                    "orderIds": [str(oid) for oid in (order_ids or [])],
                }
            ]
        }
        return await self._http(
            "POST",
            self.endpoints["build_cancel_order_calldata"],
            json_body=body,
        )

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
        direction = "CROSS_TO_ISOLATED" if is_deposit else "ISOLATED_TO_CROSS"
        return await self._http(
            "POST",
            self.endpoints["build_cash_transfer_calldata"],
            json_body={
                "accountId": int(self.account_id),
                "marketId": int(market_id),
                "direction": direction,
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

    async def simulate_place_order(
        self,
        *,
        market_acc: str,
        market_id: int,
        side: int,
        size_wei: int,
        tif: int,
        limit_tick: int | None = None,
        rate: float | None = None,
        slippage: float | None = None,
        amm_id: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "marketAcc": market_acc,
            "marketId": int(market_id),
            "side": int(side),
            "size": str(int(size_wei)),
            "tif": int(tif),
        }
        if rate is not None:
            body["rate"] = float(rate)
        elif limit_tick is not None:
            body["rate"] = await self._rate_for_limit_tick(market_id, limit_tick)
        if slippage is not None:
            body["slippage"] = float(slippage)
        if amm_id is not None:
            body["ammId"] = int(amm_id)
        return await self._http(
            "POST", self.endpoints["simulate_place_order"], json_body=body
        )

    async def simulate_deposit(
        self,
        *,
        market_acc: str,
        amount_wei: int,
    ) -> dict[str, Any]:
        return await self._http(
            "POST",
            self.endpoints["simulate_deposit"],
            json_body={"marketAcc": market_acc, "amount": str(int(amount_wei))},
        )

    async def simulate_withdraw(
        self,
        *,
        token_id: int,
        amount_wei: int,
        user_address: str | None = None,
    ) -> dict[str, Any]:
        root = user_address or self.user_address
        if not root:
            raise ValueError("user_address is required")
        return await self._http(
            "POST",
            self.endpoints["simulate_withdraw"],
            json_body={
                "root": root,
                "tokenId": int(token_id),
                "amount": str(int(amount_wei)),
            },
        )

    async def simulate_cash_transfer(
        self,
        *,
        market_id: int,
        amount_wei: int,
        is_deposit: bool = False,
        user_address: str | None = None,
        account_id: int | None = None,
    ) -> dict[str, Any]:
        root = user_address or self.user_address
        if not root:
            raise ValueError("user_address is required")
        direction = "CROSS_TO_ISOLATED" if is_deposit else "ISOLATED_TO_CROSS"
        return await self._http(
            "POST",
            self.endpoints["simulate_cash_transfer"],
            json_body={
                "root": root,
                "accountId": int(
                    account_id if account_id is not None else self.account_id
                ),
                "marketId": int(market_id),
                "direction": direction,
                "amount": str(int(amount_wei)),
            },
        )

    async def simulate_add_liquidity(
        self,
        *,
        market_id: int,
        net_cash_in_wei: int,
        user_address: str | None = None,
        account_id: int | None = None,
    ) -> dict[str, Any]:
        root = user_address or self.user_address
        if not root:
            raise ValueError("user_address is required")
        return await self._http(
            "POST",
            self.endpoints["simulate_add_liquidity"],
            json_body={
                "root": root,
                "accountId": int(
                    account_id if account_id is not None else self.account_id
                ),
                "marketId": int(market_id),
                "netCashIn": str(int(net_cash_in_wei)),
            },
        )

    async def simulate_remove_liquidity(
        self,
        *,
        market_id: int,
        lp_to_remove_wei: int,
        user_address: str | None = None,
        account_id: int | None = None,
    ) -> dict[str, Any]:
        root = user_address or self.user_address
        if not root:
            raise ValueError("user_address is required")
        return await self._http(
            "POST",
            self.endpoints["simulate_remove_liquidity"],
            json_body={
                "root": root,
                "accountId": int(
                    account_id if account_id is not None else self.account_id
                ),
                "marketId": int(market_id),
                "lpToRemove": str(int(lp_to_remove_wei)),
            },
        )

    async def submit_agent_calls(
        self,
        *,
        datas: list[dict[str, Any]],
        dedicated: bool = True,
        require_success: bool = False,
        simulate: bool = False,
        skip_receipt: bool = False,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Submit already agent-signed calldata through Send Txs.

        Advanced/non-default escape hatch only. The Wayfinder product execution
        path should prefer wallet-signed Boros user endpoints wherever possible.

        `datas` entries must include `agent`, `message`, `signature`, and
        `calldata`. This client deliberately does not create agent signatures.
        """
        body: dict[str, Any] = {
            "datas": datas,
            "requireSuccess": bool(require_success),
        }
        if dedicated:
            body["simulate"] = bool(simulate)
            path = self.endpoints["send_txs_dedicated_bulk_calls"]
        else:
            body["skipReceipt"] = bool(skip_receipt)
            path = self.endpoints["send_txs_bulk_calls"]
        return await self._http("POST", path, json_body=body)

    @staticmethod
    def _coerce_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    @classmethod
    def _clamp_int(cls, value: Any, default: int, maximum: int) -> int:
        coerced = cls._coerce_int(value, default)
        if coerced <= 0:
            return int(default)
        return min(coerced, int(maximum))

    async def _rate_for_limit_tick(self, market_id: int, limit_tick: int) -> float:
        market = await self.get_market(int(market_id))
        tick_step = (market.get("imData") or {}).get("tickStep") or market.get(
            "tickStep"
        )
        return rate_from_tick(int(limit_tick), self._coerce_int(tick_step, 1))

    @staticmethod
    def _market_acc_infos_to_collaterals(
        data: dict[str, Any],
        *,
        account_id: int,
    ) -> dict[str, Any]:
        """Normalize current marketAcc infos into the legacy adapter shape."""
        grouped: dict[int, dict[str, Any]] = {}
        for info in data.get("results") or []:
            market_acc = str(info.get("marketAcc") or "")
            token_id = token_id_from_market_acc(market_acc)
            if token_id is None:
                continue
            if account_id_from_market_acc(market_acc) not in (None, int(account_id)):
                continue

            entry = grouped.setdefault(
                token_id,
                {
                    "tokenId": token_id,
                    "crossPosition": {},
                    "isolatedPositions": [],
                },
            )
            position = {
                "marketAcc": market_acc,
                "availableBalance": info.get("totalCash") or "0",
                "netBalance": info.get("netBalance") or info.get("totalCash") or "0",
                "marketPositions": [
                    BorosClient._position_to_legacy(pos, token_id, market_acc)
                    for pos in (info.get("positions") or [])
                ],
                "raw": info,
            }
            if is_cross_market_acc(market_acc):
                entry["crossPosition"] = position
            else:
                entry["isolatedPositions"].append(position)

        return {
            "collaterals": list(grouped.values()),
            "raw": data,
            "syncStatus": data.get("syncStatus"),
        }

    @staticmethod
    def _position_to_legacy(
        position: dict[str, Any],
        token_id: int | None,
        market_acc: str,
    ) -> dict[str, Any]:
        signed_size = position.get("signedSize")
        try:
            size_wei = abs(int(signed_size or position.get("sizeWei") or 0))
        except (TypeError, ValueError):
            size_wei = 0
        market_id = position.get("marketId") or market_id_from_market_acc(market_acc)
        return {
            **position,
            "marketId": market_id,
            "tokenId": token_id,
            "sizeWei": str(size_wei),
            "notionalSize": str(size_wei),
            "pnl": {
                "unrealisedPnl": position.get("unrealisedPnl") or 0,
                "rateSettlementPnl": position.get("settlementPnl") or 0,
            },
        }
