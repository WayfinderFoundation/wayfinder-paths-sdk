from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from wayfinder_paths.core.clients.direct.DefiLlamaFreeClient import (
    DEFILLAMA_FREE_CLIENT,
)
from wayfinder_paths.mcp.arg_validation import (
    MCPArgumentError,
    normalize_enum,
    normalize_int,
    optional_str,
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
DefiLlamaDataset = Literal[
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
]


def _require_dataset_argument(
    value: Any,
    *,
    field_name: str,
    dataset: str,
    example: Any,
) -> str:
    parsed = optional_str(value, field_name=field_name)
    if parsed is None:
        raise MCPArgumentError(
            f"{field_name} is required when dataset={dataset}",
            field=field_name,
            received=value,
            suggested_arguments={"dataset": dataset, field_name: example},
        )
    return parsed


@catch_errors
async def research_defillama_free(
    dataset: DefiLlamaDataset,
    protocolSlug: Annotated[
        str,
        Field(description="Exact protocol slug; required by protocol/TVL datasets."),
    ] = "_",
    chain: str = "_",
    coins: Annotated[
        str,
        Field(
            description=(
                "Comma-separated DefiLlama coin ids such as ethereum:0x...; "
                "required for current_prices."
            )
        ),
    ] = "_",
    query: str = "_",
    dataType: str = "dailyFees",
    days: str | int = "30",
    limit: str | int = "25",
    cursor: str = "_",
) -> dict[str, Any]:
    """Read a free DeFiLlama dataset.

    `dataset` selects protocols/search/detail/TVL/fees, chains, stablecoins,
    yield pools, prices, DEX/fees, or open-interest data. Detail/TVL datasets
    require `protocolSlug`; search requires `query`; prices require
    `coins` such as `ethereum:0x...`. Use returned cursors for pagination.
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
        search_query = _require_dataset_argument(
            query,
            field_name="query",
            dataset=normalized,
            example="aave",
        )
        return ok(await DEFILLAMA_FREE_CLIENT.protocol_search(search_query, page_limit))
    if normalized == "protocol":
        slug = _require_dataset_argument(
            protocolSlug,
            field_name="protocolSlug",
            dataset=normalized,
            example="aave",
        )
        return ok(await DEFILLAMA_FREE_CLIENT.protocol(slug))
    if normalized == "tvl":
        slug = _require_dataset_argument(
            protocolSlug,
            field_name="protocolSlug",
            dataset=normalized,
            example="aave",
        )
        return ok(await DEFILLAMA_FREE_CLIENT.tvl(slug))
    if normalized == "protocol_fees":
        slug = _require_dataset_argument(
            protocolSlug,
            field_name="protocolSlug",
            dataset=normalized,
            example="aave",
        )
        return ok(
            await DEFILLAMA_FREE_CLIENT.protocol_fees(
                slug,
                data_type=dataType,
                days=normalize_int(days, field_name="days", min_value=1),
            )
        )
    if normalized == "protocol_tvl_history":
        slug = _require_dataset_argument(
            protocolSlug,
            field_name="protocolSlug",
            dataset=normalized,
            example="aave",
        )
        return ok(
            await DEFILLAMA_FREE_CLIENT.protocol_tvl_history(
                slug,
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
        coin_ids = _require_dataset_argument(
            coins,
            field_name="coins",
            dataset=normalized,
            example="ethereum:0x0000000000000000000000000000000000000000",
        )
        return ok(await DEFILLAMA_FREE_CLIENT.current_prices(coin_ids))
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
