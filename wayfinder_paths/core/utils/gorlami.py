from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any

from wayfinder_paths.core.clients.GorlamiTestnetClient import GorlamiTestnetClient
from wayfinder_paths.core.config import get_rpc_urls, set_rpc_urls


@asynccontextmanager
async def gorlami_fork(
    chain_id: int,
    *,
    native_balances: dict[str, int] | None = None,
    erc20_balances: list[tuple[str, str, int]] | None = None,
) -> AsyncIterator[tuple[GorlamiTestnetClient, dict[str, Any]]]:
    """Create a Gorlami fork and temporarily route `web3_from_chain_id` to it.

    This works by overriding `strategy.rpc_urls[chain_id]` in the global config for
    the duration of the context manager.
    """

    old_rpc_urls = deepcopy(get_rpc_urls())
    client = GorlamiTestnetClient()
    fork_info = await client.create_fork(chain_id=chain_id)
    fork_id = str(fork_info["fork_id"])

    new_rpc_urls = deepcopy(old_rpc_urls)
    new_rpc_urls[str(chain_id)] = fork_info["rpc_url"]
    set_rpc_urls(new_rpc_urls)

    try:
        if native_balances:
            for address, balance in native_balances.items():
                await client.set_native_balance(
                    fork_id=fork_id, wallet=address, amount=balance
                )

        if erc20_balances:
            for token, address, amount in erc20_balances:
                await client.set_erc20_balance(
                    fork_id=fork_id,
                    token=token,
                    wallet=address,
                    amount=amount,
                )

        yield client, fork_info
    finally:
        try:
            set_rpc_urls(old_rpc_urls)
        except Exception:
            # best-effort restore
            pass
        try:
            await client.delete_fork(fork_id)
        except Exception:
            pass
        await client.close()


def gorlami_dry_run(
    chain_id: int,
    *,
    native_balances: dict[str, int] | None = None,
    erc20_balances: list[tuple[str, str, int]] | None = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Decorator wrapper for running an async function inside a Gorlami fork."""

    def _decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        async def _wrapped(*args: Any, **kwargs: Any) -> Any:
            async with gorlami_fork(
                chain_id,
                native_balances=native_balances,
                erc20_balances=erc20_balances,
            ):
                return await fn(*args, **kwargs)

        return _wrapped

    return _decorator
