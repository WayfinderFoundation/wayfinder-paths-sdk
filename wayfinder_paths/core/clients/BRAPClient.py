from __future__ import annotations

import time
from typing import Any, NotRequired, Required, TypedDict

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


class BRAPQuoteEntry(TypedDict):
    provider: Required[str]
    quote: Required[QuoteData]
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


class BRAPQuoteResponse(TypedDict):
    quotes: Required[list[BRAPQuoteEntry]]
    best_quote: Required[BRAPQuoteEntry]


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
            data = response.json()
            result = data.get("data", data)

            elapsed = time.time() - start_time
            logger.info(f"BRAP quote request completed successfully in {elapsed:.2f}s")
            return result
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"BRAP quote request failed after {elapsed:.2f}s: {e}")
            raise


BRAP_CLIENT = BRAPClient()
