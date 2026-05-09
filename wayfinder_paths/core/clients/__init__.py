"""Client singletons — re-exports for the common public surface."""

from wayfinder_paths.core.clients.DeltaLabClient import DELTA_LAB_CLIENT, DeltaLabClient
from wayfinder_paths.core.clients.PoolClient import POOL_CLIENT
from wayfinder_paths.core.clients.ResearchClient import RESEARCH_CLIENT, ResearchClient
from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT

__all__ = [
    "DELTA_LAB_CLIENT",
    "DeltaLabClient",
    "POOL_CLIENT",
    "RESEARCH_CLIENT",
    "ResearchClient",
    "TOKEN_CLIENT",
]
