import importlib
import pkgutil
from typing import Any

from wayfinder_paths.jobs.strategies.imx_atr_target import (
    ImxAtrTargetStrategy,
)
from wayfinder_paths.jobs.strategies.imx_atr_target import (
    build_strategy as build_imx_atr_target,
)
from wayfinder_paths.jobs.strategies.imx_momentum import (
    ImxMomentumStrategy,
)
from wayfinder_paths.jobs.strategies.imx_momentum import (
    build_strategy as build_imx_momentum,
)
from wayfinder_paths.jobs.strategies.snx_momentum import (
    SnxMomentumStrategy,
)
from wayfinder_paths.jobs.strategies.snx_momentum import (
    build_strategy as build_snx_momentum,
)

__all__ = [
    "ImxAtrTargetStrategy",
    "ImxMomentumStrategy",
    "SnxMomentumStrategy",
    "build_imx_atr_target",
    "build_imx_momentum",
    "build_snx_momentum",
    "library_catalog",
]


def library_catalog() -> list[dict[str, Any]]:
    """Every shipped reference strategy: verbatim ports of audited live
    scripts, discoverable so 'there is an X strategy that works' resolves to
    an import instead of a prose transcription. To run one as a job strategy,
    the workspace script is a one-line re-export:

        # workspace/src/strategy.py
        from wayfinder_paths.jobs.strategies.<module> import build_strategy
    """
    catalog: list[dict[str, Any]] = []
    package = __name__
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{package}.{info.name}")
        build = getattr(module, "build_strategy", None)
        if not callable(build):
            continue
        doc = (module.__doc__ or "").strip().split("\n\n")[0].replace("\n", " ")
        strategy = build(None)
        catalog.append(
            {
                "name": info.name,
                "module": f"{package}.{info.name}",
                "workspace_reexport": (
                    f"from {package}.{info.name} import build_strategy"
                ),
                "description": doc,
                "default_params": dict(getattr(strategy, "params", {}) or {}),
            }
        )
    return catalog
