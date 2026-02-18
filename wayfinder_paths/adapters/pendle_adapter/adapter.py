from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from eth_utils import to_checksum_address

from wayfinder_paths.adapters.multicall_adapter.adapter import MulticallAdapter
from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.constants.erc20_abi import ERC20_ABI
from wayfinder_paths.core.utils.tokens import (
    ensure_allowance,
    get_token_balance,
)
from wayfinder_paths.core.utils.transaction import send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

# Available fields for historical data endpoint
PENDLE_HISTORY_FIELDS = [
    "timestamp",
    "baseApy",
    "impliedApy",
    "lastEpochVotes",
    "lpPrice",
    "lpRewardApy",
    "maxApy",
    "pendleApy",
    "ptPrice",
    "swapFeeApy",
    "syPrice",
    "totalPt",
    "totalSupply",
    "totalSy",
    "totalTvl",
    "tradingVolume",
    "tvl",
    "underlyingApy",
    "underlyingInterestApy",
    "underlyingRewardApy",
    "voterApr",
    "ytFloatingApy",
    "ytPrice",
]

# Default fields to fetch for historical data
DEFAULT_HISTORY_FIELDS = (
    "ptPrice,ytPrice,impliedApy,underlyingApy,tvl,totalTvl,lpPrice,syPrice"
)

# Convenience mapping for Pendle-supported chains in the Wayfinder SDK.
# Pendle also supports Optimism (10), Sonic (146), Mantle (5000), Berachain (80094)
# but those are not yet in the SDK's supported-chains table.
PENDLE_CHAIN_IDS: dict[str, int] = {
    "ethereum": 1,
    "bsc": 56,
    "arbitrum": 42161,
    "base": 8453,
    "hyperevm": 999,
    "plasma": 9745,
}

PENDLE_DEFAULT_DEPLOYMENTS_BASE_URL = "https://raw.githubusercontent.com/pendle-finance/pendle-core-v2-public/main/deployments"

PENDLE_ROUTER_STATIC_ABI: list[dict[str, Any]] = [
    {
        "inputs": [{"internalType": "address", "name": "market", "type": "address"}],
        "name": "getLpToSyRate",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "market", "type": "address"}],
        "name": "getPtToSyRate",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "market", "type": "address"}],
        "name": "getLpToAssetRate",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "market", "type": "address"}],
        "name": "getPtToAssetRate",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

ChainLike = int | str


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _parse_iso8601(s: str) -> datetime:
    # Handles "2024-03-28T00:00:00.000Z"
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _split_pendle_id(pendle_id_or_address: str) -> tuple[int | None, str]:
    """
    Pendle APIs sometimes return "id" fields like "42161-0xabc...".
    This returns (chain_id, address). If it's already an address, chain_id is None.
    """
    if not pendle_id_or_address:
        return None, pendle_id_or_address
    if "-" not in pendle_id_or_address:
        return None, pendle_id_or_address
    chain_str, addr = pendle_id_or_address.split("-", 1)
    try:
        return int(chain_str), addr
    except ValueError:
        return None, addr


def _as_address(pendle_id_or_address: str) -> str:
    return _split_pendle_id(pendle_id_or_address)[1]


def _as_chain_id(chain: ChainLike) -> int:
    if isinstance(chain, int):
        return chain
    key = chain.strip().lower()
    if key in PENDLE_CHAIN_IDS:
        return PENDLE_CHAIN_IDS[key]
    # Allow passing "42161" as a string
    try:
        return int(key)
    except ValueError as exc:
        raise ValueError(
            f"Unknown chain '{chain}'. Use int chainId or one of {sorted(PENDLE_CHAIN_IDS)}"
        ) from exc


def _compact_params(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None}


async def _gather_limited(
    coro_factories: Sequence[Callable[[], Awaitable[Any]]],
    *,
    concurrency: int = 8,
) -> list[Any]:
    """
    Run coroutine factories with a concurrency limit.
    Each entry of `coro_factories` is a zero-arg callable that returns an awaitable.
    """

    sem = asyncio.Semaphore(concurrency)
    results: list[Any] = [None] * len(coro_factories)

    async def runner(i: int, fn: Callable[[], Awaitable[Any]]) -> None:
        async with sem:
            results[i] = await fn()

    await asyncio.gather(*(runner(i, fn) for i, fn in enumerate(coro_factories)))
    return results


