from __future__ import annotations

from abc import ABC
from typing import Any

from loguru import logger


class BaseAdapter(ABC):
    adapter_type: str | None = None
    wallet_address: str | None = None

    def __init__(self, name: str, config: dict[str, Any] | None = None):
        self.name = name
        self.config = config or {}
        self.logger = logger.bind(adapter=self.__class__.__name__)

    def _require_wallet(self) -> str:
        """Return wallet address or raise ValueError.

        Convenience for methods that need a configured wallet.
        """
        addr = self.wallet_address
        if not addr:
            raise ValueError("wallet address not configured")
        return addr

    async def close(self) -> None:
        pass
