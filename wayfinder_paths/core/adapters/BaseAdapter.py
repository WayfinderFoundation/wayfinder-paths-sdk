from __future__ import annotations

from abc import ABC
from typing import Any

from loguru import logger


class BaseAdapter(ABC):
    adapter_type: str | None = None

    def __init__(self, name: str, config: dict[str, Any] | None = None):
        self.name = name
        self.config = config or {}
        self.logger = logger.bind(adapter=self.__class__.__name__)

    def _require_strategy_wallet(self) -> tuple[bool, str]:
        """Validate strategy wallet is configured.

        Returns ``(True, address)`` when available, or ``(False, error_msg)``
        so callers can ``return`` the tuple directly as a status result.
        """
        addr: str | None = getattr(self, "strategy_wallet_address", None)
        if not addr:
            return False, "strategy wallet address not configured"
        return True, addr

    async def close(self) -> None:
        pass
