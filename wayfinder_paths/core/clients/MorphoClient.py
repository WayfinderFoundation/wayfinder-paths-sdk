from __future__ import annotations

from typing import Any, Required, TypedDict

import httpx
from loguru import logger

from wayfinder_paths.core.constants.base import DEFAULT_HTTP_TIMEOUT

MORPHO_GRAPHQL_URL = "https://api.morpho.org/graphql"


class MorphoChain(TypedDict):
    id: Required[int]
    network: Required[str]


class MorphoBlueDeployment(TypedDict):
    address: Required[str]
    chain: Required[MorphoChain]


class PublicAllocatorItem(TypedDict):
    address: Required[str]
    morphoBlue: Required[MorphoBlueDeployment]


class MorphoClient:
    def __init__(self, *, graphql_url: str = MORPHO_GRAPHQL_URL) -> None:
        self.graphql_url = str(graphql_url)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(DEFAULT_HTTP_TIMEOUT))
        self.headers = {"Content-Type": "application/json"}
        self._morpho_by_chain_cache: dict[int, dict[str, str]] | None = None

    async def _post(self, *, query: str, variables: dict[str, Any] | None = None) -> Any:
        resp = await self.client.post(
            self.graphql_url,
            headers=self.headers,
            json={"query": query, "variables": variables or {}},
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("errors"):
            raise ValueError(f"Morpho GraphQL errors: {data['errors']}")
        return data.get("data", data)

    async def get_morpho_by_chain(
        self, *, force_refresh: bool = False
    ) -> dict[int, dict[str, str]]:
        if self._morpho_by_chain_cache is not None and not force_refresh:
            return self._morpho_by_chain_cache

        query = """
        query PublicAllocators($first: Int!) {
          publicAllocators(first: $first) {
            items {
              address
              morphoBlue {
                address
                chain { id network }
              }
            }
          }
        }
        """
        payload = await self._post(query=query, variables={"first": 1000})
        items = (
            (((payload or {}).get("publicAllocators") or {}).get("items") or [])
            if isinstance(payload, dict)
            else []
        )

        by_chain: dict[int, dict[str, str]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            morpho_blue = item.get("morphoBlue") or {}
            chain = morpho_blue.get("chain") or {}
            try:
                chain_id = int(chain.get("id"))
            except (TypeError, ValueError):
                continue
            morpho_addr = morpho_blue.get("address")
            allocator = item.get("address")
            network = chain.get("network")
            if not (morpho_addr and allocator and network):
                continue
            by_chain[chain_id] = {
                "network": str(network),
                "morpho": str(morpho_addr),
                "public_allocator": str(allocator),
            }

        if not by_chain:
            logger.warning("Morpho API returned no deployments")

        self._morpho_by_chain_cache = by_chain
        return by_chain

    async def get_morpho_address(
        self, *, chain_id: int, force_refresh: bool = False
    ) -> str:
        by_chain = await self.get_morpho_by_chain(force_refresh=force_refresh)
        entry = by_chain.get(int(chain_id))
        if not entry:
            raise ValueError(f"Morpho deployment not found for chain_id={chain_id}")
        return str(entry["morpho"])

    async def get_all_markets(
        self,
        *,
        chain_id: int,
        listed: bool | None = True,
        include_idle: bool = False,
        page_size: int = 200,
        max_pages: int = 50,
    ) -> list[dict[str, Any]]:
        query = """
        query Markets($first: Int, $skip: Int, $where: MarketFilters) {
          markets(first: $first, skip: $skip, where: $where) {
            items {
              uniqueKey
              lltv
              irmAddress
              listed
              loanAsset { address symbol name decimals priceUsd }
              collateralAsset { address symbol name decimals priceUsd }
              oracle { address }
              state {
                supplyApy
                netSupplyApy
                borrowApy
                netBorrowApy
                utilization
                apyAtTarget
                liquidityAssets
                liquidityAssetsUsd
                supplyAssets
                supplyAssetsUsd
                borrowAssets
                borrowAssetsUsd
              }
            }
            pageInfo { countTotal count limit skip }
          }
        }
        """

        where: dict[str, Any] = {"chainId_in": [int(chain_id)]}
        if listed is not None:
            where["listed"] = bool(listed)
        if not include_idle:
            where["isIdle"] = False

        items_out: list[dict[str, Any]] = []
        skip = 0
        for _ in range(max_pages):
            payload = await self._post(
                query=query,
                variables={"first": int(page_size), "skip": int(skip), "where": where},
            )
            page = (payload or {}).get("markets") if isinstance(payload, dict) else None
            items = (page or {}).get("items") or []
            if not items:
                break
            items_out.extend([i for i in items if isinstance(i, dict)])

            page_info = (page or {}).get("pageInfo") or {}
            try:
                count = int(page_info.get("count") or len(items))
                total = int(page_info.get("countTotal") or 0)
            except (TypeError, ValueError):
                count = len(items)
                total = 0

            skip += count
            if total and skip >= total:
                break

        return items_out

    async def get_market_by_unique_key(
        self, *, unique_key: str, chain_id: int | None = None
    ) -> dict[str, Any]:
        query = """
        query Market($k: String!, $chainId: Int) {
          marketByUniqueKey(uniqueKey: $k, chainId: $chainId) {
            uniqueKey
            lltv
            irmAddress
            listed
            loanAsset { address symbol name decimals priceUsd }
            collateralAsset { address symbol name decimals priceUsd }
            oracle { address }
            state {
              supplyApy
              netSupplyApy
              borrowApy
              netBorrowApy
              utilization
              apyAtTarget
              liquidityAssets
              liquidityAssetsUsd
              supplyAssets
              supplyAssetsUsd
              borrowAssets
              borrowAssetsUsd
            }
          }
        }
        """
        payload = await self._post(
            query=query, variables={"k": str(unique_key), "chainId": chain_id}
        )
        market = (payload or {}).get("marketByUniqueKey") if isinstance(payload, dict) else None
        if not isinstance(market, dict):
            raise ValueError(f"Market not found for uniqueKey={unique_key}")
        return market

    async def get_all_market_positions(
        self,
        *,
        user_address: str,
        chain_id: int | None = None,
        page_size: int = 200,
        max_pages: int = 50,
    ) -> list[dict[str, Any]]:
        query = """
        query MarketPositions($first: Int, $skip: Int, $where: MarketPositionFilters) {
          marketPositions(first: $first, skip: $skip, where: $where) {
            items {
              healthFactor
              market {
                uniqueKey
                lltv
                irmAddress
                listed
                loanAsset { address symbol name decimals priceUsd }
                collateralAsset { address symbol name decimals priceUsd }
                oracle { address }
              }
              state {
                collateral
                supplyAssets
                supplyAssetsUsd
                supplyShares
                borrowAssets
                borrowAssetsUsd
                borrowShares
              }
            }
            pageInfo { countTotal count limit skip }
          }
        }
        """

        where: dict[str, Any] = {"userAddress_in": [str(user_address)]}
        if chain_id is not None:
            where["chainId_in"] = [int(chain_id)]

        out: list[dict[str, Any]] = []
        skip = 0
        for _ in range(max_pages):
            payload = await self._post(
                query=query,
                variables={"first": int(page_size), "skip": int(skip), "where": where},
            )
            page = (
                (payload or {}).get("marketPositions") if isinstance(payload, dict) else None
            )
            items = (page or {}).get("items") or []
            if not items:
                break
            out.extend([i for i in items if isinstance(i, dict)])

            page_info = (page or {}).get("pageInfo") or {}
            try:
                count = int(page_info.get("count") or len(items))
                total = int(page_info.get("countTotal") or 0)
            except (TypeError, ValueError):
                count = len(items)
                total = 0
            skip += count
            if total and skip >= total:
                break

        return out

    async def get_market_position(
        self,
        *,
        user_address: str,
        market_unique_key: str,
        chain_id: int | None = None,
    ) -> dict[str, Any]:
        query = """
        query MarketPosition($userAddress: String!, $marketUniqueKey: String!, $chainId: Int) {
          marketPosition(userAddress: $userAddress, marketUniqueKey: $marketUniqueKey, chainId: $chainId) {
            healthFactor
            market {
              uniqueKey
              lltv
              irmAddress
              listed
              loanAsset { address symbol name decimals priceUsd }
              collateralAsset { address symbol name decimals priceUsd }
              oracle { address }
            }
            state {
              collateral
              supplyAssets
              supplyAssetsUsd
              supplyShares
              borrowAssets
              borrowAssetsUsd
              borrowShares
            }
          }
        }
        """
        payload = await self._post(
            query=query,
            variables={
                "userAddress": str(user_address),
                "marketUniqueKey": str(market_unique_key),
                "chainId": chain_id,
            },
        )
        pos = (payload or {}).get("marketPosition") if isinstance(payload, dict) else None
        if not isinstance(pos, dict):
            raise ValueError(
                f"Position not found for user={user_address} market={market_unique_key}"
            )
        return pos


MORPHO_CLIENT = MorphoClient()
