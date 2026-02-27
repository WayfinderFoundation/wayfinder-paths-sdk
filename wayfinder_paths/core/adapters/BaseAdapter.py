from __future__ import annotations

import functools
from abc import ABC
from collections.abc import Callable
from typing import Any

from loguru import logger


def require_wallet(fn: Callable) -> Callable:
    """Return ``(False, ...)`` early if ``self.wallet_address`` is not set."""

    @functools.wraps(fn)
    async def wrapper(self: BaseAdapter, *args: Any, **kwargs: Any) -> Any:
        if not getattr(self, "wallet_address", None):
            return False, "wallet address not configured"
        return await fn(self, *args, **kwargs)

    return wrapper


class BaseAdapter(ABC):
    adapter_type: str | None = None

    def __init__(self, name: str, config: dict[str, Any] | None = None):
        self.name = name
        self.config = config or {}
        self.logger = logger.bind(adapter=self.__class__.__name__)

    async def close(self) -> None:
        pass
