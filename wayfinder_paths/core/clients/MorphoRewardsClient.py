from __future__ import annotations

from typing import Any

import httpx

from wayfinder_paths.core.constants.base import DEFAULT_HTTP_TIMEOUT

MORPHO_REWARDS_API_BASE_URL = "https://rewards.morpho.org"


class MorphoRewardsClient:
    def __init__(self, *, base_url: str = MORPHO_REWARDS_API_BASE_URL) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(DEFAULT_HTTP_TIMEOUT))

    async def _get_json(self, *, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        resp = await self.client.get(url, params=params or {})
        resp.raise_for_status()
        return resp.json()

    async def get_user_rewards(
        self,
        *,
        user: str,
        chain_id: int | None = None,
        trusted: bool = True,
        exclude_merkl_programs: bool | None = None,
        no_cache: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"trusted": "true" if trusted else "false"}
        if chain_id is not None:
            params["chain_id"] = int(chain_id)
        if exclude_merkl_programs is not None:
            params["exclude_merkl_programs"] = (
                "true" if exclude_merkl_programs else "false"
            )
        if no_cache is not None:
            params["noCache"] = int(no_cache)

        data = await self._get_json(path=f"/v1/users/{user}/rewards", params=params)
        if not isinstance(data, dict):
            raise ValueError("Morpho Rewards API returned unexpected response type")
        return data

    async def get_user_distributions(
        self,
        *,
        user: str,
        chain_id: int | None = None,
        trusted: bool = True,
        no_cache: int | None = None,
        max_pages: int = 20,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"trusted": "true" if trusted else "false"}
        if chain_id is not None:
            params["chain_id"] = int(chain_id)
        if no_cache is not None:
            params["noCache"] = int(no_cache)

        out: list[dict[str, Any]] = []
        url_path = f"/v1/users/{user}/distributions"

        for _ in range(max_pages):
            data = await self._get_json(path=url_path, params=params)
            if not isinstance(data, dict):
                break
            items = data.get("data") or []
            if isinstance(items, list):
                out.extend([i for i in items if isinstance(i, dict)])
            pagination = data.get("pagination") or {}
            next_url = pagination.get("next") if isinstance(pagination, dict) else None
            if not next_url:
                break

            # The API returns a full URL; convert to a path so we keep base_url stable.
            if isinstance(next_url, str) and next_url.startswith(self.base_url):
                url_path = next_url[len(self.base_url) :]
                params = {}
            elif isinstance(next_url, str) and next_url.startswith("/"):
                url_path = next_url
                params = {}
            else:
                break

        return out


MORPHO_REWARDS_CLIENT = MorphoRewardsClient()

