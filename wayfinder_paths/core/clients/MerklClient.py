from __future__ import annotations

from typing import Any

import httpx

from wayfinder_paths.core.constants.base import DEFAULT_HTTP_TIMEOUT

MERKL_API_BASE_URL = "https://api.merkl.xyz"


class MerklClient:
    def __init__(self, *, base_url: str = MERKL_API_BASE_URL) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(DEFAULT_HTTP_TIMEOUT))

    async def get_user_rewards(
        self,
        *,
        address: str,
        chain_ids: list[int],
        breakdown_page: int = 0,
        claimable_only: bool = True,
        reward_type: str | None = "TOKEN",
        test: bool = False,
        reload_chain_id: int | None = None,
    ) -> list[dict[str, Any]]:
        params: list[tuple[str, str]] = []
        for cid in chain_ids:
            params.append(("chainId", str(int(cid))))
        params.append(("breakdownPage", str(int(breakdown_page))))
        params.append(("claimableOnly", "true" if claimable_only else "false"))
        if reward_type:
            params.append(("type", str(reward_type)))
        if test:
            params.append(("test", "true"))
        if reload_chain_id is not None:
            params.append(("reloadChainId", str(int(reload_chain_id))))

        url = f"{self.base_url}/v4/users/{address}/rewards"
        resp = await self.client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            raise ValueError("Merkl API returned unexpected response type")
        return [d for d in data if isinstance(d, dict)]


MERKL_CLIENT = MerklClient()

