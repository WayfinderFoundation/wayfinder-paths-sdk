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

    def _init_strategy_wallet(self, config: dict[str, Any] | None) -> None:
        cfg = config or {}
        strategy_addr = (cfg.get("strategy_wallet") or {}).get("address")
        self.strategy_wallet_address: str | None = (
            to_checksum_address(strategy_addr) if strategy_addr else None
        )

    async def close(self) -> None:
        pass
