import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any

from loguru import logger
from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.middleware import ExtraDataToPOAMiddleware
from web3.module import Module

from wayfinder_paths.core.config import (
    get_api_base_url,
    get_api_key,
    get_gorlami_base_url,
    get_rpc_urls,
)
from wayfinder_paths.core.constants.chains import (
    CHAIN_ID_HYPEREVM,
    POA_MIDDLEWARE_CHAIN_IDS,
)


class HyperModule(Module):
    def __init__(self, w3):
        super().__init__(w3)

    async def big_block_gas_price(self):
        big_block_gas_price = await self.w3.manager.coro_request(
            "eth_bigBlockGasPrice", []
        )
        return int(big_block_gas_price, 16)


# Rate-limit failover policy:
# - Fail over only for provider rate limiting (HTTP 429 / known RPC codes / known messages)
# - Do not fail over for client errors or on-chain execution errors
_GORLAMI_RETRYABLE_STATUS_CODES = {502, 503, 504}
_RATE_LIMIT_HTTP_STATUS = 429
_RATE_LIMIT_RPC_ERROR_CODES = {429, -32005, -33200, -33300, -33400}
_RATE_LIMIT_MESSAGE_MARKERS = (
    "too many requests",
    "rate limit",
    "request rate exceeded",
    "limit exceeded",
    "compute units per second",
    "concurrent requests",
)
_RPC_FAILOVER_PATH = "/blockchain/rpc"
_DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 60.0
_RPC_RATE_LIMIT_COOLDOWN_UNTIL: dict[tuple[int, str], float] = {}


def _default_rpc_headers() -> dict[str, str]:
    return AsyncHTTPProvider.get_request_headers()


def _wayfinder_auth_headers() -> dict[str, str]:
    headers = AsyncHTTPProvider.get_request_headers()
    api_key = get_api_key()
    if api_key:
        headers = {**headers, "X-API-KEY": api_key}
    return headers


def _decode_rpc_response_with_id(
    provider: AsyncHTTPProvider, raw_response: bytes, request_id: Any
) -> dict[str, Any]:
    response = provider.decode_rpc_response(raw_response)
    if isinstance(response, dict) and "id" not in response:
        response["id"] = request_id
    return response


async def _perform_rpc_request(
    provider: AsyncHTTPProvider,
    *,
    method: str,
    request_data: bytes,
    request_id: Any,
) -> dict[str, Any]:
    raw_response = await provider._make_request(method, request_data)
    return _decode_rpc_response_with_id(provider, raw_response, request_id)


def _extract_http_status(exc: Exception) -> int | None:
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return status
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    response = getattr(exc, "response", None)
    if response is not None:
        code = getattr(response, "status_code", None)
        if isinstance(code, int):
            return code
    return None


