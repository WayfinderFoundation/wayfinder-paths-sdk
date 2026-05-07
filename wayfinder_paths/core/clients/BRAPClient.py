from __future__ import annotations

import asyncio
import time
from typing import Any, NotRequired, Required, TypedDict, cast

from loguru import logger

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url


class QuoteTx(TypedDict, total=False):
    data: str
    to: str
    value: str
    chainId: int


class QuoteData(TypedDict):
    gas: Required[str]
    amountOut: Required[str]
    priceImpact: Required[int]
    feeAmount: Required[list[str]]
    minAmountOut: Required[str]
    createdAt: Required[int]
    tx: Required[QuoteTx]
    route: Required[list[dict[str, Any]]]


class FeeBreakdown(TypedDict):
    name: Required[str]
    amount: Required[int]
    amount_usd: Required[float]
    token: Required[str]
    token_chain: Required[int]


class FeeEstimate(TypedDict):
    fee_total_usd: Required[float]
    fee_breakdown: Required[list[FeeBreakdown]]


class Calldata(TypedDict, total=False):
    data: str
    to: str
    value: str
    chainId: int


class BRAPBridgeTracking(TypedDict, total=False):
    provider: Required[str]
    requires_source_tx_hash: Required[bool]
    from_chain: int | None
    to_chain: int | None
    bridge: str | None
    protocol: str | None
    order_id: str | None


class BRAPQuoteEntry(TypedDict):
    provider: Required[str]
    quote: Required[QuoteData]
    bridge_tracking: NotRequired[BRAPBridgeTracking | None]
    calldata: Required[Calldata]
    output_amount: Required[int]
    input_amount: Required[int]
    gas_estimate: NotRequired[int | None]
    error: NotRequired[str | None]
    input_amount_usd: Required[float]
    output_amount_usd: Required[float]
    fee_estimate: Required[FeeEstimate]
    wrap_transaction: NotRequired[dict[str, Any] | None]
    unwrap_transaction: NotRequired[dict[str, Any] | None]
    native_input: Required[bool]
    native_output: Required[bool]


class BRAPQuoteResponse(TypedDict, total=False):
    quotes: Required[list[BRAPQuoteEntry]]
    best_quote: Required[BRAPQuoteEntry | None]
    quote_count: NotRequired[int]
    errors: NotRequired[list[dict[str, Any]]]


class BRAPBridgeExecutionStatus(TypedDict, total=False):
    provider: str | None
    state: str
    provider_status: str | None
    provider_substatus: str | None
    message: str | None
    error: str | None
    source_tx_hash: str | None
    source_tx_url: str | None
    source_chain_id: int | None
    destination_tx_hash: str | None
    destination_tx_url: str | None
    destination_chain_id: int | None
    bridge_tool: str | None
    bridge_protocol: str | None
    provider_tracking_id: str | None
    provider_explorer_url: str | None
    estimated_seconds_remaining: int | float | None
    next_poll_seconds: int | float | None
    is_finished: bool
    is_success: bool
    raw_status: dict[str, Any]
    status: dict[str, Any]


def normalize_brap_quote_response(data: Any) -> BRAPQuoteResponse:
    """Normalize legacy and current BRAP quote response shapes.

    Historical SDK/API callers have seen both:
    - {"quotes": [...], "best_quote": {...}}
    - {"quotes": {"all_quotes": [...], "best_quote": {...}, "quote_count": N}}
    """
    payload = data.get("data", data) if isinstance(data, dict) else {}
    if not isinstance(payload, dict):
        return {"quotes": [], "best_quote": None, "quote_count": 0}

    raw_quotes = payload.get("quotes")
    raw_best_quote = payload.get("best_quote")
    raw_quote_count = payload.get("quote_count")
    raw_errors = payload.get("errors")
    legacy_response = payload.get("legacy_quote_response")
    legacy_quotes = (
        legacy_response.get("quotes")
        if isinstance(legacy_response, dict)
        and isinstance(legacy_response.get("quotes"), dict)
        else None
    )

    if isinstance(raw_quotes, dict):
        nested_quotes = raw_quotes.get("all_quotes") or raw_quotes.get("quotes")
        if raw_best_quote is None:
            raw_best_quote = raw_quotes.get("best_quote")
        if raw_quote_count is None:
            raw_quote_count = raw_quotes.get("quote_count")
        if raw_errors is None:
            raw_errors = raw_quotes.get("errors")
        raw_quotes = nested_quotes
    elif legacy_quotes is not None:
        raw_quotes = legacy_quotes.get("all_quotes") or legacy_quotes.get("quotes")
        if raw_best_quote is None:
            raw_best_quote = legacy_quotes.get("best_quote")
        if raw_quote_count is None:
            raw_quote_count = legacy_quotes.get("quote_count")
        if raw_errors is None:
            raw_errors = legacy_quotes.get("errors")

    quotes = (
        [q for q in raw_quotes if isinstance(q, dict)]
        if isinstance(raw_quotes, list)
        else []
    )
    best_quote = raw_best_quote if isinstance(raw_best_quote, dict) else None

    try:
        quote_count = int(raw_quote_count)
    except (TypeError, ValueError):
        quote_count = len(quotes)

    response: BRAPQuoteResponse = {
        "quotes": cast(list[BRAPQuoteEntry], quotes),
        "best_quote": cast(BRAPQuoteEntry | None, best_quote),
        "quote_count": quote_count,
    }
    if isinstance(raw_errors, list):
        response["errors"] = [e for e in raw_errors if isinstance(e, dict)]
    return response


