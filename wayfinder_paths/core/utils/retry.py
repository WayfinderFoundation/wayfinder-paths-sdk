from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


def exponential_backoff_s(
    attempt: int, *, base_delay_s: float = 0.25, max_delay_s: float | None = None
) -> float:
    delay_s = base_delay_s * (2**attempt)
    if max_delay_s is not None:
        delay_s = min(delay_s, max_delay_s)
    return delay_s


async def retry_async[T](
    fn: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 3,
    base_delay_s: float = 0.25,
    max_delay_s: float | None = None,
    should_retry: Callable[[Exception], bool] | None = None,
    get_delay_s: Callable[[int, Exception], float] | None = None,
    on_retry: Callable[[int, Exception, float], None] | None = None,
) -> T:
    if max_retries < 1:
        raise ValueError("max_retries must be >= 1")

    for attempt in range(max_retries):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001
            if attempt >= max_retries - 1:
                raise
            if should_retry is not None and not should_retry(exc):
                raise

            delay_s = (
                get_delay_s(attempt, exc)
                if get_delay_s is not None
                else exponential_backoff_s(
                    attempt, base_delay_s=base_delay_s, max_delay_s=max_delay_s
                )
            )
            if on_retry is not None:
                on_retry(attempt, exc, delay_s)
            await asyncio.sleep(delay_s)

    raise RuntimeError("retry_async exhausted retries")
