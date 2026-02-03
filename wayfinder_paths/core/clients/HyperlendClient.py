from __future__ import annotations

from typing import Any, Required, TypedDict

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url


class AssetsView(TypedDict):
    block_number: Required[int]
    user: Required[str]
    native_balance_wei: Required[int]
    native_balance: Required[float]
    assets: Required[list[AssetInfo]]
    account_data: Required[AccountData]
    base_currency_info: Required[BaseCurrencyInfo]


class MarketEntry(TypedDict):
    symbol: Required[str]
    symbol_canonical: Required[str]
    display_symbol: Required[str]
    reserve: Required[dict[str, Any]]


class LendRateHistory(TypedDict):
    history: Required[list[RateHistoryEntry]]


class MarketHeadroom(TypedDict):
    symbol: Required[str]
    symbol_canonical: Required[str]
    display_symbol: Required[str]
    reserve: Required[dict[str, Any]]
    decimals: Required[int]
    headroom: Required[int]
    supply_cap: Required[int]


class StableMarketsHeadroomResponse(TypedDict):
    markets: Required[dict[str, MarketHeadroom]]
    notes: Required[list[str]]


class RateHistoryEntry(TypedDict):
    timestamp_ms: Required[int]
    timestamp: Required[float]
    supply_apr: Required[float]
    supply_apy: Required[float]
    borrow_apr: Required[float]
    borrow_apy: Required[float]
    token: Required[str]
    symbol: Required[str]
    display_symbol: Required[str]


class AssetInfo(TypedDict):
    underlying: Required[str]
    symbol: Required[str]
    symbol_canonical: Required[str]
    symbol_display: Required[str]
    decimals: Required[int]
    a_token: Required[str]
    variable_debt_token: Required[str]
    usage_as_collateral_enabled: Required[bool]
    borrowing_enabled: Required[bool]
    is_active: Required[bool]
    is_frozen: Required[bool]
    is_paused: Required[bool]
    is_siloed_borrowing: Required[bool]
    is_stablecoin: Required[bool]
    underlying_wallet_balance: Required[float]
    underlying_wallet_balance_wei: Required[int]
    price_usd: Required[float]
    supply: Required[float]
    variable_borrow: Required[float]
    supply_usd: Required[float]
    variable_borrow_usd: Required[float]
    supply_apr: Required[float]
    supply_apy: Required[float]
    variable_borrow_apr: Required[float]
    variable_borrow_apy: Required[float]


class AccountData(TypedDict):
    total_collateral_base: Required[int | float]
    total_debt_base: Required[int | float]
    available_borrows_base: Required[int | float]
    current_liquidation_threshold: Required[int | float]
    ltv: Required[int | float]
    health_factor_wad: Required[int]
    health_factor: Required[float]


class BaseCurrencyInfo(TypedDict):
    marketReferenceCurrencyUnit: Required[int]
    marketReferenceCurrencyPriceInUsd: Required[int]
    networkBaseTokenPriceInUsd: Required[int]
    networkBaseTokenPriceDecimals: Required[int]


class HyperlendClient(WayfinderClient):
    async def get_stable_markets(
        self,
        *,
        required_underlying_tokens: float | None = None,
        buffer_bps: int | None = None,
        min_buffer_tokens: float | None = None,
    ) -> StableMarketsHeadroomResponse:
        url = f"{get_api_base_url()}/blockchain/hyperlend/stable_markets_headroom/"
        params: dict[str, Any] = {}
        if required_underlying_tokens is not None:
            params["required_underlying_tokens"] = required_underlying_tokens
        if buffer_bps is not None:
            params["buffer_bps"] = buffer_bps
        if min_buffer_tokens is not None:
            params["min_buffer_tokens"] = min_buffer_tokens

        response = await self._authed_request("GET", url, params=params, headers={})
        response.raise_for_status()
        data = response.json()
        return data.get("data", data)

    async def get_assets_view(
        self,
        *,
        user_address: str,
    ) -> AssetsView:
        url = f"{get_api_base_url()}/blockchain/hyperlend/assets/"
        params: dict[str, Any] = {
            "user_address": user_address,
        }

        response = await self._authed_request("GET", url, params=params, headers={})
        response.raise_for_status()
        data = response.json()
        return data.get("data", data)

    async def get_market_entry(
        self,
        *,
        token: str,
    ) -> MarketEntry:
        url = f"{get_api_base_url()}/blockchain/hyperlend/market_entry/"
        params: dict[str, Any] = {
            "token": token,
        }

        response = await self._authed_request("GET", url, params=params, headers={})
        response.raise_for_status()
        data = response.json()
        return data.get("data", data)

    async def get_lend_rate_history(
        self,
        *,
        token: str,
        lookback_hours: int,
        force_refresh: bool | None = None,
    ) -> LendRateHistory:
        url = f"{get_api_base_url()}/blockchain/hyperlend/lend_rate_history/"
        params: dict[str, Any] = {
            "token": token,
            "lookback_hours": lookback_hours,
        }
        if force_refresh is not None:
            params["force_refresh"] = "true" if force_refresh else "false"

        response = await self._authed_request("GET", url, params=params, headers={})
        response.raise_for_status()
        data = response.json()
        return data.get("data", data)


HYPERLEND_CLIENT = HyperlendClient()
