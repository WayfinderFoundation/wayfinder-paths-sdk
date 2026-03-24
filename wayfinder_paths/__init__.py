from __future__ import annotations

from importlib import import_module
from typing import Any

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "BaseAdapter",
    "LiquidationResult",
    "Strategy",
    "StatusDict",
    "StatusTuple",
]

_LAZY_EXPORTS = {
    "BaseAdapter": ("wayfinder_paths.core", "BaseAdapter"),
    "LiquidationResult": ("wayfinder_paths.core", "LiquidationResult"),
    "Strategy": ("wayfinder_paths.core", "Strategy"),
    "StatusDict": ("wayfinder_paths.core", "StatusDict"),
    "StatusTuple": ("wayfinder_paths.core", "StatusTuple"),
}


def __getattr__(name: str) -> Any:
    if name not in _LAZY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attr_name = _LAZY_EXPORTS[name]
    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
