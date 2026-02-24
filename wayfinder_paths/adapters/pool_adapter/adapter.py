from typing import Any

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.adapters.decorators import status_tuple
from wayfinder_paths.core.clients.PoolClient import (
    POOL_CLIENT,
    LlamaMatchesResponse,
    PoolList,
)


class PoolAdapter(BaseAdapter):
    adapter_type: str = "POOL"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
    ):
        super().__init__("pool_adapter", config)

    @status_tuple
    async def get_pools_by_ids(self, pool_ids: list[str]) -> PoolList:
        return await POOL_CLIENT.get_pools_by_ids(pool_ids=pool_ids)

    @status_tuple
    async def get_pools(
        self,
        *,
        chain_id: int | None = None,
        project: str | None = None,
    ) -> LlamaMatchesResponse:
        return await POOL_CLIENT.get_pools(chain_id=chain_id, project=project)
