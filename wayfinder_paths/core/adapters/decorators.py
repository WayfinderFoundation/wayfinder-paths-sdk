from __future__ import annotations

from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any


def status_tuple[T](
    fn: Callable[..., Coroutine[Any, Any, T]],
) -> Callable[..., Coroutine[Any, Any, tuple[bool, T | str]]]:
    """Wrap an async adapter method to return ``(True, result)`` or ``(False, error_str)``.

    The decorated function should perform its work and return the result directly.
    Exceptions are caught, logged via ``self.logger``, and returned as ``(False, str(e))``.
    """

    @wraps(fn)
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> tuple[bool, T | str]:
        try:
            result = await fn(self, *args, **kwargs)
            return (True, result)
        except Exception as exc:
            self.logger.error(f"Error in {fn.__name__}: {exc}")
            return (False, str(exc))

    return wrapper  # type: ignore[return-value]
