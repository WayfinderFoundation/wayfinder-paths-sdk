from __future__ import annotations

from abc import ABC
from typing import Any

from eth_utils import to_checksum_address
from loguru import logger


class BaseAdapter(ABC):
    adapter_type: str | None = None

    def __init__(self, name: str, config: dict[str, Any] | None = None):
        self.name = name
        self.config = config or {}
        self.logger = logger.bind(adapter=self.__class__.__name__)

    @staticmethod
    def _resolve_strategy_wallet_address(config: dict[str, Any]) -> str | None:
        addr = (config.get("strategy_wallet") or {}).get("address")
        return to_checksum_address(addr) if addr else None

    async def close(self) -> None:
        pass
