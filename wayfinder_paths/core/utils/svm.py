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


def _get_solana_client(rpc: str, commitment: Commitment | None) -> AsyncClient:
    api_key = get_api_key() if _is_wayfinder_rpc(rpc) else None
    headers = {"X-API-KEY": api_key} if api_key else None
    return AsyncClient(rpc, commitment=commitment or Confirmed, extra_headers=headers)


@asynccontextmanager
async def solana_client_from_chain_id(
    chain_id: int = CHAIN_ID_SOLANA,
    commitment: Commitment | None = None,
):
    """Async context manager yielding a single ``AsyncClient`` for ``chain_id``.

    RPC resolution mirrors ``web3_from_chain_id``: explicit ``rpc_urls``
    config overrides win, otherwise the Wayfinder RPC proxy is used
    (authenticated with the configured API key). Only the first resolved
    RPC gets a client — constructing one per RPC would leak the unused
    httpx sessions. Use ``solana_clients_from_chain_id`` for fan-out reads.
    """
    client = _get_solana_client(_get_rpcs_for_chain_id(chain_id)[0], commitment)
    try:
        yield client
    finally:
        await client.close()


@asynccontextmanager
async def solana_clients_from_chain_id(
    chain_id: int = CHAIN_ID_SOLANA,
    commitment: Commitment | None = None,
):
    """Async context manager yielding one ``AsyncClient`` per resolved RPC.

    Mirrors the EVM ``web3s_from_chain_id`` fan-out: the Wayfinder RPC proxy
    exposes N indexed endpoints, so reads that must defeat divergent per-node
    views (e.g. recent priority fees) can query every node in parallel.
    """
    clients = [
        _get_solana_client(rpc, commitment) for rpc in _get_rpcs_for_chain_id(chain_id)
    ]
    try:
        yield clients
    finally:
        for client in clients:
            await client.close()
