"""Lazy client re-exports for the common public surface.

Keeping these imports lazy prevents an unrelated client from completing a
partially initialized dependency cycle when callers import an adapter directly.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from wayfinder_paths.core.clients.DeltaLabClient import (
        DELTA_LAB_CLIENT,
        DeltaLabClient,
    )
    from wayfinder_paths.core.clients.PoolClient import POOL_CLIENT
    from wayfinder_paths.core.clients.ResearchClient import (
        RESEARCH_CLIENT,
        ResearchClient,
    )
    from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT

__all__ = [
    "DELTA_LAB_CLIENT",
    "DeltaLabClient",
    "POOL_CLIENT",
    "RESEARCH_CLIENT",
    "ResearchClient",
    "TOKEN_CLIENT",
]

_EXPORTS: dict[str, tuple[str, str]] = {
    "DELTA_LAB_CLIENT": (
        "wayfinder_paths.core.clients.DeltaLabClient",
        "DELTA_LAB_CLIENT",
    ),
    "DeltaLabClient": (
        "wayfinder_paths.core.clients.DeltaLabClient",
        "DeltaLabClient",
    ),
    "POOL_CLIENT": ("wayfinder_paths.core.clients.PoolClient", "POOL_CLIENT"),
    "RESEARCH_CLIENT": (
        "wayfinder_paths.core.clients.ResearchClient",
        "RESEARCH_CLIENT",
    ),
    "ResearchClient": (
        "wayfinder_paths.core.clients.ResearchClient",
        "ResearchClient",
    ),
    "TOKEN_CLIENT": ("wayfinder_paths.core.clients.TokenClient", "TOKEN_CLIENT"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    module = import_module(module_name)
    # Importing a child module temporarily assigns that module object to the
    # same-named package attribute. Populate every export from the module so
    # class exports such as ``DeltaLabClient`` retain their historic meaning.
    for export_name, (export_module, export_attribute) in _EXPORTS.items():
        if export_module == module_name:
            globals()[export_name] = getattr(module, export_attribute)
    return globals()[name]


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
