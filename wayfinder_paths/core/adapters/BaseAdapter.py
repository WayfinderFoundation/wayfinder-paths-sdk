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

    async def close(self) -> None:
        pass
