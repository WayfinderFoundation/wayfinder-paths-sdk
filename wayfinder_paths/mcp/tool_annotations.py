"""Reusable JSON-schema annotations for MCP parameters with non-obvious formats."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

HumanTokenAmount = Annotated[
    str,
    Field(
        description=(
            "Positive decimal token units as a string, e.g. '1.25'; "
            "include a decimal point and never pass wei/base units."
        )
    ),
]

TokenQuery = Annotated[
    str,
    Field(
        description=(
            "Canonical token id from onchain_resolve_token, or an unambiguous "
            "symbol/address accepted by the resolver."
        )
    ),
]

ChainRecipient = Annotated[
    str,
    Field(
        description=(
            "Destination address for the resolved chain (0x EVM address or "
            "base58 Solana address)."
        )
    ),
]

ChainCode = Annotated[
    str,
    Field(
        description=(
            "Canonical chain code such as ethereum, base, arbitrum, polygon, "
            "bsc, avalanche, plasma, or hyperevm."
        )
    ),
]

ChainFilter = Annotated[
    str | int,
    Field(
        description=(
            "Canonical chain code or numeric chain id; use 'all' or '_' to omit."
        )
    ),
]

JsonObjectInput = Annotated[
    dict[str, Any] | str,
    Field(
        description=(
            "A JSON object or an object encoded as JSON text; optional provider "
            "fields also accept '_' to mean omitted."
        )
    ),
]

GoldskyEndpoint = Annotated[
    str,
    Field(
        description=(
            "Full https://api.goldsky.com/api/public/.../gn or "
            "/api/private/.../gn endpoint returned by research_goldsky_search."
        )
    ),
]

ProviderEndpointId = Annotated[
    str,
    Field(
        description=(
            "Allowlisted endpoint id returned by the provider catalog; never a URL."
        )
    ),
]

HyperliquidAssetName = Annotated[
    str,
    Field(
        description=(
            "Canonical id from hyperliquid_search_market: BTC-USDC (perp), "
            "xyz:SP500 (HIP-3), BTC/USDC (spot), or #40 (HIP-4)."
        )
    ),
]

DeltaInstrumentType = Annotated[
    str,
    Field(
        description=(
            "PERP, LENDING_SUPPLY, LENDING_BORROW, BOROS_MARKET, PENDLE_PT "
            "(PT alias accepted), YIELD_TOKEN, or all."
        )
    ),
]

HyperliquidIsBuy = Annotated[
    bool,
    Field(
        description=(
            "True buys/opens long; false sells/opens short. To close a perp, "
            "use the opposite side with reduce_only=true."
        )
    ),
]

HyperliquidSize = Annotated[
    float | None,
    Field(
        description=(
            "Order quantity in asset units. Pass exactly one of size or "
            "usd_amount; HIP-4 sizes must be whole contracts."
        )
    ),
]

HyperliquidRequiredSize = Annotated[
    float,
    Field(description="Positive quantity in asset units; rounded down to lot size."),
]

HyperliquidUsdAmount = Annotated[
    float | None,
    Field(
        description=(
            "USD notional to convert into asset size. Pass exactly one of "
            "usd_amount or size; unavailable for HIP-4 limit orders."
        )
    ),
]

SlippageFraction = Annotated[
    float,
    Field(description="Decimal fraction, e.g. 0.01 means 1%; not percent points."),
]

SlippageBps = Annotated[
    int,
    Field(description="Basis points, e.g. 50 means 0.5%; must be non-negative."),
]

ContractAddress = Annotated[
    str,
    Field(description="20-byte EVM address in 0x-prefixed hexadecimal form."),
]

WeiValue = Annotated[
    int,
    Field(description="Optional native value in raw wei; use 0 for no value."),
]

ContractFunctionSignature = Annotated[
    str | None,
    Field(
        description=(
            "Exact ABI signature for an overload, e.g. deposit(uint256); "
            "omit when function_name is unambiguous."
        )
    ),
]

ContractArguments = Annotated[
    list[Any] | str | None,
    Field(
        description=(
            "ABI-ordered argument list, either as a JSON array or JSON-array string."
        )
    ),
]

ContractAbi = Annotated[
    list[dict[str, Any]] | str | None,
    Field(
        description=(
            "ABI array/object or JSON text. Omit with abi_path to use local/"
            "Etherscan resolution; never pass abi and abi_path together."
        )
    ),
]

RepoJsonPath = Annotated[
    str | None,
    Field(description="Repository-local path to a .json ABI artifact."),
]

RunnerJobType = Annotated[
    Literal["strategy", "script"] | None,
    Field(
        description=(
            "Job payload kind for add_job; strategy needs strategy, script "
            "needs script_path."
        )
    ),
]

CronExpression = Annotated[
    str | None,
    Field(
        description=(
            "Standard 5-field cron expression, e.g. '0 */6 * * *'; pass exactly "
            "one of cron_expr or interval_seconds."
        )
    ),
]

IanaTimezone = Annotated[
    str | None,
    Field(description="IANA timezone such as UTC or America/Toronto; cron jobs only."),
]

RepoPythonScript = Annotated[
    str | None,
    Field(description="Repository-local .py path, normally under .wayfinder_runs/."),
]

RunsPythonScript = Annotated[
    str,
    Field(
        description=(
            "Existing .py file inside .wayfinder_runs/; absolute paths outside "
            "that directory are rejected."
        )
    ),
]

PolymarketMarketSlug = Annotated[
    str | None,
    Field(
        description=(
            "Exact market slug from polymarket_read search/get_event; "
            "pair with outcome and omit token_id."
        )
    ),
]

PolymarketTokenId = Annotated[
    str | None,
    Field(
        description=("Exact CLOB outcome token id; use instead of market_slug+outcome.")
    ),
]

PolymarketOutcome = Annotated[
    str | int,
    Field(
        description=(
            "Outcome label such as YES/NO or its zero-based index; used only "
            "with market_slug."
        )
    ),
]

PolymarketBuyAmount = Annotated[
    float | None,
    Field(
        description=("Positive pUSD collateral to spend; required only when side=BUY.")
    ),
]

PolymarketSellShares = Annotated[
    float | None,
    Field(description="Positive outcome shares to sell; required only when side=SELL."),
]

PolymarketProbability = Annotated[
    float,
    Field(description="Limit probability strictly between 0 and 1, e.g. 0.62."),
]

PolymarketShares = Annotated[
    float,
    Field(description="Positive outcome-token share quantity; not pUSD collateral."),
]

SlippagePercentPoints = Annotated[
    float | None,
    Field(description="Percent points, e.g. 2.0 means 2%; not the fraction 0.02."),
]

ChartSeriesSpecs = Annotated[
    list[dict[str, Any]],
    Field(
        description=(
            "One or more {id, source, x?, y?, transforms?} specs; copy "
            "dataset sources from visual_search_chart_series."
        )
    ),
]

ChartKind = Annotated[
    str,
    Field(description="One of price_candle, line, bar, or table."),
]

ChartTransforms = Annotated[
    list[dict[str, Any]] | None,
    Field(
        description=(
            "Optional chart-wide transform objects; prefer series transforms "
            "unless every series should change."
        )
    ),
]

ChartIndicatorList = Annotated[
    list[dict[str, Any]],
    Field(description="Replacement list of TradingView {name, inputs?} studies."),
]

ChartIndicators = Annotated[
    list[dict[str, Any]] | None,
    Field(
        description=(
            "TradingView studies as {name, inputs?}; only supported on "
            "TradingView-backed charts."
        )
    ),
]