class PendleAdapter(BaseAdapter):
    adapter_type: str = "PENDLE"

    MAX_UINT256 = 2**256 - 1

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
        signing_callback: Callable | None = None,
    ) -> None:
        super().__init__("pendle_adapter", config)
        cfg = config or {}
        adapter_cfg = cfg.get("pendle_adapter") or {}

        default_base = "https://api-v2.pendle.finance/core"
        resolved_base = base_url or adapter_cfg.get("base_url") or default_base
        self.base_url = str(resolved_base).rstrip("/")
        self.client = client
        self.timeout = float(adapter_cfg.get("timeout", timeout))

        self._owns_client = False
        self.signing_callback = signing_callback
        self.wallet = cfg.get("strategy_wallet") or {}
        self.max_retries = int(adapter_cfg.get("max_retries", 3))
        self.retry_backoff_seconds = float(
            adapter_cfg.get("retry_backoff_seconds", 0.75)
        )
        self.deployments_base_url = str(
            adapter_cfg.get("deployments_base_url")
            or PENDLE_DEFAULT_DEPLOYMENTS_BASE_URL
        ).rstrip("/")
        self._deployments_cache: dict[int, dict[str, Any]] = {}

    async def close(self) -> None:
        if self._owns_client and self.client is not None:
            await self.client.aclose()
            self.client = None
            self._owns_client = False

    # ---------------------------
    # Execution helpers
    # ---------------------------

    def _strategy_address(self) -> str:
        addr = None
        if isinstance(self.wallet, dict):
            addr = self.wallet.get("address")
        elif isinstance(self.wallet, str):
            addr = self.wallet
        if not addr:
            raise ValueError("strategy_wallet address is required for Pendle execution")
        return to_checksum_address(addr)

    async def _send_tx(self, tx: dict[str, Any]) -> tuple[bool, Any]:
        if self.signing_callback is None:
            raise ValueError("signing_callback is required for tx execution")
        txn_hash = await send_transaction(tx, self.signing_callback)
        return True, txn_hash

    # ---------------------------
    # Multicall helpers
    # ---------------------------

    @staticmethod
    def _chunks(seq: list[Any], n: int) -> list[list[Any]]:
        return [seq[i : i + n] for i in range(0, len(seq), n)]

    async def _multicall_uint256_chunked(
        self,
        *,
        multicall: MulticallAdapter,
        calls: list[Any],
        chunk_size: int,
    ) -> list[int | None]:
        """
        Execute multicall and decode each return as uint256.

        If a chunk reverts, fall back to executing calls one-by-one so we can salvage
        partial results (returning None for failed calls).
        """
        out: list[int | None] = []
        for chunk in self._chunks(calls, max(1, int(chunk_size))):
            if not chunk:
                continue
            try:
                res = await multicall.aggregate(chunk)
                out.extend([multicall.decode_uint256(b) for b in res.return_data])
            except Exception:  # noqa: BLE001 - fall back to individual calls
                for call in chunk:
                    try:
                        r = await multicall.aggregate([call])
                        if r.return_data:
                            out.append(multicall.decode_uint256(r.return_data[0]))
                        else:
                            out.append(None)
                    except Exception:  # noqa: BLE001
                        out.append(None)
        return out

    @staticmethod
    def _rate_limit_from_headers(headers: httpx.Headers) -> dict[str, int | None]:
        def _get_int(name: str) -> int | None:
            value = headers.get(name)
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        return {
            "ratelimitLimit": _get_int("x-ratelimit-limit"),
            "ratelimitRemaining": _get_int("x-ratelimit-remaining"),
            "ratelimitReset": _get_int("x-ratelimit-reset"),
            "ratelimitWeeklyLimit": _get_int("x-ratelimit-weekly-limit"),
            "ratelimitWeeklyRemaining": _get_int("x-ratelimit-weekly-remaining"),
            "ratelimitWeeklyReset": _get_int("x-ratelimit-weekly-reset"),
            "computingUnit": _get_int("x-computing-unit"),
        }

    @staticmethod
    def _decode_response_payload(response: httpx.Response) -> Any:
        try:
            return response.json()
        except Exception:  # noqa: BLE001
            return response.text

    def _attach_meta(self, payload: Any, response: httpx.Response) -> Any:
        rate_limit = self._rate_limit_from_headers(response.headers)
        if isinstance(payload, dict):
            if "rateLimit" not in payload:
                payload["rateLimit"] = rate_limit
            else:
                payload["_rateLimit"] = rate_limit
            return payload
        return {"data": payload, "rateLimit": rate_limit}

    async def _request_raw(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None

        for attempt in range(1, max(1, self.max_retries) + 1):
            try:
                if self.client is not None:
                    return await self.client.request(
                        method,
                        url,
                        params=params,
                        json=json,
                        timeout=self.timeout,
                    )
                async with httpx.AsyncClient() as client:
                    return await client.request(
                        method,
                        url,
                        params=params,
                        json=json,
                        timeout=self.timeout,
                    )
            except httpx.RequestError as exc:
                last_exc = exc
                if attempt >= max(1, self.max_retries):
                    raise
                await asyncio.sleep(self.retry_backoff_seconds * (2 ** (attempt - 1)))

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("unreachable")

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._request_raw("GET", path, params=params)
        response.raise_for_status()
        payload = self._decode_response_payload(response)
        return self._attach_meta(payload, response)

    async def _post(self, path: str, json: dict[str, Any]) -> Any:
        response = await self._request_raw("POST", path, json=json)
        response.raise_for_status()
        payload = self._decode_response_payload(response)
        return self._attach_meta(payload, response)

    # ---------------------------
    # Pendle API endpoints
    # ---------------------------

    async def fetch_markets(
        self,
        chain_id: int | None = None,
        is_active: bool | None = None,
        ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Fetch whitelisted markets with metadata.

        Endpoint: `{base_url}/v1/markets/all` (default `base_url` ends with `/core`)
          { "markets": [ ... ] }
        """
        params: dict[str, Any] = {}
        if chain_id is not None:
            params["chainId"] = int(chain_id)
        if is_active is not None:
            params["isActive"] = str(bool(is_active)).lower()
        if ids:
            params["ids"] = ",".join(ids)
        data = await self._get("/v1/markets/all", params=params or None)
        return data if isinstance(data, dict) else {"data": data}

    async def fetch_market_snapshot(
        self,
        chain_id: int,
        market_address: str,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        params = {"timestamp": timestamp} if timestamp else None
        data = await self._get(
            f"/v2/{int(chain_id)}/markets/{market_address}/data", params=params
        )
        return data if isinstance(data, dict) else {"data": data}

    async def fetch_market_history(
        self,
        chain_id: int,
        market_address: str,
        time_frame: Literal["hour", "day", "week"] = "day",
        timestamp_start: str | None = None,
        timestamp_end: str | None = None,
        fields: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "time_frame": time_frame,
            "fields": fields or DEFAULT_HISTORY_FIELDS,
        }
        if timestamp_start:
            params["timestamp_start"] = timestamp_start
        if timestamp_end:
            params["timestamp_end"] = timestamp_end

        data = await self._get(
            f"/v2/{int(chain_id)}/markets/{market_address}/historical-data",
            params=params,
        )
        return data if isinstance(data, dict) else {"data": data}

    async def fetch_ohlcv_prices(
        self,
        chain_id: int,
        token_address: str,
        start: str | None = None,
        end: str | None = None,
        interval: str | None = None,
    ) -> dict[str, Any]:
        params = _compact_params({"start": start, "end": end, "interval": interval})
        data = await self._get(
            f"/v4/{int(chain_id)}/prices/{token_address}/ohlcv", params=params or None
        )
        return data if isinstance(data, dict) else {"data": data}

    async def fetch_asset_prices(self) -> dict[str, Any]:
        data = await self._get("/v1/prices/assets")
        return data if isinstance(data, dict) else {"data": data}

    async def fetch_swapping_prices(
        self, chain_id: int, market_address: str
    ) -> dict[str, Any]:
        data = await self._get(
            f"/v1/sdk/{int(chain_id)}/markets/{market_address}/swapping-prices"
        )
        return data if isinstance(data, dict) else {"data": data}

    # ---------------------------
    # Helpful SDK & discovery
    # ---------------------------

    async def fetch_supported_chain_ids(self) -> dict[str, Any]:
        data = await self._get("/v1/chains")
        return data if isinstance(data, dict) else {"data": data}

    async def fetch_supported_aggregators(self, chain: ChainLike) -> dict[str, Any]:
        chain_id = _as_chain_id(chain)
        data = await self._get(f"/v1/sdk/{chain_id}/supported-aggregators")
        return data if isinstance(data, dict) else {"data": data}

    async def fetch_positions_database(
        self,
        *,
        user: str,
        filter_usd: float | None = None,
    ) -> dict[str, Any]:
        """
        Fast, indexed user positions across chains (claimables cached ~24h).

        Endpoint: /v1/dashboard/positions/database/{user}
        """
        params: dict[str, Any] = {}
        if filter_usd is not None:
            params["filterUsd"] = float(filter_usd)
        data = await self._get(
            f"/v1/dashboard/positions/database/{user}", params=params or None
        )
        return data if isinstance(data, dict) else {"data": data}

    # ---------------------------------------
    # Limit Orders
    # ---------------------------------------

    async def fetch_taker_limit_orders(
        self,
        *,
        chain: ChainLike,
        yt: str,
        order_type: int,
        skip: int | None = None,
        limit: int | None = None,
        sort_by: str | None = None,
        sort_order: Literal["asc", "desc"] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "chainId": _as_chain_id(chain),
            "yt": yt,
            "type": int(order_type),
        }
        if skip is not None:
            params["skip"] = int(skip)
        if limit is not None:
            params["limit"] = int(limit)
        if sort_by is not None:
            params["sortBy"] = str(sort_by)
        if sort_order is not None:
            params["sortOrder"] = sort_order

        data = await self._get("/v1/limit-orders/takers/limit-orders", params=params)
        return data if isinstance(data, dict) else {"data": data}

    async def generate_maker_limit_order_data(
        self,
        *,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        data = await self._post(
            "/v1/limit-orders/makers/generate-limit-order-data", json=payload
        )
        return data if isinstance(data, dict) else {"data": data}

    async def post_maker_limit_order(
        self,
        *,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        data = await self._post("/v1/limit-orders/makers/limit-orders", json=payload)
        return data if isinstance(data, dict) else {"data": data}

    async def fetch_maker_limit_orders(
        self,
        *,
        chain: ChainLike,
        maker: str,
        yt: str | None = None,
        order_type: int | None = None,
        is_active: bool | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "chainId": _as_chain_id(chain),
            "maker": maker,
        }
        if yt is not None:
            params["yt"] = yt
        if order_type is not None:
            params["type"] = int(order_type)
        if is_active is not None:
            params["isActive"] = self._bool_q(bool(is_active))
        data = await self._get("/v1/limit-orders/makers/limit-orders", params=params)
        return data if isinstance(data, dict) else {"data": data}

    # ---------------------------------------
    # Deployments (address discovery)
    # ---------------------------------------

    async def fetch_core_deployments(
        self,
        *,
        chain: ChainLike,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        chain_id = _as_chain_id(chain)
        if not force_refresh and chain_id in self._deployments_cache:
            return self._deployments_cache[chain_id]

        url = f"{self.deployments_base_url}/{chain_id}-core.json"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = self._decode_response_payload(resp)

        if not isinstance(data, dict):
            raise ValueError(f"Unexpected deployments payload for chain {chain_id}")

        self._deployments_cache[chain_id] = data
        return data

    async def get_router_static_address(self, *, chain: ChainLike) -> str:
        deployments = await self.fetch_core_deployments(chain=chain)
        addr = deployments.get("routerStatic")
        if not isinstance(addr, str) or not addr:
            raise ValueError("routerStatic not found in Pendle deployments")
        return to_checksum_address(addr)

    async def get_limit_router_address(self, *, chain: ChainLike) -> str:
        deployments = await self.fetch_core_deployments(chain=chain)
        addr = deployments.get("limitRouter")
        if not isinstance(addr, str) or not addr:
            raise ValueError("limitRouter not found in Pendle deployments")
        return to_checksum_address(addr)

    # ---------------------------------------
    # RouterStatic (off-chain spot-rate checks)
    # ---------------------------------------

    async def router_static_rates(
        self,
        *,
        chain: ChainLike,
        market: str,
    ) -> dict[str, Any]:
        chain_id = _as_chain_id(chain)
        router_static = await self.get_router_static_address(chain=chain_id)
        market_checksum = to_checksum_address(market)

        async with web3_from_chain_id(chain_id) as web3:
            contract = web3.eth.contract(
                address=to_checksum_address(router_static),
                abi=PENDLE_ROUTER_STATIC_ABI,
            )
            lp_to_sy = await contract.functions.getLpToSyRate(market_checksum).call()
            pt_to_sy = await contract.functions.getPtToSyRate(market_checksum).call()
            lp_to_asset = await contract.functions.getLpToAssetRate(
                market_checksum
            ).call()
            pt_to_asset = await contract.functions.getPtToAssetRate(
                market_checksum
            ).call()

        return {
            "chainId": int(chain_id),
            "routerStatic": router_static,
            "market": market_checksum,
            "rates": {
                "lpToSy": int(lp_to_sy),
                "ptToSy": int(pt_to_sy),
                "lpToAsset": int(lp_to_asset),
                "ptToAsset": int(pt_to_asset),
            },
        }

    async def sdk_swap_v2(
        self,
        *,
        chain: ChainLike,
        market_address: str,
        receiver: str | None,
        slippage: float,
        token_in: str,
        token_out: str,
        amount_in: str,
        enable_aggregator: bool = False,
        aggregators: Sequence[str] | str | None = None,
        additional_data: Sequence[str] | str | None = None,
        need_scale: bool | None = None,
    ) -> dict[str, Any]:
        """
        Build calldata to swap tokenIn -> tokenOut via Pendle Hosted SDK.
        Uses /v2/sdk/{chainId}/markets/{market}/swap (GET with query params).

        Returns a payload that typically includes:
          - tx: { to, data, value, from }
          - tokenApprovals: [{ token, amount }, ...]
          - data: { amountOut, priceImpact, impliedApy?, effectiveApy? } (depending on additionalData)
        """
        chain_id = _as_chain_id(chain)

        # API wants comma-separated strings for aggregators + additionalData
        if isinstance(aggregators, (list, tuple)):
            aggregators_q = ",".join(aggregators)
        else:
            aggregators_q = aggregators

        if isinstance(additional_data, (list, tuple)):
            additional_data_q = ",".join(additional_data)
        else:
            additional_data_q = additional_data

        params = _compact_params(
            {
                "receiver": receiver,
                "slippage": slippage,
                "enableAggregator": str(bool(enable_aggregator)).lower(),
                "aggregators": aggregators_q,
                "tokenIn": token_in,
                "tokenOut": token_out,
                "amountIn": amount_in,
                "additionalData": additional_data_q,
                "needScale": str(bool(need_scale)).lower()
                if need_scale is not None
                else None,
            }
        )

        data = await self._get(
            f"/v2/sdk/{chain_id}/markets/{market_address}/swap", params=params
        )
        return data if isinstance(data, dict) else {"data": data}

    # ---------------------------------------
    # Hosted SDK: Universal Convert
    # ---------------------------------------

    @staticmethod
    def _bool_q(v: bool) -> str:
        return "true" if v else "false"

    @staticmethod
    def _coerce_int(value: Any, *, default: int = 0) -> int:
        if value is None:
            return default
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                if value.startswith("0x"):
                    return int(value, 16)
                return int(value)
            except ValueError:
                return default
        return default

    @classmethod
    def _select_best_convert_route(cls, convert: dict[str, Any]) -> dict[str, Any]:
        routes = convert.get("routes") or []
        if not isinstance(routes, list) or not routes:
            raise ValueError("Pendle convert response missing routes")

        def _score(route: dict[str, Any]) -> int:
            outputs = route.get("outputs") or []
            if not isinstance(outputs, list) or not outputs:
                return 0
            score = 0
            for out in outputs:
                if not isinstance(out, dict):
                    continue
                score += cls._coerce_int(out.get("amount"), default=0)
            return score

        return max((r for r in routes if isinstance(r, dict)), key=_score)

    @staticmethod
    def _extract_convert_approvals(
        convert: dict[str, Any],
        *,
        route: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        approvals = (
            convert.get("requiredApprovals")
            or convert.get("tokenApprovals")
            or (route.get("requiredApprovals") if isinstance(route, dict) else None)
            or (route.get("tokenApprovals") if isinstance(route, dict) else None)
            or []
        )
        if not isinstance(approvals, list):
            return []

        out: list[dict[str, Any]] = []
        for approval in approvals:
            if not isinstance(approval, dict):
                continue
            token = approval.get("token")
            amount = approval.get("amount")
            if not (isinstance(token, str) and token and amount is not None):
                continue
            out.append({"token": token, "amount": str(amount)})
        return out

    async def sdk_convert_v2(
        self,
        *,
        chain: ChainLike,
        slippage: float,
        inputs: Sequence[dict[str, str]],
        outputs: Sequence[str],
        receiver: str | None = None,
        enable_aggregator: bool = False,
        aggregators: Sequence[str] | str | None = None,
        additional_data: Sequence[str] | str | None = None,
        need_scale: bool | None = None,
        use_limit_order: bool | None = True,
        redeem_rewards: bool | None = False,
        prefer_post: bool = True,
    ) -> dict[str, Any]:
        """
        Universal Convert endpoint (swap, mint/redeem, LP add/remove, roll, etc).

        Prefer POST (OpenAPI), but Pendle currently serves GET for some setups, so we
        fallback to GET when POST returns 404.
        """
        chain_id = _as_chain_id(chain)

        if receiver is None:
            # Always set receiver, per Pendle recommendation.
            receiver = self._strategy_address()

        s = float(slippage)
        if not (0.0 <= s <= 1.0):
            raise ValueError("slippage must be between 0 and 1 (inclusive)")
        if not inputs:
            raise ValueError("inputs is required")
        if not outputs:
            raise ValueError("outputs is required")

        inputs_norm: list[dict[str, str]] = []
        for i, item in enumerate(inputs):
            token = item.get("token")
            amount = item.get("amount")
            if not (isinstance(token, str) and token):
                raise ValueError(f"inputs[{i}].token is required")
            if amount is None:
                raise ValueError(f"inputs[{i}].amount is required")
            inputs_norm.append({"token": token, "amount": str(amount)})

        outputs_norm = [str(o) for o in outputs if str(o).strip()]
        if not outputs_norm:
            raise ValueError("outputs is required")

        # Normalize aggregators/additionalData for both POST and GET shapes.
        if isinstance(aggregators, str):
            aggregators_list = [a.strip() for a in aggregators.split(",") if a.strip()]
        else:
            aggregators_list = list(aggregators) if aggregators is not None else None

        if isinstance(additional_data, str):
            additional_data_csv = additional_data
        elif additional_data is None:
            additional_data_csv = None
        else:
            additional_data_csv = ",".join([str(a) for a in additional_data if str(a)])

        if prefer_post:
            body = _compact_params(
                {
                    "slippage": s,
                    "inputs": inputs_norm,
                    "outputs": outputs_norm,
                    "receiver": receiver,
                    "enableAggregator": bool(enable_aggregator),
                    "aggregators": aggregators_list,
                    "additionalData": additional_data_csv,
                    "needScale": need_scale,
                    "useLimitOrder": use_limit_order,
                    "redeemRewards": redeem_rewards,
                }
            )
            resp = await self._request_raw(
                "POST", f"/v2/sdk/{chain_id}/convert", json=body
            )
            if resp.status_code != 404:
                resp.raise_for_status()
                payload = self._decode_response_payload(resp)
                attached = self._attach_meta(payload, resp)
                return attached if isinstance(attached, dict) else {"data": attached}

        params: dict[str, Any] = {
            "receiver": receiver,
            "slippage": s,
            "tokensIn": ",".join([i["token"] for i in inputs_norm]),
            "amountsIn": ",".join([i["amount"] for i in inputs_norm]),
            "tokensOut": ",".join(outputs_norm),
            "enableAggregator": self._bool_q(bool(enable_aggregator)),
        }
        if aggregators_list:
            params["aggregators"] = ",".join([str(a) for a in aggregators_list])
        if additional_data_csv:
            params["additionalData"] = additional_data_csv
        if need_scale is not None:
            params["needScale"] = self._bool_q(bool(need_scale))
        if use_limit_order is not None:
            params["useLimitOrder"] = self._bool_q(bool(use_limit_order))
        if redeem_rewards is not None:
            params["redeemRewards"] = self._bool_q(bool(redeem_rewards))

        data = await self._get(f"/v2/sdk/{chain_id}/convert", params=params)
        return data if isinstance(data, dict) else {"data": data}

    def build_convert_plan(
        self,
        *,
        chain: ChainLike,
        convert_response: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Turn a Hosted SDK convert response into a ready-to-send tx + approvals.

        Notes:
        - Choose the best route by maximizing summed output amounts.
        - Always trust the returned tx.to for execution (Pendle warns it may change).
        """
        chain_id = _as_chain_id(chain)

        if not isinstance(convert_response, dict):
            raise ValueError("convert_response must be a dict")

        best_route = self._select_best_convert_route(convert_response)
        tx = best_route.get("tx")
        if not isinstance(tx, dict):
            raise ValueError("Pendle convert route missing tx")

        to_addr = tx.get("to")
        from_addr = tx.get("from")
        data = tx.get("data")
        if not (
            isinstance(to_addr, str)
            and isinstance(from_addr, str)
            and isinstance(data, str)
        ):
            raise ValueError("Pendle convert tx missing to/from/data")

        return {
            "chainId": int(chain_id),
            "action": convert_response.get("action"),
            "route": best_route,
            "approvals": self._extract_convert_approvals(
                convert_response, route=best_route
            ),
            "outputs": best_route.get("outputs")
            if isinstance(best_route.get("outputs"), list)
            else [],
            "tx": {
                "chainId": int(chain_id),
                "from": to_checksum_address(from_addr),
                "to": to_checksum_address(to_addr),
                "data": data,
                "value": self._coerce_int(tx.get("value"), default=0),
            },
            "raw": convert_response,
        }

    # ---------------------------------------
    # Market discovery: PT/YT markets
    # ---------------------------------------

    async def list_active_pt_yt_markets(
        self,
        *,
        chains: Sequence[ChainLike] | None = None,
        chain: ChainLike | None = None,
        min_liquidity_usd: float = 0.0,
        min_volume_usd_24h: float = 0.0,
        min_days_to_expiry: float = 0.0,
        sort_by: Literal[
            "fixed_apy", "liquidity", "volume", "underlying_apy", "expiry"
        ] = "fixed_apy",
        descending: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Fetch active markets and return a normalized list with:
          - marketAddress, ptAddress, ytAddress, syAddress, underlyingAddress
          - fixedApy (impliedApy), underlyingApy, floatingApy (underlyingApy - impliedApy)
          - liquidityUsd, volumeUsd24h, totalTvlUsd, expiry, daysToExpiry

        NOTE: "fixed_apy" uses `impliedApy` from /v1/markets/all market.details.
        """
        if chain is not None and chains is not None:
            raise ValueError("Pass either chain=... or chains=[...], not both.")
        if chain is not None:
            chains = [chain]
        if chains is None:
            chains = [42161, 8453, 999, 9745]

        chain_ids = [_as_chain_id(c) for c in chains]

        async def fetch_one(cid: int) -> dict[str, Any]:
            return await self.fetch_markets(chain_id=cid, is_active=True)

        markets_responses = await _gather_limited(
            [lambda cid=cid: fetch_one(cid) for cid in chain_ids], concurrency=4
        )

        rows: list[dict[str, Any]] = []
        now = _now_utc()

        for resp in markets_responses:
            for m in (resp.get("markets") or []) if isinstance(resp, dict) else []:
                details = m.get("details", {}) or {}
                expiry_s = m.get("expiry")
                if not expiry_s:
                    continue

                try:
                    expiry_dt = _parse_iso8601(str(expiry_s))
                except Exception:
                    continue

                days_to_expiry = (expiry_dt - now).total_seconds() / 86400.0

                try:
                    liquidity = float(details.get("liquidity", 0.0) or 0.0)
                    volume = float(details.get("tradingVolume", 0.0) or 0.0)
                    total_tvl = float(details.get("totalTvl", 0.0) or 0.0)
                except Exception:
                    # If a market has unexpected formatting, skip it.
                    continue

                implied_apy = float(details.get("impliedApy", 0.0) or 0.0)
                underlying_apy = float(details.get("underlyingApy", 0.0) or 0.0)
                floating_apy = underlying_apy - implied_apy

                if liquidity < min_liquidity_usd:
                    continue
                if volume < min_volume_usd_24h:
                    continue
                if days_to_expiry < min_days_to_expiry:
                    continue

                chain_id_val = m.get("chainId")
                try:
                    chain_id_int = (
                        int(chain_id_val) if chain_id_val is not None else None
                    )
                except Exception:
                    chain_id_int = None
                if chain_id_int is None:
                    continue

                row: dict[str, Any] = {
                    "chainId": chain_id_int,
                    "marketName": m.get("name"),
                    "marketAddress": _as_address(str(m.get("address", ""))),
                    "expiry": expiry_s,
                    "daysToExpiry": days_to_expiry,
                    "ptAddress": _as_address(str(m.get("pt", ""))),
                    "ytAddress": _as_address(str(m.get("yt", ""))),
                    "syAddress": _as_address(str(m.get("sy", ""))),
                    "underlyingAddress": _as_address(str(m.get("underlyingAsset", ""))),
                    # Key metrics
                    "fixedApy": implied_apy,
                    "underlyingApy": underlying_apy,
                    "floatingApy": floating_apy,
                    "liquidityUsd": liquidity,
                    "volumeUsd24h": volume,
                    "totalTvlUsd": total_tvl,
                    # Extra details if you want them for decision making
                    "swapFeeApy": float(details.get("swapFeeApy", 0.0) or 0.0),
                    "pendleApy": float(details.get("pendleApy", 0.0) or 0.0),
                    "aggregatedApy": float(details.get("aggregatedApy", 0.0) or 0.0),
                    "maxBoostedApy": float(details.get("maxBoostedApy", 0.0) or 0.0),
                }
                rows.append(row)

        def sort_key(r: dict[str, Any]) -> Any:
            if sort_by == "fixed_apy":
                return r["fixedApy"]
            if sort_by == "liquidity":
                return r["liquidityUsd"]
            if sort_by == "volume":
                return r["volumeUsd24h"]
            if sort_by == "underlying_apy":
                return r["underlyingApy"]
            if sort_by == "expiry":
                return r["daysToExpiry"]
            return r["fixedApy"]

        rows.sort(key=sort_key, reverse=descending)
        return rows

    # ---------------------------------------
    # Decision + execution: best PT swap
    # ---------------------------------------

    async def build_best_pt_swap_tx(
        self,
        *,
        chain: ChainLike,
        token_in: str,
        amount_in: str,
        receiver: str,
        slippage: float = 0.01,
        enable_aggregator: bool = True,
        aggregators: Sequence[str] | str | None = None,
        # filters
        min_liquidity_usd: float = 250_000.0,
        min_volume_usd_24h: float = 25_000.0,
        min_days_to_expiry: float = 7.0,
        # performance / rate-limit controls
        max_markets_to_quote: int = 10,
        quote_concurrency: int = 6,
        # selection preference
        prefer: Literal["effective_apy", "fixed_apy"] = "effective_apy",
    ) -> dict[str, Any]:
        """
        1) Fetch active markets on chain
        2) Filter by liquidity/volume/expiry window
        3) Take top N by fixedApy (impliedApy)
        4) Quote swap token_in -> PT for each candidate market via Hosted SDK swap endpoint
           requesting additionalData: impliedApy,effectiveApy
        5) Pick best by effectiveApy (default), return full swap response incl tx + approvals
        """
        chain_id = _as_chain_id(chain)

        markets = await self.list_active_pt_yt_markets(
            chain=chain_id,
            min_liquidity_usd=min_liquidity_usd,
            min_volume_usd_24h=min_volume_usd_24h,
            min_days_to_expiry=min_days_to_expiry,
            sort_by="fixed_apy",
            descending=True,
        )

        if not markets:
            return {
                "ok": False,
                "reason": "No markets matched filters",
                "chainId": chain_id,
                "filters": {
                    "min_liquidity_usd": min_liquidity_usd,
                    "min_volume_usd_24h": min_volume_usd_24h,
                    "min_days_to_expiry": min_days_to_expiry,
                },
            }

        candidates = markets[: max(1, int(max_markets_to_quote))]

        async def quote_one(m: dict[str, Any]) -> dict[str, Any]:
            swap = await self.sdk_swap_v2(
                chain=chain_id,
                market_address=m["marketAddress"],
                receiver=receiver,
                slippage=slippage,
                token_in=token_in,
                token_out=m["ptAddress"],
                amount_in=amount_in,
                enable_aggregator=enable_aggregator,
                aggregators=aggregators,
                additional_data=["impliedApy", "effectiveApy"],
            )
            return {"market": m, "swap": swap}

        quote_jobs: list[Callable[[], Awaitable[Any]]] = [
            (lambda m=m: quote_one(m)) for m in candidates
        ]

        try:
            quoted = await _gather_limited(
                quote_jobs, concurrency=int(quote_concurrency)
            )
        except Exception as exc:
            return {
                "ok": False,
                "reason": "Quote failed",
                "chainId": chain_id,
                "error": repr(exc),
            }

        def extract_effective_apy(bundle: dict[str, Any]) -> float | None:
            data = (bundle.get("swap") or {}).get("data") or {}
            val = data.get("effectiveApy")
            if val is None:
                return None
            try:
                return float(val)
            except Exception:
                return None

        def extract_implied_after(bundle: dict[str, Any]) -> float | None:
            data = (bundle.get("swap") or {}).get("data") or {}
            imp = data.get("impliedApy")
            # impliedApy can be {before, after}
            if isinstance(imp, dict):
                after = imp.get("after")
                if after is None:
                    return None
                try:
                    return float(after)
                except Exception:
                    return None
            return None

        def extract_price_impact(bundle: dict[str, Any]) -> float:
            data = (bundle.get("swap") or {}).get("data") or {}
            try:
                return float(data.get("priceImpact", 0.0) or 0.0)
            except Exception:
                return 0.0

        valid: list[dict[str, Any]] = []
        for b in quoted:
            swap = b.get("swap") or {}
            tx = swap.get("tx")
            if isinstance(tx, dict) and tx.get("to") and tx.get("data") is not None:
                valid.append(b)

        if not valid:
            return {
                "ok": False,
                "reason": "No valid swap quotes (tx missing). Check token_in existence/decimals and enable_aggregator.",
                "chainId": chain_id,
            }

        def score(bundle: dict[str, Any]) -> tuple[float, float, float, float]:
            m = bundle["market"]
            eff = extract_effective_apy(bundle)
            imp_after = extract_implied_after(bundle)
            fixed = float(m.get("fixedApy", 0.0) or 0.0)

            if prefer == "effective_apy":
                primary = (
                    eff
                    if eff is not None
                    else (imp_after if imp_after is not None else fixed)
                )
            else:
                primary = fixed

            pi = extract_price_impact(bundle)
            liq = float(m.get("liquidityUsd", 0.0) or 0.0)
            vol = float(m.get("volumeUsd24h", 0.0) or 0.0)

            # Max primary, min price impact, max liquidity, max volume
            return (primary, -pi, liq, vol)

        best = max(valid, key=score)
        best_market = best["market"]
        best_swap = best["swap"]
        best_data = best_swap.get("data") or {}

        return {
            "ok": True,
            "chainId": chain_id,
            "selectedMarket": best_market,
            "quote": {
                "amountOut": best_data.get("amountOut"),
                "priceImpact": best_data.get("priceImpact"),
                "impliedApy": best_data.get("impliedApy"),
                "effectiveApy": best_data.get("effectiveApy"),
            },
            "tx": best_swap.get("tx"),
            "tokenApprovals": best_swap.get("tokenApprovals", []),
            "raw": best_swap,
            "evaluated": [
                {
                    "marketAddress": b["market"]["marketAddress"],
                    "ptAddress": b["market"]["ptAddress"],
                    "fixedApy": b["market"]["fixedApy"],
                    "liquidityUsd": b["market"]["liquidityUsd"],
                    "volumeUsd24h": b["market"]["volumeUsd24h"],
                    "daysToExpiry": b["market"]["daysToExpiry"],
                    "effectiveApy": extract_effective_apy(b),
                    "impliedApyAfter": extract_implied_after(b),
                    "priceImpact": extract_price_impact(b),
                }
                for b in valid
            ],
        }

    async def build_best_pt_convert_tx(
        self,
        *,
        chain: ChainLike,
        token_in: str,
        amount_in: str,
        receiver: str,
        slippage: float = 0.01,
        enable_aggregator: bool = True,
        aggregators: Sequence[str] | str | None = None,
        additional_data: Sequence[str] | str | None = (
            "impliedApy",
            "effectiveApy",
            "priceImpact",
        ),
        # filters
        min_liquidity_usd: float = 250_000.0,
        min_volume_usd_24h: float = 25_000.0,
        min_days_to_expiry: float = 7.0,
        # rate-limit controls
        max_markets_to_quote: int = 10,
        min_ratelimit_remaining: int = 1,
        # selection preference
        prefer: Literal["effective_apy", "fixed_apy"] = "effective_apy",
    ) -> dict[str, Any]:
        """
        Like build_best_pt_swap_tx(), but uses the universal convert endpoint.

        This avoids hardcoding market routers and supports mint/redeem/roll flows.
        """
        chain_id = _as_chain_id(chain)

        markets = await self.list_active_pt_yt_markets(
            chain=chain_id,
            min_liquidity_usd=min_liquidity_usd,
            min_volume_usd_24h=min_volume_usd_24h,
            min_days_to_expiry=min_days_to_expiry,
            sort_by="fixed_apy",
            descending=True,
        )

        if not markets:
            return {
                "ok": False,
                "reason": "No markets matched filters",
                "chainId": chain_id,
            }

        candidates = markets[: max(1, int(max_markets_to_quote))]

        def extract_effective_apy(bundle: dict[str, Any]) -> float | None:
            data = ((bundle.get("plan") or {}).get("route") or {}).get("data") or {}
            val = data.get("effectiveApy")
            if val is None:
                return None
            try:
                return float(val)
            except Exception:
                return None

        def extract_implied_after(bundle: dict[str, Any]) -> float | None:
            data = ((bundle.get("plan") or {}).get("route") or {}).get("data") or {}
            imp = data.get("impliedApy")
            if isinstance(imp, dict):
                after = imp.get("after")
                if after is None:
                    return None
                try:
                    return float(after)
                except Exception:
                    return None
            return None

        def extract_price_impact(bundle: dict[str, Any]) -> float:
            data = ((bundle.get("plan") or {}).get("route") or {}).get("data") or {}
            try:
                return float(data.get("priceImpact", 0.0) or 0.0)
            except Exception:
                return 0.0

        valid: list[dict[str, Any]] = []
        last_rate_limit: dict[str, Any] | None = None

        for m in candidates:
            if (
                last_rate_limit is not None
                and isinstance(last_rate_limit.get("ratelimitRemaining"), int)
                and last_rate_limit["ratelimitRemaining"]
                <= int(min_ratelimit_remaining)
            ):
                break

            try:
                convert_resp = await self.sdk_convert_v2(
                    chain=chain_id,
                    slippage=slippage,
                    receiver=receiver,
                    inputs=[{"token": token_in, "amount": str(amount_in)}],
                    outputs=[m["ptAddress"]],
                    enable_aggregator=enable_aggregator,
                    aggregators=aggregators,
                    additional_data=additional_data,
                )
                plan = self.build_convert_plan(
                    chain=chain_id, convert_response=convert_resp
                )
            except Exception:
                continue

            last_rate_limit = (
                convert_resp.get("rateLimit")
                if isinstance(convert_resp, dict)
                else None
            )

            tx = plan.get("tx") if isinstance(plan, dict) else None
            if not (
                isinstance(tx, dict) and tx.get("to") and tx.get("data") is not None
            ):
                continue

            valid.append({"market": m, "plan": plan, "raw": convert_resp})

        if not valid:
            return {
                "ok": False,
                "reason": "No valid convert quotes (tx missing). Check token_in existence/decimals and enable_aggregator.",
                "chainId": chain_id,
                "rateLimit": last_rate_limit,
            }

        def score(bundle: dict[str, Any]) -> tuple[float, float, float, float]:
            m = bundle["market"]
            eff = extract_effective_apy(bundle)
            imp_after = extract_implied_after(bundle)
            fixed = float(m.get("fixedApy", 0.0) or 0.0)

            if prefer == "effective_apy":
                primary = (
                    eff
                    if eff is not None
                    else (imp_after if imp_after is not None else fixed)
                )
            else:
                primary = fixed

            pi = extract_price_impact(bundle)
            liq = float(m.get("liquidityUsd", 0.0) or 0.0)
            vol = float(m.get("volumeUsd24h", 0.0) or 0.0)
            return (primary, -pi, liq, vol)

        best = max(valid, key=score)
        best_market = best["market"]
        best_plan = best["plan"]
        best_route_data = (best_plan.get("route") or {}).get("data") or {}
        best_outputs = best_plan.get("outputs") or []
        amount_out = (
            best_outputs[0].get("amount")
            if isinstance(best_outputs, list) and best_outputs
            else None
        )

        return {
            "ok": True,
            "chainId": chain_id,
            "selectedMarket": best_market,
            "quote": {
                "amountOut": amount_out,
                "priceImpact": best_route_data.get("priceImpact"),
                "impliedApy": best_route_data.get("impliedApy"),
                "effectiveApy": best_route_data.get("effectiveApy"),
            },
            "tx": best_plan.get("tx"),
            "requiredApprovals": best_plan.get("approvals", []),
            # Backwards-friendly alias
            "tokenApprovals": best_plan.get("approvals", []),
            "raw": best.get("raw"),
            "rateLimit": (best.get("raw") or {}).get("rateLimit")
            if isinstance(best.get("raw"), dict)
            else None,
            "evaluated": [
                {
                    "marketAddress": b["market"]["marketAddress"],
                    "ptAddress": b["market"]["ptAddress"],
                    "fixedApy": b["market"]["fixedApy"],
                    "liquidityUsd": b["market"]["liquidityUsd"],
                    "volumeUsd24h": b["market"]["volumeUsd24h"],
                    "daysToExpiry": b["market"]["daysToExpiry"],
                    "effectiveApy": extract_effective_apy(b),
                    "impliedApyAfter": extract_implied_after(b),
                    "priceImpact": extract_price_impact(b),
                }
                for b in valid
            ],
        }

    async def build_best_pt_swap_tx_multi_chain(
        self,
        *,
        chains: Sequence[ChainLike] = ("arbitrum", "hyperevm", "base"),
        token_in_by_chain: dict[int, str],
        amount_in_by_chain: dict[int, str],
        receiver_by_chain: dict[int, str],
        slippage: float = 0.01,
        enable_aggregator: bool = True,
        aggregators: Sequence[str] | str | None = None,
        min_liquidity_usd: float = 250_000.0,
        min_volume_usd_24h: float = 25_000.0,
        min_days_to_expiry: float = 7.0,
        max_markets_to_quote: int = 10,
        quote_concurrency: int = 6,
        prefer: Literal["effective_apy", "fixed_apy"] = "effective_apy",
    ) -> dict[int, dict[str, Any]]:
        """
        Convenience: run best-PT selection per chain.
        You must supply token/amount/receiver per chain (tokens live on that chain).
        """
        chain_ids = [_as_chain_id(c) for c in chains]

        async def run_one(cid: int) -> dict[str, Any]:
            return await self.build_best_pt_swap_tx(
                chain=cid,
                token_in=token_in_by_chain[cid],
                amount_in=amount_in_by_chain[cid],
                receiver=receiver_by_chain[cid],
                slippage=slippage,
                enable_aggregator=enable_aggregator,
                aggregators=aggregators,
                min_liquidity_usd=min_liquidity_usd,
                min_volume_usd_24h=min_volume_usd_24h,
                min_days_to_expiry=min_days_to_expiry,
                max_markets_to_quote=max_markets_to_quote,
                quote_concurrency=quote_concurrency,
                prefer=prefer,
            )

        results = await _gather_limited(
            [lambda cid=cid: run_one(cid) for cid in chain_ids], concurrency=3
        )
        return dict(zip(chain_ids, results, strict=False))

    async def get_full_user_state(
        self,
        *,
        account: str,
        include_inactive: bool = True,
        include_sy: bool = True,
        include_zero_positions: bool = False,
        multicall_chunk_size: int = 400,
        include_prices: bool = False,
        price_concurrency: int = 8,
    ) -> tuple[bool, dict[str, Any] | str]:
        """Query all Pendle chains and return merged positions."""
        all_positions: list[dict[str, Any]] = []
        chains_queried: list[int] = []
        errors: list[str] = []

        for cid in PENDLE_CHAIN_IDS.values():
            ok, result = await self.get_full_user_state_per_chain(
                chain=cid,
                account=account,
                include_inactive=include_inactive,
                include_sy=include_sy,
                include_zero_positions=include_zero_positions,
                multicall_chunk_size=multicall_chunk_size,
                include_prices=include_prices,
                price_concurrency=price_concurrency,
            )
            if ok:
                chain_data = result  # type: ignore[assignment]
                all_positions.extend(chain_data.get("positions", []))
                chains_queried.append(cid)
            else:
                errors.append(f"chain {cid}: {result}")

        if not chains_queried and errors:
            return False, "; ".join(errors)

        return True, {
            "protocol": "pendle",
            "account": account,
            "chains": chains_queried,
            "positions": all_positions,
            "errors": errors,
        }

    async def get_full_user_state_per_chain(
        self,
        *,
        chain: ChainLike,
        account: str,
        include_inactive: bool = True,
        include_sy: bool = True,
        include_zero_positions: bool = False,
        multicall_chunk_size: int = 400,
        include_prices: bool = False,
        price_concurrency: int = 8,
    ) -> tuple[bool, dict[str, Any] | str]:
        """
        Pendle "full user state" snapshot via on-chain ERC20 balance scan.

        Flow:
          1) Fetch markets from Pendle API (market/pt/yt/sy addresses + expiry metadata)
          2) Multicall ERC20.balanceOf(account) + ERC20.decimals() for PT/YT/LP/(SY)
          3) Optionally fetch market snapshots (API) for markets with positions
        """
        chain_id = _as_chain_id(chain)

        try:
            markets_resp = await self.fetch_markets(
                chain_id=chain_id,
                is_active=None if include_inactive else True,
            )
            markets = markets_resp.get("markets") or []

            now = _now_utc()
            normalized: list[dict[str, Any]] = []
            for m in markets:
                expiry_s = m.get("expiry")
                try:
                    expiry_dt = _parse_iso8601(str(expiry_s)) if expiry_s else None
                except Exception:  # noqa: BLE001
                    expiry_dt = None
                days_to_expiry = (
                    (expiry_dt - now).total_seconds() / 86400.0 if expiry_dt else None
                )

                normalized.append(
                    {
                        "chainId": int(m.get("chainId") or chain_id),
                        "marketName": m.get("name"),
                        "marketAddress": _as_address(str(m.get("address", ""))),
                        "pt": _as_address(str(m.get("pt", ""))),
                        "yt": _as_address(str(m.get("yt", ""))),
                        "sy": _as_address(str(m.get("sy", ""))),
                        "underlying": _as_address(str(m.get("underlyingAsset", ""))),
                        "expiry": expiry_s,
                        "daysToExpiry": days_to_expiry,
                        "active": bool(m.get("isActive"))
                        if m.get("isActive") is not None
                        else None,
                    }
                )

            async with web3_from_chain_id(chain_id) as web3:
                user_ck = web3.to_checksum_address(account)
                multicall = MulticallAdapter(chain_id=chain_id, web3=web3)

                call_specs: list[tuple[int, str, str, str]] = []
                calls: list[Any] = []

                def add_token_calls(midx: int, kind: str, token: str) -> None:
                    if not token:
                        return
                    token_ck = web3.to_checksum_address(token)
                    erc20 = web3.eth.contract(address=token_ck, abi=ERC20_ABI)

                    calls.append(
                        multicall.build_call(
                            token_ck,
                            erc20.encode_abi("balanceOf", args=[user_ck]),
                        )
                    )
                    call_specs.append((midx, kind, token_ck, "bal"))

                    calls.append(
                        multicall.build_call(
                            token_ck,
                            erc20.encode_abi("decimals", args=[]),
                        )
                    )
                    call_specs.append((midx, kind, token_ck, "dec"))

                for i, m in enumerate(normalized):
                    add_token_calls(i, "pt", m["pt"])
                    add_token_calls(i, "yt", m["yt"])
                    add_token_calls(i, "lp", m["marketAddress"])
                    if include_sy:
                        add_token_calls(i, "sy", m["sy"])

                decoded = await self._multicall_uint256_chunked(
                    multicall=multicall,
                    calls=calls,
                    chunk_size=multicall_chunk_size,
                )

                per_market: list[dict[str, Any]] = [
                    dict(m, balances={}) for m in normalized
                ]

                for spec, val in zip(call_specs, decoded, strict=False):
                    midx, kind, token, which = spec
                    if midx >= len(per_market):
                        continue
                    bucket = per_market[midx]["balances"].setdefault(
                        kind,
                        {
                            "address": token,
                            "raw": 0,
                            "decimals": None,
                        },
                    )
                    if which == "bal":
                        bucket["raw"] = int(val or 0)
                    else:
                        bucket["decimals"] = int(val) if val is not None else None

            positions: list[dict[str, Any]] = []
            for m in per_market:
                balances = m.get("balances") or {}
                has_any = False
                for kind in ("pt", "yt", "lp", "sy"):
                    if kind in balances and int(balances[kind].get("raw") or 0) > 0:
                        has_any = True
                        break
                if not include_zero_positions and not has_any:
                    continue
                positions.append(m)

            if include_prices and positions:

                async def fetch_one(pos: dict[str, Any]) -> dict[str, Any]:
                    cid = int(pos.get("chainId") or chain_id)
                    market_address = str(pos.get("marketAddress") or "").strip()
                    if not market_address:
                        return {}
                    try:
                        return await self.fetch_market_snapshot(
                            chain_id=cid, market_address=market_address
                        )
                    except Exception:  # noqa: BLE001
                        return {}

                snapshots = await _gather_limited(
                    [lambda pos=pos: fetch_one(pos) for pos in positions],
                    concurrency=int(price_concurrency),
                )
                for pos, snap in zip(positions, snapshots, strict=False):
                    if snap:
                        pos["marketSnapshot"] = snap

            return (
                True,
                {
                    "protocol": "pendle",
                    "source": "onchain_scan_multicall",
                    "chainId": int(chain_id),
                    "account": account,
                    "positions": positions,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    # ---------------------------------------
    # Execute swap
    # ---------------------------------------

    async def execute_swap(
        self,
        *,
        chain: ChainLike,
        market_address: str,
        token_in: str,
        token_out: str,
        amount_in: str,
        receiver: str | None = None,
        slippage: float = 0.01,
        enable_aggregator: bool = False,
        aggregators: Sequence[str] | str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """
        Execute a Pendle swap: get quote, handle approvals, broadcast tx.

        This is a generic execution method that can swap into any token:
        - PT (Principal Token) - for fixed yield
        - YT (Yield Token) - for floating yield
        - SY (Standardized Yield) - underlying wrapper
        - Or any other token the market supports

        Args:
            chain: Chain ID or name (e.g., 42161 or "arbitrum")
            market_address: Pendle market address
            token_in: Input token address (ERC20)
            token_out: Output token address (PT, YT, SY, etc.)
            amount_in: Amount in raw base units (string)
            receiver: Optional receiver address (defaults to strategy wallet)
            slippage: Slippage tolerance as decimal (0.01 = 1%)
            enable_aggregator: Enable DEX aggregators for routing
            aggregators: Specific aggregators to use

        Returns:
            tuple[bool, dict]: (success, details_dict)
        """
        chain_id = _as_chain_id(chain)
        sender = self._strategy_address()
        actual_receiver = receiver or sender

        # Step 1: Get quote via sdk_swap_v2
        quote_result = await self.sdk_swap_v2(
            chain=chain_id,
            market_address=market_address,
            receiver=actual_receiver,
            slippage=slippage,
            token_in=token_in,
            token_out=token_out,
            amount_in=amount_in,
            enable_aggregator=enable_aggregator,
            aggregators=aggregators,
            additional_data=["impliedApy", "effectiveApy"],
        )

        tx_data = quote_result.get("tx")
        if not tx_data or not tx_data.get("to"):
            return False, {
                "error": "Quote returned invalid tx",
                "stage": "quote",
                "raw": quote_result,
            }

        # Step 2: Handle token approvals
        token_approvals = quote_result.get("tokenApprovals") or []
        spender = to_checksum_address(tx_data["to"])

        for approval in token_approvals:
            token = approval.get("token")
            amount = approval.get("amount")
            if not token or not amount:
                continue
            if not self.signing_callback:
                return False, {
                    "error": "signing_callback is required",
                    "stage": "approval",
                    "details": {"error": "signing_callback is required"},
                }
            approved, result = await ensure_allowance(
                chain_id=chain_id,
                token_address=token,
                owner=sender,
                spender=spender,
                amount=int(amount),
                signing_callback=self.signing_callback,
            )
            if not approved:
                return False, {
                    "error": f"Approval failed for {token}",
                    "stage": "approval",
                    "details": result,
                }

        # Step 3: Broadcast swap tx
        swap_tx = {
            "chainId": chain_id,
            "from": to_checksum_address(sender),
            "to": to_checksum_address(tx_data["to"]),
            "data": tx_data["data"],
            "value": int(tx_data.get("value") or 0),
        }

        try:
            success, tx_hash = await self._send_tx(swap_tx)
        except Exception as exc:
            return False, {
                "error": str(exc),
                "stage": "broadcast",
                "quote": quote_result,
            }

        return True, {
            "tx_hash": tx_hash,
            "chainId": chain_id,
            "quote": quote_result.get("data"),
            "tokenApprovals": token_approvals,
        }

    # ---------------------------------------
    # Execute universal convert
    # ---------------------------------------

    async def execute_convert(
        self,
        *,
        chain: ChainLike,
        slippage: float,
        inputs: Sequence[dict[str, str]],
        outputs: Sequence[str],
        receiver: str | None = None,
        enable_aggregator: bool = False,
        aggregators: Sequence[str] | str | None = None,
        additional_data: Sequence[str] | str | None = None,
        need_scale: bool | None = None,
        use_limit_order: bool | None = True,
        redeem_rewards: bool | None = False,
        rebuild_after_approval: bool = True,
    ) -> tuple[bool, dict[str, Any]]:
        """
        Execute a Pendle Hosted SDK convert: build tx, handle approvals, broadcast.

        This is the recommended universal entrypoint for Pendle actions:
        swaps, LP add/remove, mint/redeem, roll, etc.
        """
        chain_id = _as_chain_id(chain)
        sender = self._strategy_address()
        actual_receiver = receiver or sender

        # Preflight balances for each input token.
        try:
            for i, item in enumerate(inputs):
                token = item.get("token")
                amount_s = item.get("amount")
                if not (isinstance(token, str) and token):
                    return False, {
                        "stage": "preflight",
                        "error": f"inputs[{i}].token is required",
                    }
                if amount_s is None:
                    return False, {
                        "stage": "preflight",
                        "error": f"inputs[{i}].amount is required",
                    }
                amount = int(str(amount_s))
                bal = await get_token_balance(token, chain_id, sender)
                if int(bal) < int(amount):
                    return False, {
                        "stage": "preflight",
                        "error": "Insufficient balance",
                        "token": token,
                        "need": amount,
                        "have": int(bal),
                    }
        except Exception as exc:  # noqa: BLE001
            return False, {"stage": "preflight", "error": str(exc)}

        try:
            convert_resp = await self.sdk_convert_v2(
                chain=chain_id,
                slippage=slippage,
                receiver=actual_receiver,
                inputs=inputs,
                outputs=outputs,
                enable_aggregator=enable_aggregator,
                aggregators=aggregators,
                additional_data=additional_data,
                need_scale=need_scale,
                use_limit_order=use_limit_order,
                redeem_rewards=redeem_rewards,
            )
            plan = self.build_convert_plan(
                chain=chain_id, convert_response=convert_resp
            )
        except Exception as exc:  # noqa: BLE001
            return False, {"stage": "quote", "error": str(exc)}

        spender = plan["tx"]["to"]
        approvals = plan.get("approvals") or []

        # Approvals
        for approval in approvals:
            token = approval.get("token")
            amount = approval.get("amount")
            if not (isinstance(token, str) and token and amount is not None):
                continue
            if not self.signing_callback:
                return False, {
                    "stage": "approval",
                    "error": "signing_callback is required",
                    "token": token,
                }
            try:
                approved, result = await ensure_allowance(
                    chain_id=chain_id,
                    token_address=token,
                    owner=sender,
                    spender=spender,
                    amount=int(str(amount)),
                    signing_callback=self.signing_callback,
                )
            except Exception as exc:  # noqa: BLE001
                return False, {
                    "stage": "approval",
                    "error": str(exc),
                    "token": token,
                }
            if not approved:
                return False, {
                    "stage": "approval",
                    "error": f"Approval failed for {token}",
                    "details": result,
                }

        # Optional re-build after approvals to avoid "preview routes".
        if approvals and rebuild_after_approval:
            try:
                convert_resp = await self.sdk_convert_v2(
                    chain=chain_id,
                    slippage=slippage,
                    receiver=actual_receiver,
                    inputs=inputs,
                    outputs=outputs,
                    enable_aggregator=enable_aggregator,
                    aggregators=aggregators,
                    additional_data=additional_data,
                    need_scale=need_scale,
                    use_limit_order=use_limit_order,
                    redeem_rewards=redeem_rewards,
                )
                plan = self.build_convert_plan(
                    chain=chain_id, convert_response=convert_resp
                )
                approvals = plan.get("approvals") or approvals
            except Exception as exc:  # noqa: BLE001
                return False, {"stage": "rebuild", "error": str(exc)}

        # Broadcast tx (exactly as returned)
        try:
            _, tx_hash = await self._send_tx(plan["tx"])
        except Exception as exc:  # noqa: BLE001
            return False, {
                "stage": "broadcast",
                "error": str(exc),
                "tx": plan.get("tx"),
            }

        # Post-check balances for output tokens.
        post_balances: dict[str, int] = {}
        for out in plan.get("outputs") or []:
            if not isinstance(out, dict):
                continue
            token = out.get("token")
            if not isinstance(token, str):
                continue
            try:
                post_balances[token] = int(
                    await get_token_balance(token, chain_id, actual_receiver)
                )
            except Exception:  # noqa: BLE001
                continue

        return True, {
            "tx_hash": tx_hash,
            "chainId": chain_id,
            "action": plan.get("action"),
            "approvals": approvals,
            "outputs": plan.get("outputs"),
            "postBalances": post_balances,
            "rateLimit": (convert_resp or {}).get("rateLimit")
            if isinstance(convert_resp, dict)
            else None,
        }
