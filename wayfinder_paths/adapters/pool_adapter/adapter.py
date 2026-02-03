from typing import Any

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
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

    async def get_pools_by_ids(
        self, pool_ids: list[str]
    ) -> tuple[bool, PoolList | str]:
        try:
            data = await POOL_CLIENT.get_pools_by_ids(pool_ids=pool_ids)
            return (True, data)
        except Exception as e:
            self.logger.error(f"Error fetching pools by IDs: {e}")
            return (False, str(e))

    async def get_pools(
        self,
        *,
        chain_id: int | None = None,
        project: str | None = None,
    ) -> tuple[bool, LlamaMatchesResponse | str]:
        try:
            data = await POOL_CLIENT.get_pools(chain_id=chain_id, project=project)
            return (True, data)
        except Exception as e:
            self.logger.error(f"Error fetching pools: {e}")
            return (False, str(e))