class BRAPClient(WayfinderClient):
    async def get_quote(
        self,
        *,
        from_token: str,
        to_token: str,
        from_chain: int,
        to_chain: int,
        from_wallet: str,
        from_amount: str,
        slippage: float | None = None,
    ) -> BRAPQuoteResponse:  # type: ignore # noqa: E501
        logger.info(
            f"Getting BRAP quote: {from_token} -> {to_token} (chain {from_chain} -> {to_chain})"
        )
        logger.debug(f"Quote params: amount={from_amount}")
        start_time = time.time()

        url = f"{get_api_base_url()}/blockchain/braps/quote/"

        params: dict[str, Any] = {
            "from_token": from_token,
            "to_token": to_token,
            "from_chain": from_chain,
            "to_chain": to_chain,
            "from_wallet": from_wallet,
            "from_amount": from_amount,
        }
        if slippage is not None:
            params["slippage"] = slippage

        try:
            response = await self._authed_request("GET", url, params=params, headers={})
            response.raise_for_status()
            result = normalize_brap_quote_response(response.json())

            elapsed = time.time() - start_time
            logger.info(f"BRAP quote request completed successfully in {elapsed:.2f}s")
            return result
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"BRAP quote request failed after {elapsed:.2f}s: {e}")
            raise

    async def get_bridge_execution_status(
        self,
        *,
        bridge_tracking: BRAPBridgeTracking | dict[str, Any] | None = None,
        provider: str | None = None,
        tx_hash: str | None = None,
        from_chain: int | None = None,
        to_chain: int | None = None,
        bridge: str | None = None,
        protocol: str | None = None,
        quote: dict[str, Any] | None = None,
        order_id: str | None = None,
    ) -> BRAPBridgeExecutionStatus:
        """Fetch the latest normalized bridge execution status without polling."""
        tracking = bridge_tracking or {}
        url = f"{get_api_base_url()}/blockchain/braps/bridge-execution-status/"
        payload = {
            "bridge_tracking": tracking,
            "provider": provider or tracking.get("provider"),
            "tx_hash": tx_hash,
            "from_chain": from_chain or tracking.get("from_chain"),
            "to_chain": to_chain or tracking.get("to_chain"),
            "bridge": bridge or tracking.get("bridge"),
            "protocol": protocol or tracking.get("protocol"),
            "quote": quote,
            "order_id": order_id or tracking.get("order_id"),
        }

        response = await self._authed_request("POST", url, json=payload, headers={})
        data = response.json()
        return data.get("data", data)

    async def wait_for_bridge_execution(
        self,
        *,
        bridge_tracking: BRAPBridgeTracking | dict[str, Any] | None = None,
        provider: str | None = None,
        tx_hash: str | None = None,
        from_chain: int | None = None,
        to_chain: int | None = None,
        bridge: str | None = None,
        protocol: str | None = None,
        quote: dict[str, Any] | None = None,
        order_id: str | None = None,
        timeout_seconds: float = 600.0,
    ) -> BRAPBridgeExecutionStatus:
        """Poll normalized bridge status until a terminal state or timeout."""
        start_time = time.time()
        while True:
            status = await self.get_bridge_execution_status(
                bridge_tracking=bridge_tracking,
                provider=provider,
                tx_hash=tx_hash,
                from_chain=from_chain,
                to_chain=to_chain,
                bridge=bridge,
                protocol=protocol,
                quote=quote,
                order_id=order_id,
            )
            if status.get("is_finished"):
                return status
            if time.time() - start_time > timeout_seconds:
                raise TimeoutError(
                    f"Bridge execution did not finish within {timeout_seconds:.0f}s"
                )
            next_poll = status.get("next_poll_seconds")
            try:
                sleep_seconds = float(next_poll)
            except (TypeError, ValueError):
                sleep_seconds = 5.0
            await asyncio.sleep(max(1.0, min(sleep_seconds, 30.0)))


BRAP_CLIENT = BRAPClient()
