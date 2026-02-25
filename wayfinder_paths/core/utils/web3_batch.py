from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from web3 import AsyncWeb3

Web3CallFactory = Callable[[], Awaitable[Any]]


async def batch_web3_calls(
    web3: AsyncWeb3,
    *call_factories: Web3CallFactory,
    fallback_to_gather: bool = True,
) -> tuple[Any, ...]:
    """
    Execute multiple web3 reads using JSON-RPC batching when supported.

    Usage:
        a, b = await batch_web3_calls(
            web3,
            lambda: contract.functions.foo().call(block_identifier="latest"),
            lambda: contract.functions.bar().call(block_identifier="latest"),
        )

    Falls back to `asyncio.gather` if batching fails.
    """

    if not call_factories:
        return ()

    batch = None
    try:
        batch = web3.batch_requests()
        for factory in call_factories:
            batch.add(factory())
        results = await batch.async_execute()
        return tuple(results)
    except Exception as batch_exc:
        if batch is not None:
            try:
                batch.cancel()
            except Exception:
                pass

        if not fallback_to_gather:
            raise

        try:
            results = await asyncio.gather(*(factory() for factory in call_factories))
            return tuple(results)
        except Exception as gather_exc:
            raise gather_exc from batch_exc
