from __future__ import annotations

import asyncio
import json
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
        self._timeout = httpx.Timeout(DEFAULT_HTTP_TIMEOUT)
        self.client = httpx.AsyncClient(timeout=self._timeout)
        self.headers = {"Content-Type": "application/json"}
        self._client_loop: asyncio.AbstractEventLoop | None = None

    async def _reset_client(self) -> None:
        try:
            await self.client.aclose()
        except Exception:  # noqa: BLE001
            pass
        self.client = httpx.AsyncClient(timeout=self._timeout)

    async def _ensure_client(self) -> None:
        loop = asyncio.get_running_loop()
        if self._client_loop is None:
            self._client_loop = loop
            return
        if self._client_loop is not loop or getattr(self.client, "is_closed", False):
            await self._reset_client()
            self._client_loop = loop

    async def _post(
        self, *, query: str, variables: dict[str, Any] | None = None
    ) -> Any:
        max_retries = 3
        delay_s = 0.25

        for attempt in range(max_retries):
            try:
                await self._ensure_client()

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
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                retryable = status in (429, 500, 502, 503, 504)
                if retryable and attempt < (max_retries - 1):
                    await asyncio.sleep(delay_s * (2**attempt))
                    continue
                raise
            except (
                httpx.TransportError,
                httpx.TimeoutException,
                json.JSONDecodeError,
            ) as exc:
                if attempt < (max_retries - 1):
                    logger.warning(
                        "Morpho API request failed (attempt {}/{}): {}",
                        attempt + 1,
                        max_retries,
                        type(exc).__name__,
                    )
                    await self._reset_client()
                    await asyncio.sleep(delay_s * (2**attempt))
                    continue
                raise

        raise RuntimeError("Morpho API request failed")

    async def get_morpho_by_chain(self) -> dict[int, dict[str, str]]:
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

        return by_chain

    async def get_morpho_address(self, *, chain_id: int) -> str:
        by_chain = await self.get_morpho_by_chain()
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
              reallocatableLiquidityAssets
              warnings { type level }
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
                price
                rewards { supplyApr borrowApr asset { address symbol name decimals priceUsd } }
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
            reallocatableLiquidityAssets
            warnings { type level }
            publicAllocatorSharedLiquidity {
              assets
              publicAllocator { address }
              vault { address symbol }
              withdrawMarket { uniqueKey }
              supplyMarket { uniqueKey }
            }
            supplyingVaults { address symbol }
            supplyingVaultV2s { address symbol }
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
              price
              rewards { supplyApr borrowApr asset { address symbol name decimals priceUsd } }
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
        market = (
            (payload or {}).get("marketByUniqueKey")
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(market, dict):
            raise ValueError(f"Market not found for uniqueKey={unique_key}")
        return market

    async def get_market_history(
        self,
        *,
        unique_key: str,
        chain_id: int | None = None,
    ) -> dict[str, Any]:
        query = """
        query MarketHistory($k: String!, $chainId: Int) {
          marketByUniqueKey(uniqueKey: $k, chainId: $chainId) {
            uniqueKey
            historicalState {
              supplyApy { x y }
              netSupplyApy { x y }
              borrowApy { x y }
              netBorrowApy { x y }

              dailySupplyApy { x y }
              dailyNetSupplyApy { x y }
              dailyBorrowApy { x y }
              dailyNetBorrowApy { x y }

              weeklySupplyApy { x y }
              weeklyNetSupplyApy { x y }
              weeklyBorrowApy { x y }
              weeklyNetBorrowApy { x y }

              monthlySupplyApy { x y }
              monthlyNetSupplyApy { x y }
              monthlyBorrowApy { x y }
              monthlyNetBorrowApy { x y }

              quarterlySupplyApy { x y }
              quarterlyNetSupplyApy { x y }
              quarterlyBorrowApy { x y }
              quarterlyNetBorrowApy { x y }

              yearlySupplyApy { x y }
              yearlyNetSupplyApy { x y }
              yearlyBorrowApy { x y }
              yearlyNetBorrowApy { x y }

              utilization { x y }
              liquidityAssets { x y }
              borrowAssets { x y }
              supplyAssets { x y }
              price { x y }
            }
          }
        }
        """
        payload = await self._post(
            query=query, variables={"k": str(unique_key), "chainId": chain_id}
        )
        market = (
            (payload or {}).get("marketByUniqueKey")
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(market, dict):
            raise ValueError(f"Market not found for uniqueKey={unique_key}")
        return market.get("historicalState") or {}

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
              priceVariationToLiquidationPrice
              listed
              market {
                uniqueKey
                lltv
                irmAddress
                listed
                morphoBlue { chain { id network } }
                loanAsset { address symbol name decimals priceUsd }
                collateralAsset { address symbol name decimals priceUsd }
                oracle { address }
                state {
                  supplyApy
                  netSupplyApy
                  borrowApy
                  netBorrowApy
                  rewards { supplyApr borrowApr asset { address symbol name decimals priceUsd } }
                }
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
                (payload or {}).get("marketPositions")
                if isinstance(payload, dict)
                else None
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
            priceVariationToLiquidationPrice
            listed
            market {
              uniqueKey
              lltv
              irmAddress
              listed
              morphoBlue { chain { id network } }
              loanAsset { address symbol name decimals priceUsd }
              collateralAsset { address symbol name decimals priceUsd }
              oracle { address }
              state {
                supplyApy
                netSupplyApy
                borrowApy
                netBorrowApy
                rewards { supplyApr borrowApr asset { address symbol name decimals priceUsd } }
              }
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
        pos = (
            (payload or {}).get("marketPosition") if isinstance(payload, dict) else None
        )
        if not isinstance(pos, dict):
            raise ValueError(
                f"Position not found for user={user_address} market={market_unique_key}"
            )
        return pos

    async def get_all_vaults(
        self,
        *,
        chain_id: int,
        listed: bool | None = True,
        page_size: int = 50,
        max_pages: int = 50,
    ) -> list[dict[str, Any]]:
        query = """
        query Vaults($first: Int, $skip: Int, $where: VaultFilters) {
          vaults(first: $first, skip: $skip, where: $where) {
            items {
              address
              symbol
              name
              listed
              featured
              warnings { type level }
              asset { address symbol name decimals priceUsd }
              state {
                apy
                netApy
                netApyWithoutRewards
                totalAssets
                totalAssetsUsd
                rewards { supplyApr asset { address symbol name decimals priceUsd } }
                allocation {
                  supplyAssets
                  supplyAssetsUsd
                  supplyCap
                  supplyCapUsd
                  market {
                    uniqueKey
                    lltv
                    loanAsset { address symbol decimals }
                    collateralAsset { address symbol decimals }
                  }
                }
              }
            }
            pageInfo { countTotal count limit skip }
          }
        }
        """

        where: dict[str, Any] = {"chainId_in": [int(chain_id)]}
        if listed is not None:
            where["listed"] = bool(listed)

        out: list[dict[str, Any]] = []
        skip = 0
        for _ in range(max_pages):
            payload = await self._post(
                query=query,
                variables={"first": int(page_size), "skip": int(skip), "where": where},
            )
            page = (payload or {}).get("vaults") if isinstance(payload, dict) else None
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

    async def get_vault_by_address(
        self, *, address: str, chain_id: int | None = None
    ) -> dict[str, Any]:
        query = """
        query Vault($address: String!, $chainId: Int) {
          vaultByAddress(address: $address, chainId: $chainId) {
            address
            symbol
            name
            listed
            featured
            warnings { type level }
            asset { address symbol name decimals priceUsd }
            state {
              apy
              netApy
              netApyWithoutRewards
              totalAssets
              totalAssetsUsd
              rewards { supplyApr asset { address symbol name decimals priceUsd } }
              allocation {
                supplyAssets
                supplyAssetsUsd
                supplyCap
                supplyCapUsd
                market {
                  uniqueKey
                  lltv
                  loanAsset { address symbol decimals }
                  collateralAsset { address symbol decimals }
                }
              }
            }
          }
        }
        """
        payload = await self._post(
            query=query, variables={"address": str(address), "chainId": chain_id}
        )
        vault = (
            (payload or {}).get("vaultByAddress") if isinstance(payload, dict) else None
        )
        if not isinstance(vault, dict):
            raise ValueError(f"Vault not found for address={address}")
        return vault

    async def get_all_vault_v2s(
        self,
        *,
        chain_id: int,
        listed: bool | None = True,
        page_size: int = 50,
        max_pages: int = 50,
    ) -> list[dict[str, Any]]:
        query = """
        query VaultV2s($first: Int, $skip: Int, $where: VaultV2sFilters) {
          vaultV2s(first: $first, skip: $skip, where: $where) {
            items {
              address
              symbol
              name
              listed
              warnings { type level }
              asset { address symbol name decimals priceUsd }
              apy
              netApy
              avgApy
              avgNetApy
              totalAssets
              totalAssetsUsd
              liquidity
              liquidityUsd
              rewards { supplyApr asset { address symbol name decimals priceUsd } }
              liquidityAdapter { address type assets assetsUsd }
              adapters { items { address type assets assetsUsd } }
            }
            pageInfo { countTotal count limit skip }
          }
        }
        """

        where: dict[str, Any] = {"chainId_in": [int(chain_id)]}
        if listed is not None:
            where["listed"] = bool(listed)

        out: list[dict[str, Any]] = []
        skip = 0
        for _ in range(max_pages):
            payload = await self._post(
                query=query,
                variables={"first": int(page_size), "skip": int(skip), "where": where},
            )
            page = (
                (payload or {}).get("vaultV2s") if isinstance(payload, dict) else None
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

    async def get_vault_v2_by_address(
        self, *, address: str, chain_id: int | None = None
    ) -> dict[str, Any]:
        query = """
        query VaultV2($address: String!, $chainId: Int) {
          vaultV2ByAddress(address: $address, chainId: $chainId) {
            address
            symbol
            name
            listed
            warnings { type level }
            asset { address symbol name decimals priceUsd }
            apy
            netApy
            avgApy
            avgNetApy
            totalAssets
            totalAssetsUsd
            liquidity
            liquidityUsd
            rewards { supplyApr asset { address symbol name decimals priceUsd } }
            liquidityAdapter { address type assets assetsUsd }
            adapters { items { address type assets assetsUsd } }
          }
        }
        """
        payload = await self._post(
            query=query, variables={"address": str(address), "chainId": chain_id}
        )
        vault = (
            (payload or {}).get("vaultV2ByAddress")
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(vault, dict):
            raise ValueError(f"VaultV2 not found for address={address}")
        return vault


MORPHO_CLIENT = MorphoClient()
