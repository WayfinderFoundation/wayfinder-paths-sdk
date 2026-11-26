from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url


class FlopClient(WayfinderClient):
    """The Wayfinder backend's FLOP routes: account record, restricted signing, activity."""

    async def get_account(self) -> dict[str, Any]:
        response = await self._authed_request(
            "GET", f"{get_api_base_url()}/flop/account/"
        )
        return response.json()

    async def connect(self, wallet_address: str) -> dict[str, Any]:
        response = await self._authed_request(
            "POST",
            f"{get_api_base_url()}/flop/account/",
            json={"wallet_address": wallet_address},
        )
        return response.json()

    async def sign_extrinsic(self, payload_hex: str) -> str:
        response = await self._authed_request(
            "POST",
            f"{get_api_base_url()}/flop/sign/",
            json={"kind": "extrinsic", "payload": payload_hex},
        )
        return response.json()["signature"]

    async def sign_faucet_claim(self, nonce: str) -> str:
        """The account's signature on the testnet faucet's agent claim for `nonce`."""
        response = await self._authed_request(
            "POST",
            f"{get_api_base_url()}/flop/sign/",
            json={"kind": "faucet_claim", "nonce": nonce},
        )
        return response.json()["signature"]

    async def get_activity(self, limit: int = 20) -> dict[str, Any]:
        response = await self._authed_request(
            "GET", f"{get_api_base_url()}/flop/activity/", params={"limit": limit}
        )
        return response.json()


FLOP_CLIENT = FlopClient()
