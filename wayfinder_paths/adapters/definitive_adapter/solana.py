"""Definitive Flash on Solana: check and rebuild setup instructions, check the
order message, before the wallet's Solana leg signs anything.

The wallet grants an SPL delegate to Flash's `flash_orders` PDA and
Ed25519-signs a plaintext order message (`DFS|m=<mint>|t=<total>|h=<hash>`).
Nothing from the quote is signed as-is: setup instructions are parsed, checked
against this order and rebuilt here, and the message hash is recomputed from
the wallet's own token accounts and our trading group.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from solders.instruction import Instruction
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from spl.token.instructions import approve, sync_native
from spl.token.models import ApproveParams, SyncNativeParams

from wayfinder_paths.core.utils.svm_tokens import (
    _create_ata_idempotent_instruction,
    get_associated_token_address,
)

FLASH_SVM_PROGRAM = Pubkey.from_string("FLaSHxRVv6G1NCJ2SFGPvQqKUPszuDjaTkBMFCSzEqpJ")
WSOL_MINT = "So11111111111111111111111111111111111111112"
NATIVE_SOL = "11111111111111111111111111111111"
_ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
_SYSTEM_PROGRAM = "11111111111111111111111111111111"
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _refuse(reason: str) -> ValueError:
    return ValueError(f"Refusing to sign this order: {reason}")


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def b58decode(text: str) -> bytes:
    n = 0
    for char in text:
        n = n * 58 + _B58.index(char)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(text) - len(text.lstrip("1"))) + raw


def flash_delegate_pda() -> Pubkey:
    return Pubkey.find_program_address([b"flash_orders"], FLASH_SVM_PROGRAM)[0]


def flash_svm_order_hash(
    *,
    output_account: Pubkey,
    nonce: int,
    deadline: int,
    input_account: Pubkey,
    group: str,
) -> str:
    """base58(sha256("DFS_ORDER_V1" ‖ out ATA ‖ nonce u64 ‖ deadline i64 ‖ in
    ATA ‖ program ‖ group)[:16]) — mirrors the backend and frontend."""
    digest = hashlib.sha256(
        b"DFS_ORDER_V1"
        + bytes(output_account)
        + nonce.to_bytes(8, "little")
        + deadline.to_bytes(8, "little", signed=True)
        + bytes(input_account)
        + bytes(FLASH_SVM_PROGRAM)
        + bytes(Pubkey.from_string(group))
    ).digest()[:16]
    return b58encode(digest)


def check_flash_svm_message(
    message: str | None, *, spent_mint: str, max_total: int, expected_hash: str
) -> str:
    match = re.fullmatch(
        r"DFS\|m=([1-9A-HJ-NP-Za-km-z]{32,44})\|t=(\d{1,20})\|h=(\w+)", message or ""
    )
    if not match or not message:
        raise _refuse("the quote has no Solana order message")
    mint, total, digest = match.groups()
    if mint != spent_mint:
        raise _refuse("it spends a different token")
    if not 0 < int(total) <= max_total:
        raise _refuse("it spends more than requested")
    if digest != expected_hash:
        raise _refuse("it pays a different account")
    return message


def _accounts_match(ix: dict[str, Any], expected: list[tuple[str, bool, bool]]) -> bool:
    accounts = ix.get("accounts") or []
    return len(accounts) == len(expected) and all(
        (a.get("pubkey"), a.get("isSigner"), a.get("isWritable")) == e
        for a, e in zip(accounts, expected, strict=True)
    )


def _check_create_ata(
    ix: dict[str, Any], funder: str, programs: dict[str, Pubkey]
) -> Instruction:
    accounts = ix.get("accounts") or []
    mint = accounts[3].get("pubkey") if len(accounts) > 3 else None
    program = programs.get(mint or "")
    if ix.get("programId") != _ATA_PROGRAM or program is None:
        raise _refuse("unexpected account setup")
    owner = Pubkey.from_string(funder)
    ata = get_associated_token_address(owner, Pubkey.from_string(mint), program)
    expected = [
        (funder, True, True),
        (str(ata), False, True),
        (funder, False, False),
        (mint, False, False),
        (_SYSTEM_PROGRAM, False, False),
        (str(program), False, False),
    ]
    if b58decode(ix.get("data", "")) != b"\x01" or not _accounts_match(ix, expected):
        raise _refuse("unexpected account setup")
    return _create_ata_idempotent_instruction(
        payer=owner,
        owner=owner,
        mint=Pubkey.from_string(mint),
        token_program_id=program,
    )


def check_flash_svm_setup(
    svm: dict[str, Any],
    wrap: list[dict[str, Any]] | None,
    *,
    funder: str,
    programs: dict[str, Pubkey],
    spent_mint: str,
    max_delegate: int,
    max_wrap_lamports: int,
) -> list[Instruction]:
    """Parse, check and rebuild Flash's setup: create token accounts, wrap
    native SOL, then delegate to Flash — in that order."""
    owner = Pubkey.from_string(funder)
    out = [
        _check_create_ata(ix, funder, programs) for ix in svm.get("ataSetupIxs") or []
    ]

    if wrap:
        if spent_mint != WSOL_MINT:
            raise _refuse("unexpected SOL wrap")
        wsol_ata = get_associated_token_address(
            owner, Pubkey.from_string(WSOL_MINT), programs[WSOL_MINT]
        )
        wrapped = 0
        for ix in wrap:
            program_id = ix.get("programId")
            data = b58decode(ix.get("data", ""))
            if program_id == _ATA_PROGRAM:
                out.append(_check_create_ata(ix, funder, programs))
            elif program_id == _SYSTEM_PROGRAM:
                if len(data) != 12 or int.from_bytes(data[:4], "little") != 2:
                    raise _refuse("unexpected SOL wrap")
                if not _accounts_match(
                    ix, [(funder, True, True), (str(wsol_ata), False, True)]
                ):
                    raise _refuse("unexpected SOL wrap")
                lamports = int.from_bytes(data[4:], "little")
                wrapped += lamports
                if lamports <= 0 or wrapped > max_wrap_lamports:
                    raise _refuse("it wraps more SOL than requested")
                out.append(
                    transfer(
                        TransferParams(
                            from_pubkey=owner, to_pubkey=wsol_ata, lamports=lamports
                        )
                    )
                )
            elif program_id == str(programs[WSOL_MINT]):
                if data != bytes([17]) or not _accounts_match(
                    ix, [(str(wsol_ata), False, True)]
                ):
                    raise _refuse("unexpected SOL wrap")
                out.append(
                    sync_native(
                        SyncNativeParams(
                            program_id=programs[WSOL_MINT], account=wsol_ata
                        )
                    )
                )
            else:
                raise _refuse("unexpected SOL wrap")
        if wrapped == 0:
            raise _refuse("the SOL wrap moves nothing")

    delegate = svm.get("delegateIx")
    if not delegate:
        if svm.get("sponsoredDelegateTx"):
            raise _refuse("sponsored delegation isn't supported yet")
        return out
    program = programs[spent_mint]
    source = get_associated_token_address(
        owner, Pubkey.from_string(spent_mint), program
    )
    data = b58decode(delegate.get("data", ""))
    expected = [
        (str(source), False, True),
        (str(flash_delegate_pda()), False, False),
        (funder, True, False),
    ]
    if (
        delegate.get("programId") != str(program)
        or len(data) != 9
        or data[0] != 4
        or not _accounts_match(delegate, expected)
    ):
        raise _refuse("the delegation is not to Flash for this token")
    amount = int.from_bytes(data[1:], "little")
    if not 0 < amount <= max_delegate:
        raise _refuse("the delegation is larger than the wallet's balance")
    out.append(
        approve(
            ApproveParams(
                program_id=program,
                source=source,
                delegate=flash_delegate_pda(),
                owner=owner,
                amount=amount,
            )
        )
    )
    return out
