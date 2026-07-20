"""Solana (SVM) RPC client resolution.

Mirrors the EVM client layer in ``core/utils/web3.py`` for chain id 900: RPC
resolution goes through the same config override / Wayfinder RPC proxy
fallback, exposed as an ``AsyncClient`` context manager. Token balances live
in ``svm_tokens.py``; transaction broadcast/confirmation in
``svm_transaction.py``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Commitment, Confirmed

from wayfinder_paths.core.config import get_api_key
from wayfinder_paths.core.constants.chains import CHAIN_ID_SOLANA
from wayfinder_paths.core.utils.web3 import _get_rpcs_for_chain_id, _is_wayfinder_rpc


def _client_for_rpc(rpc: str, commitment: Commitment | None) -> AsyncClient:
    extra_headers: dict[str, str] | None = None
    if _is_wayfinder_rpc(rpc):
        api_key = get_api_key()
        if api_key:
            extra_headers = {"X-API-KEY": api_key}
    return AsyncClient(
        rpc,
        commitment=commitment or Confirmed,
        extra_headers=extra_headers,
    )


@asynccontextmanager
async def solana_client_from_chain_id(
    chain_id: int = CHAIN_ID_SOLANA,
    commitment: Commitment | None = None,
):
    """Async context manager yielding an ``AsyncClient`` for ``chain_id``.

    RPC resolution mirrors ``web3_from_chain_id``: explicit ``rpc_urls``
    config overrides win, otherwise the Wayfinder RPC proxy is used
    (authenticated with the configured API key). Only the first resolved
    RPC gets a client — constructing one per RPC would leak the unused
    httpx sessions.
    """
    rpcs = _get_rpcs_for_chain_id(chain_id)
    client = _client_for_rpc(rpcs[0], commitment)
    try:
        yield client
    finally:
        await client.close()
