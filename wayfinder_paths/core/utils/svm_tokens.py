"""Solana (SVM) token balances and mint metadata.

Mirrors the EVM token layer in ``core/utils/tokens.py`` for chain id 900:
native SOL + SPL balances and mint decimals, built on the ``AsyncClient``
context manager in ``svm.py``. Token-2022 aware.
"""

from __future__ import annotations

from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
from spl.token._layouts import MINT_LAYOUT
from spl.token.constants import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
)

from wayfinder_paths.core.constants.chains import CHAIN_ID_SOLANA
from wayfinder_paths.core.utils.svm import solana_client_from_chain_id
from wayfinder_paths.core.utils.tokens import is_native_token

SOL_NATIVE_SENTINEL = "11111111111111111111111111111111"
WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"
SOL_DECIMALS = 9


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
