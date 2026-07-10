"""Solana (SVM) chain utilities.

Mirrors the EVM helpers in ``core/utils/web3.py`` for chain id 900: RPC
resolution goes through the same config override / Wayfinder RPC proxy
fallback, and native/SPL balances are exposed as small async helpers built
on solana-py's ``AsyncClient``. Transaction broadcast/confirmation live in
``svm_transaction.py``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Commitment, Confirmed
from solders.pubkey import Pubkey
from spl.token._layouts import MINT_LAYOUT
from spl.token.constants import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
)

from wayfinder_paths.core.config import get_api_key
from wayfinder_paths.core.constants.chains import CHAIN_ID_SOLANA, SVM_CHAIN_IDS
from wayfinder_paths.core.utils.tokens import is_native_token
from wayfinder_paths.core.utils.web3 import _get_rpcs_for_chain_id, _is_wayfinder_rpc

SOL_NATIVE_SENTINEL = "11111111111111111111111111111111"
WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"
SOL_DECIMALS = 9


def is_solana_chain(chain_id: int | str | None) -> bool:
    try:
        return int(chain_id) in SVM_CHAIN_IDS  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _require_solana_chain(chain_id: int) -> int:
    if not is_solana_chain(chain_id):
        raise ValueError(
            f"chain_id {chain_id} is not a Solana chain (expected one of {sorted(SVM_CHAIN_IDS)})"
        )
    return int(chain_id)


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
    _require_solana_chain(chain_id)
    rpcs = _get_rpcs_for_chain_id(chain_id)
    client = _client_for_rpc(rpcs[0], commitment)
    try:
        yield client
    finally:
        await client.close()


def get_associated_token_address(
    owner: Pubkey, mint: Pubkey, program_id: Pubkey | None = None
) -> Pubkey:
    """Derive the associated token account address for ``owner``/``mint``.

    ``program_id`` selects the owning token program (classic SPL Token by
    default, pass ``TOKEN_2022_PROGRAM_ID`` for Token-2022 mints).
    """
    if program_id is None:
        program_id = TOKEN_PROGRAM_ID
    key, _ = Pubkey.find_program_address(
        seeds=[bytes(owner), bytes(program_id), bytes(mint)],
        program_id=ASSOCIATED_TOKEN_PROGRAM_ID,
    )
    return key


async def _get_mint_account(client: AsyncClient, mint: Pubkey):
    info = (await client.get_account_info(mint)).value
    if info is None:
        raise ValueError(f"Token mint account not found: {mint}")
    return info


async def _resolve_token_program_id(client: AsyncClient, mint: Pubkey) -> Pubkey:
    info = await _get_mint_account(client, mint)
    return (
        TOKEN_2022_PROGRAM_ID
        if info.owner == TOKEN_2022_PROGRAM_ID
        else TOKEN_PROGRAM_ID
    )


async def get_sol_balance(wallet_address: str, chain_id: int = CHAIN_ID_SOLANA) -> int:
    """Native SOL balance in lamports."""
    async with solana_client_from_chain_id(chain_id) as client:
        resp = await client.get_balance(Pubkey.from_string(wallet_address))
        return int(resp.value)


async def get_spl_token_balance(
    wallet_address: str, mint: str, chain_id: int = CHAIN_ID_SOLANA
) -> int:
    """SPL token balance (raw base units) held in the owner's ATA.

    Token-2022 aware: the token program is resolved from the mint account's
    owner. Returns 0 when the associated token account does not exist.
    """
    async with solana_client_from_chain_id(chain_id) as client:
        owner = Pubkey.from_string(wallet_address)
        mint_pubkey = Pubkey.from_string(mint)
        program_id = await _resolve_token_program_id(client, mint_pubkey)
        ata = get_associated_token_address(owner, mint_pubkey, program_id)
        ata_info = (await client.get_account_info(ata)).value
        if ata_info is None:
            return 0
        resp = await client.get_token_account_balance(ata)
        return int(resp.value.amount)


async def get_solana_token_balance(
    wallet_address: str, token_address: str, chain_id: int = CHAIN_ID_SOLANA
) -> int:
    if is_native_token(token_address):
        return await get_sol_balance(wallet_address, chain_id)
    return await get_spl_token_balance(wallet_address, token_address, chain_id)


async def get_spl_mint_decimals(mint: str, chain_id: int = CHAIN_ID_SOLANA) -> int:
    """Decimals for an SPL mint (native SOL sentinel returns 9)."""
    if is_native_token(mint):
        return SOL_DECIMALS
    async with solana_client_from_chain_id(chain_id) as client:
        info = await _get_mint_account(client, Pubkey.from_string(mint))
        data = bytes(info.data)
        if len(data) < MINT_LAYOUT.sizeof():
            raise ValueError(f"Account {mint} does not look like an SPL mint")
        return int(MINT_LAYOUT.parse(data).decimals)
