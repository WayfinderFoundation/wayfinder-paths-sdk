"""Shared helpers for sports gateway-backed quant pipelines."""

from __future__ import annotations

import asyncio
from typing import Any

RATE_RETRIES = 4
RATE_SLEEPS_S = (20.0, 30.0, 45.0, 60.0)
SLOW_PACE_S = 13.0  # once upstream rate limits bite, stay under ~5 calls/min


class GatewayPacer:
    """Adaptive inter-call pacing for provider requests."""

    def __init__(self, base_s: float) -> None:
        self.delay = base_s

    def throttled(self) -> None:
        self.delay = max(self.delay, SLOW_PACE_S)

    async def wait(self) -> None:
        if self.delay > 0:
            await asyncio.sleep(self.delay)


async def call_provider(
    client: Any,
    pacer: GatewayPacer | None = None,
    *,
    retries: int = RATE_RETRIES,
    **kwargs: Any,
) -> Any:
    for attempt in range(retries + 1):
        try:
            return await client.provider_call(**kwargs)
        except Exception as exc:  # noqa: BLE001 - retry only rate limits, re-raise the rest
            if "rate" in str(exc).lower() and attempt < retries:
                if pacer is not None:
                    pacer.throttled()
                await asyncio.sleep(RATE_SLEEPS_S[min(attempt, len(RATE_SLEEPS_S) - 1)])
                continue
            raise


def rows_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, dict):
            data = data.get("data", [])
        return data if isinstance(data, list) else []
    return []


def next_cursor(payload: Any) -> Any:
    if isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, dict):
            meta = data.get("meta") or {}
            return meta.get("next_cursor")
    return None


async def fetch_paginated_rows(
    client: Any,
    pacer: GatewayPacer,
    *,
    max_pages: int,
    query: dict[str, Any] | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = None
    for _ in range(max_pages):
        page_query = dict(query or {})
        if cursor is not None:
            page_query["cursor"] = cursor
        payload = await call_provider(client, pacer, query=page_query, **kwargs)
        rows.extend(rows_from_payload(payload))
        cursor = next_cursor(payload)
        await pacer.wait()
        if cursor is None:
            break
    return rows
