from __future__ import annotations

from typing import Any, Required, TypedDict

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url


class PoolPredictions(TypedDict, total=False):
    predictedClass: str | None
    predictedProbability: int | None
    binnedConfidence: int | None


class PoolData(TypedDict, total=False):
    pool: str
    timestamp: str
    project: str
    chain: str
    symbol: str
    poolMeta: str | None
    underlyingTokens: list[str]
    rewardTokens: list[str] | None
    tvlUsd: float
    apy: float
    apyBase: float
    apyReward: float | None
    il7d: float | None
    apyBase7d: float | None
    volumeUsd1d: float | None
    volumeUsd7d: float | None
    apyBaseInception: float | None
    url: str
    apyPct1D: float
    apyPct7D: float
    apyPct30D: float
    apyMean30d: float
    stablecoin: bool
    ilRisk: str
    exposure: str
    count: int
    apyMeanExpanding: float
    apyStdExpanding: float
    mu: float
    sigma: float
    outlier: bool
    project_factorized: int
    chain_factorized: int
    predictions: PoolPredictions
    pool_old: str
    pool_old_addr: str
    pool_old_chain: str
    apy_pct: float
    kind: str
    source: str
    underlying_apy_pct: float | None
    combined_apy_pct: float
    id: str
    address: str
    network: str
    chain_code: str


class PoolList(TypedDict):
    pools: Required[list[PoolData]]


class LlamaMatchesResponse(TypedDict):
    matches: Required[list[PoolData]]


def _normalize_pool(raw: dict[str, Any]) -> dict[str, Any]:
    out = dict(raw)
    pool_id_val = out.get("pool")
    if pool_id_val is not None:
        if "id" not in out:
            out["id"] = pool_id_val
        if "token_id" not in out:
            out["token_id"] = pool_id_val
        if "pool_id" not in out:
            out["pool_id"] = pool_id_val
    if "address" not in out and "pool_old_addr" in out:
        out["address"] = out["pool_old_addr"]
    chain_val = out.get("pool_old_chain") or out.get("chain")
    if chain_val is not None:
        chain_lower = (
            chain_val.lower() if isinstance(chain_val, str) else str(chain_val)
        )
        if "network" not in out:
            out["network"] = chain_lower
        if "chain_code" not in out:
            out["chain_code"] = chain_lower
    return out


class PoolClient(WayfinderClient):
    def _pools_url(self) -> str:
        return f"{get_api_base_url()}/blockchain/pools/"

    async def get_pools(
        self,
        *,
        chain_id: int | None = None,
        project: str | None = None,
    ) -> LlamaMatchesResponse:
        url = self._pools_url()
        params: dict[str, Any] = {}
        if chain_id is not None:
            params["chain_id"] = chain_id
        if project is not None:
            params["project"] = project
        response = await self._authed_request("GET", url, params=params, headers={})
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            data = [_normalize_pool(p) for p in data]
            return {"matches": data}
        inner = data.get("data", data)
        if isinstance(inner, list):
            return {"matches": [_normalize_pool(p) for p in inner]}
        return {"matches": inner.get("matches", [])}

    async def get_pools_by_ids(
        self,
        *,
        pool_ids: list[str] | str,
    ) -> PoolList:
        url = self._pools_url()
        ids = (
            pool_ids
            if isinstance(pool_ids, list)
            else [s.strip() for s in str(pool_ids).split(",") if s.strip()]
        )
        body: dict[str, Any] = {"pool_ids": ids}
        response = await self._authed_request("POST", url, json=body, headers={})
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            data = [_normalize_pool(p) for p in data]
            return {"pools": data}
        inner = data.get("data", data)
        if isinstance(inner, list):
            return {"pools": [_normalize_pool(p) for p in inner]}
        return {"pools": inner.get("pools", [])}


POOL_CLIENT = PoolClient()
