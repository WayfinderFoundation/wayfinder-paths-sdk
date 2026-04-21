from __future__ import annotations

from typing import Any

from loguru import logger

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url


class WalletClient(WayfinderClient):
    async def list_wallets(
        self, instance_id: str | None = None
    ) -> list[dict[str, Any]]:
        url = f"{get_api_base_url()}/wallets/"
        if instance_id:
            url += f"?instance_id={instance_id}"
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
        url = f"{get_api_base_url()}/wallets/{wallet_address}/bind-instance/"
        resp = await self._authed_request(
            "POST", url, json={"instance_id": instance_id}
        )
        return resp.json()

    async def sign_transaction(self, wallet_address: str, transaction: dict) -> str:
        url = f"{get_api_base_url()}/wallets/{wallet_address}/sign-evm-transaction/"
        try:
            resp = await self._authed_request(
                "POST", url, json={"transaction": transaction}
            )
            return resp.json()["signed_transaction"]
        except Exception as exc:
            logger.error(f"sign_transaction failed for {wallet_address}: {exc}")
            raise

    async def sign_typed_data(self, wallet_address: str, typed_data: dict) -> str:
        url = f"{get_api_base_url()}/wallets/{wallet_address}/sign-typed-data/"
        try:
            resp = await self._authed_request(
                "POST", url, json={"typed_data": typed_data}
            )
            return resp.json()["signature"]
        except Exception as exc:
            logger.error(f"sign_typed_data failed for {wallet_address}: {exc}")
            raise

    async def sign_hash(self, wallet_address: str, hash_hex: str) -> str:
        url = f"{get_api_base_url()}/wallets/{wallet_address}/sign-hash/"
        try:
            resp = await self._authed_request("POST", url, json={"hash": hash_hex})
            return resp.json()["signature"]
        except Exception as exc:
            logger.error(f"sign_hash failed for {wallet_address}: {exc}")
            raise

    async def personal_sign(self, wallet_address: str, message: str) -> str:
        url = f"{get_api_base_url()}/wallets/{wallet_address}/personal-sign/"
        try:
            resp = await self._authed_request("POST", url, json={"message": message})
            return resp.json()["signature"]
        except Exception as exc:
            logger.error(f"personal_sign failed for {wallet_address}: {exc}")
            raise


WALLET_CLIENT = WalletClient()
