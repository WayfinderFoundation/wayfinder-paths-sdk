from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url


class ContractClient(WayfinderClient):
    async def get_abi(self, chain_id: int, address: str) -> list[dict[str, Any]]:
        url = f"{get_api_base_url()}/blockchain/contracts/abi/"
        params = {"chain_id": int(chain_id), "address": address}
        response = await self._authed_request("GET", url, params=params)
        return response.json()["abi"]


CONTRACT_CLIENT = ContractClient()
