"""Solana (SVM) token balances, mint metadata, and transfer envelopes.

Mirrors the EVM token layer in ``core/utils/tokens.py`` for chain id 900:
native SOL + SPL balances, mint decimals, and the unsigned transfer envelope
builder, built on the ``AsyncClient`` context manager in ``svm.py``.
Token-2022 aware.
"""

from __future__ import annotations

import base64

from solana.rpc.async_api import AsyncClient
from solders.instruction import Instruction
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction
from spl.token._layouts import MINT_LAYOUT
from spl.token.constants import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
)
from spl.token.instructions import create_associated_token_account, transfer_checked
from spl.token.models import TransferCheckedParams

from wayfinder_paths.core.constants.chains import CHAIN_ID_SOLANA, SOL_DECIMALS
from wayfinder_paths.core.utils.svm import solana_client_from_chain_id
from wayfinder_paths.core.utils.tokens import is_native_token

SOL_NATIVE_SENTINEL = "11111111111111111111111111111111"
WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"


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


async def _get_mint_decimals(client: AsyncClient, mint: Pubkey) -> int:
    info = await _get_mint_account(client, mint)
    data = bytes(info.data)
    if len(data) < MINT_LAYOUT.sizeof():
        raise ValueError(f"Account {mint} does not look like an SPL mint")
    return int(MINT_LAYOUT.parse(data).decimals)


def get_associated_token_address(
    owner: Pubkey, mint: Pubkey, program_id: Pubkey
) -> Pubkey:
    """Derive the associated token account address for ``owner``/``mint``.

    ``program_id`` is the owning token program (``TOKEN_PROGRAM_ID`` or
    ``TOKEN_2022_PROGRAM_ID``), resolved from the mint account's owner.
    """
    key, _ = Pubkey.find_program_address(
        seeds=[bytes(owner), bytes(program_id), bytes(mint)],
        program_id=ASSOCIATED_TOKEN_PROGRAM_ID,
    )
    return key


def _create_ata_idempotent_instruction(
    payer: Pubkey, owner: Pubkey, mint: Pubkey, token_program_id: Pubkey
) -> Instruction:
    """CreateIdempotent variant of the associated-token-account instruction.

    The installed spl helper only emits plain Create (empty instruction data),
    which fails if the account already exists. CreateIdempotent (discriminator
    ``1``) is a no-op in that case, so a transfer built while the recipient's
    ATA was missing cannot be raced by a concurrent ATA creation.
    """
    base = create_associated_token_account(
        payer=payer, owner=owner, mint=mint, token_program_id=token_program_id
    )
    return Instruction(
        program_id=base.program_id, data=bytes([1]), accounts=base.accounts
    )


def _serialize_unsigned(message: MessageV0) -> str:
    """Serialize a compiled message as an unsigned VersionedTransaction (base64)."""
    placeholder_signatures = [
        Signature.default() for _ in range(message.header.num_required_signatures)
    ]
    tx = VersionedTransaction.populate(message, placeholder_signatures)
    return base64.b64encode(bytes(tx)).decode("ascii")


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
    wallet_address: str, token_address: str | None, chain_id: int = CHAIN_ID_SOLANA
) -> int:
    if is_native_token(token_address):
        return await get_sol_balance(wallet_address, chain_id)
    assert token_address is not None  # is_native_token(None) is True
    return await get_spl_token_balance(wallet_address, token_address, chain_id)


async def get_spl_mint_decimals(
    mint: str | None, chain_id: int = CHAIN_ID_SOLANA
) -> int:
    """Decimals for an SPL mint (native SOL sentinel returns 9)."""
    if is_native_token(mint):
        return SOL_DECIMALS
    assert mint is not None  # is_native_token(None) is True
    async with solana_client_from_chain_id(chain_id) as client:
        return await _get_mint_decimals(client, Pubkey.from_string(mint))


async def build_solana_send_transaction(
    from_address: str,
    to_address: str,
    token_address: str | None,
    amount: int,
    chain_id: int = CHAIN_ID_SOLANA,
) -> dict:
    """Build an unsigned SOL/SPL transfer and return the Solana tx envelope.

    Returns the shared Solana transaction envelope used across the stack:

        {
            "chainType": "solana",
            "chainId": 900,
            "serializedTransaction": "<base64>",
            "lastValidBlockHeight": <int>,
        }

    The transaction is serialized UNSIGNED (placeholder signatures) with the
    sender as fee payer. Native SOL uses a system transfer; SPL tokens use
    ``transfer_checked``, prepended with an idempotent ATA-create for the
    recipient when their associated token account does not exist yet (rent
    paid by the sender). Token-2022 aware via the mint account's owner.

    Args:
        from_address: Sender (and fee payer) base58 address.
        to_address: Recipient base58 address.
        token_address: SPL mint address, or a native sentinel/None for SOL.
        amount: Amount in raw base units (lamports for SOL).
        chain_id: Internal Solana chain id (900).
    """
    if amount <= 0:
        raise ValueError("Transfer amount must be positive")

    from_pubkey = Pubkey.from_string(from_address)
    to_pubkey = Pubkey.from_string(to_address)
    native_transfer = is_native_token(token_address)
    transfer_metadata: dict[str, str | bool] = {}

    async with solana_client_from_chain_id(chain_id) as client:
        instructions: list[Instruction] = []

        if native_transfer:
            instructions.append(
                transfer(
                    TransferParams(
                        from_pubkey=from_pubkey,
                        to_pubkey=to_pubkey,
                        lamports=amount,
                    )
                )
            )
        else:
            assert token_address is not None  # is_native_token(None) is True
            mint = Pubkey.from_string(token_address)
            program_id = await _resolve_token_program_id(client, mint)
            source_ata = get_associated_token_address(from_pubkey, mint, program_id)
            dest_ata = get_associated_token_address(to_pubkey, mint, program_id)

            dest_info = (await client.get_account_info(dest_ata)).value
            recipient_ata_created = dest_info is None
            if dest_info is None:
                # Recipient has no associated token account yet — the sender
                # pays rent to create it as part of the same transaction.
                instructions.append(
                    _create_ata_idempotent_instruction(
                        payer=from_pubkey,
                        owner=to_pubkey,
                        mint=mint,
                        token_program_id=program_id,
                    )
                )

            decimals = await _get_mint_decimals(client, mint)
            transfer_metadata.update(
                {
                    "tokenProgram": str(program_id),
                    "sourceTokenAccount": str(source_ata),
                    "destinationTokenAccount": str(dest_ata),
                    "recipientTokenAccountCreated": recipient_ata_created,
                }
            )
            instructions.append(
                transfer_checked(
                    TransferCheckedParams(
                        program_id=program_id,
                        source=source_ata,
                        mint=mint,
                        dest=dest_ata,
                        owner=from_pubkey,
                        amount=amount,
                        decimals=decimals,
                    )
                )
            )

        blockhash_resp = await client.get_latest_blockhash()
        recent_blockhash = blockhash_resp.value.blockhash
        last_valid_block_height = int(blockhash_resp.value.last_valid_block_height)

    message = MessageV0.try_compile(
        payer=from_pubkey,
        instructions=instructions,
        address_lookup_table_accounts=[],
        recent_blockhash=recent_blockhash,
    )

    return {
        "chainType": "solana",
        "chainId": int(chain_id),
        "serializedTransaction": _serialize_unsigned(message),
        "lastValidBlockHeight": last_valid_block_height,
        **transfer_metadata,
    }
