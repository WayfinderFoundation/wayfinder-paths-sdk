from __future__ import annotations

from typing import Any

from wayfinder_paths.core.constants.projectx import PRJX_SUBGRAPH_URL

GOLDSKY_ENDPOINTS: list[dict[str, Any]] = [
    {
        "id": "projectx.uniswap_v3_hyperevm_position.prod",
        "label": "ProjectX Uniswap V3 HypereVM positions and swaps",
        "chain": "hyperevm",
        "protocol": "projectx",
        "dataset": "uniswap_v3_positions_swaps",
        "endpoint": PRJX_SUBGRAPH_URL,
        "schemaSummary": {
            "entities": ["positions", "swaps"],
            "commonFields": {
                "positions": [
                    "id",
                    "owner",
                    "pool",
                    "tickLower",
                    "tickUpper",
                    "liquidity",
                ],
                "swaps": [
                    "id",
                    "timestamp",
                    "tick",
                    "sqrtPriceX96",
                    "amount0",
                    "amount1",
                    "amountUSD",
                ],
            },
        },
    }
]


def search_goldsky_endpoints(
    *,
    query: str | None = None,
    chain: str | None = None,
    protocol: str | None = None,
    dataset: str | None = None,
) -> list[dict[str, Any]]:
    needle = _normalize(query)
    chain_filter = _normalize(chain)
    protocol_filter = _normalize(protocol)
    dataset_filter = _normalize(dataset)

    results = []
    for endpoint in GOLDSKY_ENDPOINTS:
        searchable = " ".join(
            str(endpoint.get(key) or "")
            for key in ("id", "label", "chain", "protocol", "dataset")
        ).lower()
        if needle and needle not in searchable:
            continue
        if chain_filter and chain_filter != _normalize(endpoint.get("chain")):
            continue
        if protocol_filter and protocol_filter != _normalize(endpoint.get("protocol")):
            continue
        if dataset_filter and dataset_filter != _normalize(endpoint.get("dataset")):
            continue
        results.append(endpoint)
    return results


def goldsky_schema(endpoint_id: str) -> dict[str, Any]:
    normalized = _normalize(endpoint_id)
    for endpoint in GOLDSKY_ENDPOINTS:
        if _normalize(endpoint["id"]) == normalized:
            return endpoint
    raise ValueError("unknown Goldsky endpointId")


def _normalize(value: Any) -> str:
    return str(value or "").strip().lower()
