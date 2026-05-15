from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.direct.DefiLlamaFreeClient import (
    DEFILLAMA_FREE_CLIENT,
)
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
    dataType: str = "dailyFees",
    days: str = "30",
    limit: str = "10",
) -> dict[str, Any]:
    """Call DeFiLlama free APIs directly from the OpenCode runtime.

    Args:
        dataset: protocols, protocol_search, protocol, tvl, protocol_fees,
            protocol_tvl_history, chains, stablecoins, yields_pools,
            current_prices, dex_overview, fees_overview, or open_interest_overview.
        protocolSlug: Required for protocol/tvl/protocol_fees/protocol_tvl_history.
        chain: Optional for dex_overview and fees_overview.
        coins: Required for current_prices, e.g. ethereum:0xa0b8...
        query: Required for protocol_search.
        dataType: For protocol_fees: dailyFees or dailyRevenue.
        days: Lookback days for protocol_fees/protocol_tvl_history.
        limit: Result cap for protocol_search.
    """
    normalized = dataset.strip().lower()
    if normalized not in DATASETS:
        raise ValueError(f"dataset must be one of: {', '.join(sorted(DATASETS))}")

    if normalized == "protocols":
        return ok(await DEFILLAMA_FREE_CLIENT.protocols())
    if normalized == "protocol_search":
        if query == "_":
            raise ValueError("query is required for dataset=protocol_search")
        return ok(await DEFILLAMA_FREE_CLIENT.protocol_search(query, int(limit)))
    if normalized == "protocol":
        if protocolSlug == "_":
            raise ValueError("protocolSlug is required for dataset=protocol")
        return ok(await DEFILLAMA_FREE_CLIENT.protocol(protocolSlug))
    if normalized == "tvl":
        if protocolSlug == "_":
            raise ValueError("protocolSlug is required for dataset=tvl")
        return ok(await DEFILLAMA_FREE_CLIENT.tvl(protocolSlug))
    if normalized == "protocol_fees":
        if protocolSlug == "_":
            raise ValueError("protocolSlug is required for dataset=protocol_fees")
        return ok(
            await DEFILLAMA_FREE_CLIENT.protocol_fees(
                protocolSlug,
                data_type=dataType,
                days=int(days),
            )
        )
    if normalized == "protocol_tvl_history":
        if protocolSlug == "_":
            raise ValueError("protocolSlug is required for dataset=protocol_tvl_history")
        return ok(
            await DEFILLAMA_FREE_CLIENT.protocol_tvl_history(
                protocolSlug,
                days=int(days),
            )
        )
    if normalized == "chains":
        return ok(await DEFILLAMA_FREE_CLIENT.chains())
    if normalized == "stablecoins":
        return ok(await DEFILLAMA_FREE_CLIENT.stablecoins())
    if normalized == "yields_pools":
        return ok(await DEFILLAMA_FREE_CLIENT.yields_pools())
    if normalized == "current_prices":
        if coins == "_":
            raise ValueError("coins is required for dataset=current_prices")
        return ok(await DEFILLAMA_FREE_CLIENT.current_prices(coins))
    if normalized == "dex_overview":
        return ok(
            await DEFILLAMA_FREE_CLIENT.dex_overview(None if chain == "_" else chain)
        )
    if normalized == "fees_overview":
        return ok(
            await DEFILLAMA_FREE_CLIENT.fees_overview(None if chain == "_" else chain)
        )
    if normalized == "open_interest_overview":
        return ok(await DEFILLAMA_FREE_CLIENT.open_interest_overview())

    raise ValueError("unsupported dataset")
