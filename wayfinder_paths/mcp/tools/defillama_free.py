from __future__ import annotations

from typing import Any, Literal

from wayfinder_paths.core.clients.direct.DefiLlamaFreeClient import (
    DEFILLAMA_FREE_CLIENT,
)
from wayfinder_paths.mcp.arg_validation import normalize_enum, normalize_int
from wayfinder_paths.mcp.utils import catch_errors, ok

DATASETS = {
    "protocols",
    "protocol_search",
    "protocol",
    "tvl",
    "protocol_fees",
    "protocol_tvl_history",
    "chains",
    "stablecoins",
    "yields_pools",
    "current_prices",
    "dex_overview",
    "fees_overview",
    "open_interest_overview",
}


@catch_errors
async def research_defillama_free(
    dataset: str,
    protocolSlug: str = "_",
    chain: str = "_",
    coins: str = "_",
    query: str = "_",
    dataType: Literal["dailyFees", "dailyRevenue", "dailyHoldersRevenue"] = "dailyFees",
    days: str | int = "30",
    limit: str | int = "25",
    cursor: str = "_",
    includeChainBreakdown: bool = False,
    category: str = "_",
    protocolSlugs: list[str] | None = None,
) -> dict[str, Any]:
    """Call DeFiLlama free APIs directly from the OpenCode runtime.

    Args:
        dataset: protocols, protocol_search, protocol, tvl, protocol_fees,
            protocol_tvl_history, chains, stablecoins, yields_pools,
            current_prices, dex_overview, fees_overview, or open_interest_overview.
            protocol returns metadata/current chain TVL, not bulk historical
            arrays. Use protocol_tvl_history with days for bounded TVL history.
            open_interest_overview reports outstanding-notional snapshots, not
            trading volume. Its multi-day totals are provider aggregates, not
            current open interest, revenue or new positions opened in that period.
        protocolSlug: Required for protocol/tvl/protocol_fees/protocol_tvl_history.
        chain: Optional for dex_overview and fees_overview.
        coins: Required for current_prices, e.g. ethereum:0xa0b8...
        query: Text search for protocol_search; optional when category is given.
        category: Exact DeFiLlama category (case insensitive), applied before pagination.
        protocolSlugs: Optional exact returned slugs for fees_overview, filtered
            before pagination. Use bulk overview before per-finalist histories.
        dataType: For protocol_fees/fees_overview: dailyFees, dailyRevenue or dailyHoldersRevenue.
            Holder revenue can include buybacks/burns or distributions to eligible
            stakers; it is not necessarily cash income to every spot holder.
            Missing data is unavailable, not zero holder value. Read the returned
            provider methodology before interpreting a metric as business revenue.
            Totals are provider-reported periods, not annualized projections.
            change_1m compares the latest day with the day a month ago, NOT
            rolling-month growth; use change_30dover30d and total60dto30d for
            that comparison. total1y is a trailing total, not the annualized
            current pace. Fees and buybacks funded from those fees are the same
            flow at different stages, not additive revenue. Each row's slug,
            parentProtocol and chains define its scope, not the entire business.
        days: Lookback days for protocol_fees/protocol_tvl_history.
        limit: Result cap for page-able collection datasets.
        cursor: Page cursor returned by a prior response, or "_".
        includeChainBreakdown: For protocol_fees, include the large per-chain
            daily breakdown. Defaults to false; aggregate daily rows, reported
            totals and methodology remain available. Use a short days window
            when a chain-by-chain comparison is needed.
    """
    normalized = normalize_enum(
        dataset,
        field_name="dataset",
        allowed_values=DATASETS,
    )

    page_limit = normalize_int(limit, field_name="limit", min_value=1)

    if normalized == "protocols":
        return ok(
            await DEFILLAMA_FREE_CLIENT.protocols_page(
                limit=page_limit,
                cursor=cursor,
            )
        )
    if normalized == "protocol_search":
        return ok(
            await DEFILLAMA_FREE_CLIENT.protocol_search(
                query, page_limit, cursor=cursor, category=category
            )
        )
    if normalized == "protocol":
        if protocolSlug == "_":
            raise ValueError("protocolSlug is required for dataset=protocol")
        response = await DEFILLAMA_FREE_CLIENT.protocol(protocolSlug)
        metadata = response["result"]
        if not isinstance(metadata, dict):
            raise ValueError("DeFiLlama protocol response is not an object")
        history_fields = ("tvl", "chainTvls", "tokens", "tokensInUsd")
        return ok(
            {
                **response,
                "result": {
                    **{
                        key: value
                        for key, value in metadata.items()
                        if key not in history_fields
                    },
                    "historicalFieldsOmitted": [
                        key for key in history_fields if key in metadata
                    ],
                },
            }
        )
    if normalized == "tvl":
        if protocolSlug == "_":
            raise ValueError("protocolSlug is required for dataset=tvl")
        return ok(await DEFILLAMA_FREE_CLIENT.tvl(protocolSlug))
    if normalized == "protocol_fees":
        if protocolSlug == "_":
            raise ValueError("protocolSlug is required for dataset=protocol_fees")
        response = await DEFILLAMA_FREE_CLIENT.protocol_fees(
            protocolSlug,
            data_type=dataType,
            days=normalize_int(days, field_name="days", min_value=1),
        )
        if not includeChainBreakdown:
            response["result"].pop("chainDailyRows", None)
            response["result"]["chainDailyRowsOmitted"] = True
        return ok(response)
    if normalized == "protocol_tvl_history":
        if protocolSlug == "_":
            raise ValueError(
                "protocolSlug is required for dataset=protocol_tvl_history"
            )
        return ok(
            await DEFILLAMA_FREE_CLIENT.protocol_tvl_history(
                protocolSlug,
                days=normalize_int(days, field_name="days", min_value=1),
            )
        )
    if normalized == "chains":
        return ok(
            await DEFILLAMA_FREE_CLIENT.chains(
                limit=page_limit,
                cursor=cursor,
            )
        )
    if normalized == "stablecoins":
        return ok(
            await DEFILLAMA_FREE_CLIENT.stablecoins(
                limit=page_limit,
                cursor=cursor,
            )
        )
    if normalized == "yields_pools":
        return ok(
            await DEFILLAMA_FREE_CLIENT.yields_pools(
                limit=page_limit,
                cursor=cursor,
            )
        )
    if normalized == "current_prices":
        if coins == "_":
            raise ValueError("coins is required for dataset=current_prices")
        return ok(await DEFILLAMA_FREE_CLIENT.current_prices(coins))
    if normalized == "dex_overview":
        return ok(
            await DEFILLAMA_FREE_CLIENT.dex_overview(
                None if chain == "_" else chain,
                limit=page_limit,
                cursor=cursor,
            )
        )
    if normalized == "fees_overview":
        return ok(
            await DEFILLAMA_FREE_CLIENT.fees_overview(
                None if chain == "_" else chain,
                limit=page_limit,
                cursor=cursor,
                data_type=dataType,
                protocol_slugs=protocolSlugs,
            )
        )
    if normalized == "open_interest_overview":
        return ok(
            await DEFILLAMA_FREE_CLIENT.open_interest_overview(
                limit=page_limit,
                cursor=cursor,
            )
        )

    raise ValueError("unsupported dataset")