def _extract_retry_after_seconds_from_exception(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if not headers:
        return None
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        parsed = float(value)
        return parsed if parsed > 0 else None
    except Exception:
        return None


def _rpc_error_text(error: dict[str, Any]) -> str:
    msg = str(error.get("message") or "").lower()
    details = str(error.get("details") or "").lower()
    return f"{msg} {details}".strip()


def _is_rate_limited_rpc_error(error: dict[str, Any]) -> bool:
    code = error.get("code")
    if isinstance(code, int) and code in _RATE_LIMIT_RPC_ERROR_CODES:
        return True
    text = _rpc_error_text(error)
    return any(marker in text for marker in _RATE_LIMIT_MESSAGE_MARKERS)


def _is_rate_limited_exception(exc: Exception) -> bool:
    return _extract_http_status(exc) == _RATE_LIMIT_HTTP_STATUS


def _extract_cooldown_seconds_from_rpc_error(error: dict[str, Any]) -> float | None:
    data = error.get("data")
    if not isinstance(data, dict):
        return None
    for key in ("backoff_seconds", "retry_after", "retry_after_seconds"):
        raw = data.get(key)
        try:
            parsed = float(raw)
            if parsed > 0:
                return parsed
        except Exception:
            continue
    return None


def _is_in_rate_limit_cooldown(chain_id: int, endpoint_uri: str) -> bool:
    key = (chain_id, endpoint_uri)
    until = _RPC_RATE_LIMIT_COOLDOWN_UNTIL.get(key, 0.0)
    if until <= time.monotonic():
        _RPC_RATE_LIMIT_COOLDOWN_UNTIL.pop(key, None)
        return False
    return True


def _mark_rate_limit_cooldown(
    chain_id: int, endpoint_uri: str, cooldown_seconds: float
) -> None:
    key = (chain_id, endpoint_uri)
    _RPC_RATE_LIMIT_COOLDOWN_UNTIL[key] = time.monotonic() + max(
        0.0, float(cooldown_seconds)
    )


def _clear_rate_limit_cooldowns() -> None:
    _RPC_RATE_LIMIT_COOLDOWN_UNTIL.clear()


class _GorlamiProvider(AsyncHTTPProvider):
    async def make_request(self, method, params):  # type: ignore[override]
        # Gorlami's JSON-RPC responses omit `id`, which breaks web3.py.
        # It can also intermittently return 502/503/504, so retry a bit.
        req = self.form_request(method, params)
        request_data = self.encode_rpc_dict(req)

        max_retries = 3
        delay_s = 0.25
        for attempt in range(max_retries):
            try:
                return await _perform_rpc_request(
                    self,
                    method=method,
                    request_data=request_data,
                    request_id=req.get("id"),
                )
            except Exception as exc:
                status = getattr(exc, "status", None)
                if status in _GORLAMI_RETRYABLE_STATUS_CODES and attempt < (
                    max_retries - 1
                ):
                    await asyncio.sleep(delay_s * (2**attempt))
                    continue
                raise


def _get_rpc_failover_endpoint(chain_id: int) -> str:
    base = get_api_base_url().rstrip("/")
    return f"{base}{_RPC_FAILOVER_PATH}/{chain_id}/"


class _FailoverRpcProvider(AsyncHTTPProvider):
    def __init__(
        self,
        rpc: str,
        chain_id: int,
        primary_request_kwargs: dict | None = None,
        failover_request_kwargs: dict | None = None,
    ):
        super().__init__(rpc, request_kwargs=primary_request_kwargs)
        self.chain_id = chain_id
        self.failover_endpoint = _get_rpc_failover_endpoint(chain_id)
        self.failover_provider = AsyncHTTPProvider(
            self.failover_endpoint,
            request_kwargs=failover_request_kwargs,
        )

    async def disconnect(self) -> None:
        primary_exc: Exception | None = None
        try:
            await super().disconnect()
        except Exception as exc:
            primary_exc = exc
        try:
            await self.failover_provider.disconnect()
        except Exception:
            if primary_exc is None:
                raise
        if primary_exc is not None:
            raise primary_exc

    async def _request_via_failover(
        self, *, method: str, request_data: bytes, request_id: Any
    ) -> dict[str, Any]:
        response = await _perform_rpc_request(
            self.failover_provider,
            method=method,
            request_data=request_data,
            request_id=request_id,
        )
        logger.info(
            f"RPC failover succeeded chain={self.chain_id} method={method} id={request_id}"
        )
        return response

    async def make_request(self, method, params):  # type: ignore[override]
        req = self.form_request(method, params)
        request_data = self.encode_rpc_dict(req)
        request_id = req.get("id")

        if _is_in_rate_limit_cooldown(self.chain_id, self.endpoint_uri):
            logger.debug(
                f"Primary RPC is in rate-limit cooldown for chain {self.chain_id}; using backend failover {self.failover_endpoint}"
            )
            return await self._request_via_failover(
                method=method,
                request_data=request_data,
                request_id=request_id,
            )

        try:
            response = await _perform_rpc_request(
                self, method=method, request_data=request_data, request_id=request_id
            )
        except Exception as exc:
            if not _is_rate_limited_exception(exc):
                raise
            cooldown_s = (
                _extract_retry_after_seconds_from_exception(exc)
                or _DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
            )
            _mark_rate_limit_cooldown(self.chain_id, self.endpoint_uri, cooldown_s)
            logger.warning(
                f"Primary RPC rate-limited for chain {self.chain_id}; using backend failover endpoint {self.failover_endpoint}. Error: {exc}"
            )
            return await self._request_via_failover(
                method=method,
                request_data=request_data,
                request_id=request_id,
            )

        error = response.get("error")
        if not isinstance(error, dict):
            return response
        if _is_rate_limited_rpc_error(error):
            cooldown_s = (
                _extract_cooldown_seconds_from_rpc_error(error)
                or _DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
            )
            _mark_rate_limit_cooldown(self.chain_id, self.endpoint_uri, cooldown_s)
            logger.warning(
                f"Primary RPC returned rate-limit JSON-RPC error for chain {self.chain_id}; using backend failover endpoint {self.failover_endpoint}. Error: {error}"
            )
            return await self._request_via_failover(
                method=method,
                request_data=request_data,
                request_id=request_id,
            )
        return response


def _get_gorlami_base_url_safe() -> str | None:
    try:
        return get_gorlami_base_url().rstrip("/")
    except Exception:
        return None


def _is_gorlami_fork_rpc(rpc: str) -> bool:
    base = _get_gorlami_base_url_safe()
    if not base:
        return False
    return rpc.startswith(f"{base}/fork/")


def _get_rpcs_for_chain_id(chain_id: int) -> list:
    mapping = get_rpc_urls()
    rpcs = mapping.get(str(chain_id))
    if rpcs is None:
        rpcs = mapping.get(chain_id)  # allow int keys
    if rpcs is None:
        raise ValueError(f"No RPCs configured for chain ID {chain_id}")
    if isinstance(rpcs, str):
        return [rpcs]
    return rpcs


def _get_web3(rpc: str, chain_id: int) -> AsyncWeb3:
    if _is_gorlami_fork_rpc(rpc):
        headers = _wayfinder_auth_headers()
        if "X-API-KEY" not in headers:
            logger.warning("No API key configured; Gorlami fork requests may fail")
        provider = _GorlamiProvider(rpc, request_kwargs={"headers": headers})
        web3 = AsyncWeb3(provider)
    else:
        primary_headers = _default_rpc_headers()
        failover_headers = _wayfinder_auth_headers()
        provider = _FailoverRpcProvider(
            rpc,
            chain_id,
            primary_request_kwargs={"headers": primary_headers},
            failover_request_kwargs={"headers": failover_headers},
        )
        web3 = AsyncWeb3(provider)
    if chain_id in POA_MIDDLEWARE_CHAIN_IDS:
        web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if chain_id == CHAIN_ID_HYPEREVM:
        web3.attach_modules({"hype": (HyperModule)})
    return web3


def get_transaction_chain_id(transaction: dict) -> int:
    if "chainId" not in transaction:
        raise ValueError("Transaction does not contain chainId")
    return int(transaction["chainId"])


def get_web3s_from_chain_id(chain_id: int) -> list[AsyncWeb3]:
    rpcs = _get_rpcs_for_chain_id(chain_id)
    return [_get_web3(rpc, chain_id) for rpc in rpcs]


@asynccontextmanager
async def web3s_from_chain_id(chain_id: int):
    web3s = get_web3s_from_chain_id(chain_id)
    try:
        yield web3s
    finally:
        for web3 in web3s:
            await web3.provider.disconnect()


@asynccontextmanager
async def web3_from_chain_id(chain_id: int):
    web3s = get_web3s_from_chain_id(chain_id)
    try:
        yield web3s[0]
    finally:
        await web3s[0].provider.disconnect()
