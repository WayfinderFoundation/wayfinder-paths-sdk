from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, NotRequired, Required, TypedDict

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url
from wayfinder_paths.core.constants.chains import CHAIN_ID_SOLANA
from wayfinder_paths.core.utils.svm_tokens import SOL_DECIMALS


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
    match_score: NotRequired[int]
    # Deprecated compatibility alias. Prefer match_score.
    confidence: NotRequired[int]
    is_canonical: NotRequired[bool]
    canonical_asset: NotRequired[CanonicalAsset]
    verification: NotRequired[str]
    suspicious: NotRequired[bool]
    protected_claim: NotRequired[str]


class CanonicalAsset(TypedDict):
    chain_code: Required[str]
    chain_id: Required[int]
    symbol: Required[str]
    name: Required[str]
    address: Required[str]
    decimals: Required[int]
    verification: NotRequired[str]
    aliases: NotRequired[list[str]]
    settlement_rank: NotRequired[int | None]
    canonical_asset: NotRequired[bool]


class FuzzyTokenResponse(TypedDict):
    tokens: Required[list[FuzzyTokenResult]]
    notice: NotRequired[str]


class CanonicalAssetsResponse(TypedDict):
    success: Required[bool]
    chain_code: Required[str]
    assets: Required[list[CanonicalAsset]]
    settlement_assets: Required[list[CanonicalAsset]]


class TokenClient(WayfinderClient):
    async def get_candles(
        self,
        coin: str,
        interval: str,
        *,
        chain_id: int,
        before_timestamp: int | None = None,
    ) -> list[dict[str, Any]]:
        url = f"{get_api_base_url()}/blockchain/tokens/candles/"
        params: dict[str, str | int] = {
            "coin": coin,
            "interval": interval,
            "chain_id": chain_id,
        }
        if before_timestamp is not None:
            params["before_timestamp"] = before_timestamp
        response = await self._authed_request("GET", url, params=params)
        response.raise_for_status()
        return response.json().get("rows", [])

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
        token = data.get("data", data)
        if str(query).strip().lower() == "solana":
            token = dict(token)
            token["decimals"] = SOL_DECIMALS
            token["symbol"] = "SOL"
            token["chain"] = {
                **dict(token.get("chain") or {}),
                "id": CHAIN_ID_SOLANA,
                "code": "solana",
                "name": "Solana",
            }
        return token

    async def discover_tokens(
        self, chain_code: str, dimension: str = "trending", limit: int = 25
    ) -> dict[str, Any]:
        url = f"{get_api_base_url()}/blockchain/tokens/discover/"
        params = {"chain_code": chain_code, "dimension": dimension, "limit": limit}
        response = await self._authed_request("GET", url, params=params)
        response.raise_for_status()
        return response.json()

    async def get_holder_intel(
        self, chain_id: int, token_address: str, refresh: bool = False
    ) -> dict[str, Any]:
        """Return backend-computed holder PnL, hold-time, and whale-entry analysis."""
        url = f"{get_api_base_url()}/blockchain/token-intel/holders/"
        params: dict[str, Any] = {"chain_id": chain_id, "address": token_address}
        if refresh:
            params["refresh"] = "true"
        # Cold computes fan out to many upstream calls server-side (~30-60s);
        # cached hits return instantly.
        response = await self._authed_request("GET", url, params=params, timeout=90.0)
        response.raise_for_status()
        return response.json()

    async def fuzzy_search(
        self, query: str, chain: str | None = None
    ) -> FuzzyTokenResponse:
        url = f"{get_api_base_url()}/blockchain/tokens/fuzzy/"
        params: dict[str, str] = {"query": query}
        if chain:
            params["chain"] = chain
        response = await self._authed_request("GET", url, params=params)
        response.raise_for_status()
        return self._parse_fuzzy_xml(response.text)

    async def get_canonical_assets(self, chain_code: str) -> CanonicalAssetsResponse:
        url = f"{get_api_base_url()}/blockchain/tokens/canonical-assets/"
        response = await self._authed_request(
            "GET", url, params={"chain_code": chain_code}
        )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _xml_bool(text: str | None) -> bool | None:
        normalized = (text or "").strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
        return None

    def _parse_fuzzy_xml(self, xml_content: str) -> FuzzyTokenResponse:
        root = ET.fromstring(xml_content)
        tokens: list[FuzzyTokenResult] = []
        for token_elem in root.findall("token"):
            token: FuzzyTokenResult = {}
            for field in ["coingecko_id", "address", "chain", "name", "symbol"]:
                elem = token_elem.find(field)
                if elem is not None and elem.text:
                    token[field] = elem.text  # type: ignore[literal-required]
            for num_field in ["price", "match_score", "confidence"]:
                elem = token_elem.find(num_field)
                if elem is not None and elem.text:
                    try:
                        if num_field == "price":
                            token["price"] = float(elem.text)
                        else:
                            token[num_field] = int(elem.text)  # type: ignore[literal-required]
                    except ValueError:
                        pass
            if "match_score" not in token and "confidence" in token:
                token["match_score"] = token["confidence"]
            if "confidence" not in token and "match_score" in token:
                token["confidence"] = token["match_score"]

            for bool_field in ["is_canonical", "suspicious"]:
                elem = token_elem.find(bool_field)
                value = self._xml_bool(elem.text if elem is not None else None)
                if value is not None:
                    token[bool_field] = value  # type: ignore[literal-required]
            for field in ["verification", "protected_claim"]:
                elem = token_elem.find(field)
                if elem is not None and elem.text:
                    token[field] = elem.text  # type: ignore[literal-required]

            canonical_elem = token_elem.find("canonical_asset")
            if canonical_elem is not None:
                canonical: CanonicalAsset = {
                    "chain_code": canonical_elem.findtext("chain_code", ""),
                    "chain_id": int(canonical_elem.findtext("chain_id", "0")),
                    "symbol": canonical_elem.findtext("symbol", ""),
                    "name": canonical_elem.findtext("name", ""),
                    "address": canonical_elem.findtext("address", ""),
                    "decimals": int(canonical_elem.findtext("decimals", "0")),
                    "canonical_asset": True,
                }
                token["canonical_asset"] = canonical
            tokens.append(token)

        result: FuzzyTokenResponse = {"tokens": tokens}
        notice = root.findtext("notice")
        if notice and notice.strip():
            result["notice"] = notice.strip()
        return result


TOKEN_CLIENT = TokenClient()
