from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url


class WalletClient(WayfinderClient):
    async def list_wallets(self) -> list[dict[str, Any]]:
        url = f"{get_api_base_url()}/wallets/"
        resp = await self._authed_request("GET", url)
        return resp.json()

    async def create_wallet(
        self,
        policies: list[dict],
        wallet_type: str,
        *,
        chain_type: str = "ethereum",
        label: str = "",
    ) -> dict[str, Any]:
        url = f"{get_api_base_url()}/wallets/"
        body: dict[str, Any] = {
            "policies": policies,
            "wallet_type": wallet_type,
            "chain_type": chain_type,
            "label": label,
        }
        resp = await self._authed_request("POST", url, json=body)
        return resp.json()

    async def bind_to_instance(
        self, wallet_address: str, instance_id: str
    ) -> dict[str, Any]:
        url = f"{get_api_base_url()}/wallets/bind-instance/"
        resp = await self._authed_request(
            "POST", url, json={"wallet_address": wallet_address, "instance_id": instance_id}
        )
        return resp.json()

    async def sign_transaction(self, wallet_address: str, transaction: dict) -> str:
        url = f"{get_api_base_url()}/wallets/{wallet_address}/sign-evm-transaction/"
        resp = await self._authed_request(
            "POST", url, json={"transaction": transaction}
        )
        return resp.json()["signed_transaction"]

    async def sign_typed_data(self, wallet_address: str, typed_data: dict) -> str:
        url = f"{get_api_base_url()}/wallets/{wallet_address}/sign-typed-data/"
        resp = await self._authed_request("POST", url, json={"typed_data": typed_data})
        return resp.json()["signature"]

    async def sign_hash(self, wallet_address: str, hash_hex: str) -> str:
        url = f"{get_api_base_url()}/wallets/{wallet_address}/sign-hash/"
        resp = await self._authed_request("POST", url, json={"hash": hash_hex})
        return resp.json()["signature"]

    async def personal_sign(self, wallet_address: str, message: str) -> str:
        url = f"{get_api_base_url()}/wallets/{wallet_address}/personal-sign/"
        resp = await self._authed_request("POST", url, json={"message": message})
        return resp.json()["signature"]


WALLET_CLIENT = WalletClient()
