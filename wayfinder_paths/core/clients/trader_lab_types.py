from __future__ import annotations

# Archetype buckets (server: src/apps/trader_lab/pipeline/archetypes.py).
BUCKETS = (
    "dip_buyer",
    "momentum_swing",
    "swing_other",
    "scalper",
    "mixed",
    "hft_mm",
    "unprofiled",
)
DEFAULT_BUCKETS = ("dip_buyer", "momentum_swing", "swing_other", "scalper", "mixed")
SORTS = ("sharpe", "net_mtm", "oos_sharpe", "copy_net_bp", "pos_weeks", "active_days")


class TraderLabAPIError(Exception):
    """A Trader Lab error envelope: `code` is stable (`rejected`,
    `bad_params`, `credits_exhausted`, `timeout`, `resource_limit`, `busy`,
    `unknown_release`, ...); `message` is safe to show."""

    def __init__(self, code: str, message: str, *, status: int, url: str | None = None):
        super().__init__(f"[{status} {code}] {message}")
        self.code = code
        self.message = message
        self.status = status
        self.url = url
