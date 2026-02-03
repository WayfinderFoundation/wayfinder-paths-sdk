from __future__ import annotations

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url


class BalanceClient(WayfinderClient):
    async def get_enriched_wallet_balances(
        self,
        *,
        wallet_address: str,
        exclude_spam_tokens: bool = True,
    ) -> dict:
        url = f"{get_api_base_url()}/blockchain/balances/enriched/"
        params = {
            "address": wallet_address,
            "exclude_spam_tokens": str(exclude_spam_tokens).lower(),
        }
        response = await self._authed_request("GET", url, params=params)
        return response.json()

    async def get_wallet_activity(
        self,
        *,
        wallet_address: str,
        limit: int = 20,
        offset: str | None = None,
    ) -> dict:
        url = f"{get_api_base_url()}/blockchain/balances/activity/"
        params: dict[str, str | int] = {"address": wallet_address, "limit": limit}
        if offset:
            params["offset"] = offset
        response = await self._authed_request("GET", url, params=params)
        return response.json()

    async def get_token_balance(
        self,
        *,
        wallet_address: str,
        token_id: str,
        human_readable: bool = True,
    ) -> dict:
        url = f"{get_api_base_url()}/public/balances/token/"
        params = {
            "wallet_address": wallet_address,
            "token_id": token_id,
            "human_readable": str(human_readable).lower(),
        }
        response = await self._authed_request("GET", url, params=params)
        return response.json()

    async def get_pool_balance(
        self,
        *,
        pool_address: str,
        chain_id: int,
        user_address: str,
        human_readable: bool = True,
    ) -> dict:
        url = f"{get_api_base_url()}/public/balances/pool/"
        params = {
            "pool_address": pool_address,
            "chain_id": chain_id,
            "user_address": user_address,
            "human_readable": str(human_readable).lower(),
        }
        response = await self._authed_request("GET", url, params=params)
        return response.json()


BALANCE_CLIENT = BalanceClient()
