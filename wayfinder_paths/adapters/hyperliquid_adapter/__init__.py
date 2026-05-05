from .adapter import HyperliquidAdapter
from .paired_filler import FillConfig, FillConfirmCfg, PairedFiller

# Read-only adapter at module scope so its aiocache (60s on meta/outcomes,
# 300s on spot) survives across calls. Fresh per-call instances defeat the
# cache. Write paths still need get_adapter(...) for per-wallet sign callbacks.
HL_ADAPTER = HyperliquidAdapter()

__all__ = [
    "HL_ADAPTER",
    "HyperliquidAdapter",
    "PairedFiller",
    "FillConfig",
    "FillConfirmCfg",
]
