from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import NotRequired, Required, TypedDict

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url


class TokenLinks(TypedDict):
    github: NotRequired[list[str]]
    reddit: NotRequired[str]
    discord: NotRequired[str]
    twitter: NotRequired[str]
    homepage: NotRequired[list[str]]
    telegram: NotRequired[str]


class ChainAddress(TypedDict):
    address: Required[str]
    token_id: Required[str]
    is_contract: NotRequired[bool]
    chain_id: NotRequired[int]


class ChainInfo(TypedDict):
    id: Required[int]
    name: Required[str]
    code: Required[str]


class TokenMetadata(TypedDict):
    query_processed: NotRequired[str]
    query_type: NotRequired[str]
    has_addresses: NotRequired[bool]
    address_count: NotRequired[int]
    has_price_data: NotRequired[bool]


class TokenDetails(TypedDict):
    asset_id: NotRequired[str]
    token_ids: NotRequired[list[str]]
    name: Required[str]
    symbol: Required[str]
    decimals: Required[int]
    description: NotRequired[str]
    links: NotRequired[TokenLinks]
    categories: NotRequired[list[str]]
    current_price: NotRequired[float]
    market_cap: NotRequired[float]
    total_volume_usd_24h: NotRequired[float]
    price_change_24h: NotRequired[float]
    price_change_7d: NotRequired[float]
    price_change_30d: NotRequired[float]
    price_change_1y: NotRequired[float]
    addresses: NotRequired[dict[str, str]]
    chain_addresses: NotRequired[dict[str, ChainAddress]]
    chain_ids: NotRequired[dict[str, int]]
    id: NotRequired[int]
    token_id: Required[str]
    address: Required[str]
    chain: NotRequired[ChainInfo]
    query: NotRequired[str]
    query_type: NotRequired[str]
    metadata: NotRequired[TokenMetadata]
    image_url: NotRequired[str | None]


class GasToken(TypedDict):
    id: Required[str]
    coingecko_id: NotRequired[str]
    token_id: Required[str]
    name: Required[str]
    symbol: Required[str]
    address: Required[str]
    decimals: Required[int]
    chain: NotRequired[ChainInfo]


class FuzzyTokenResult(TypedDict):
    coingecko_id: NotRequired[str]
    address: NotRequired[str]
    chain: NotRequired[str]
    name: NotRequired[str]
    symbol: NotRequired[str]
    price: NotRequired[float]
    confidence: NotRequired[int]


class TokenClient(WayfinderClient):
    async def get_token_details(
        self, query: str, market_data: bool = False, chain_id: int | None = None
    ) -> TokenDetails:
        url = f"{get_api_base_url()}/blockchain/tokens/detail/"
        params = {
            "query": query,
            "market_data": market_data,
        }
        if chain_id is not None:
            params["chain_id"] = chain_id
        response = await self._authed_request("GET", url, params=params)
        response.raise_for_status()
        data = response.json()
        return data.get("data", data)

    async def get_gas_token(self, query: str) -> GasToken:
        url = f"{get_api_base_url()}/blockchain/tokens/gas/"
        params = {"query": query}
        response = await self._authed_request("GET", url, params=params)
        response.raise_for_status()
        data = response.json()
        return data.get("data", data)

    async def fuzzy_search(
        self, query: str, chain: str | None = None
    ) -> dict[str, list[FuzzyTokenResult] | str | None]:
        url = f"{get_api_base_url()}/blockchain/tokens/fuzzy/"
        params: dict[str, str] = {"query": query}
        if chain:
            params["chain"] = chain
        response = await self._authed_request("GET", url, params=params)
        response.raise_for_status()
        trace_id = response.headers.get("X-Token-Fuzzy-Trace")
        tokens = self._parse_fuzzy_xml(response.text)
        return {"tokens": tokens, "trace_id": trace_id}

    def _parse_fuzzy_xml(self, xml_content: str) -> list[FuzzyTokenResult]:
        root = ET.fromstring(xml_content)
        tokens: list[FuzzyTokenResult] = []
        for token_elem in root.findall("token"):
            token: FuzzyTokenResult = {}
            for field in ["coingecko_id", "address", "chain", "name", "symbol"]:
                elem = token_elem.find(field)
                if elem is not None and elem.text:
                    token[field] = elem.text  # type: ignore[literal-required]
            for num_field in ["price", "confidence"]:
                elem = token_elem.find(num_field)
                if elem is not None and elem.text:
                    try:
                        if num_field == "price":
                            token["price"] = float(elem.text)
                        else:
                            token["confidence"] = int(elem.text)
                    except ValueError:
                        pass
            tokens.append(token)
        return tokens


TOKEN_CLIENT = TokenClient()
