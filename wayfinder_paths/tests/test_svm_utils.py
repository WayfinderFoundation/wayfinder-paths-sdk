from __future__ import annotations

import base64
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction_status import TransactionConfirmationStatus
from spl.token._layouts import MINT_LAYOUT
from spl.token.constants import TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID
from spl.token.instructions import get_associated_token_address as spl_reference_ata

from wayfinder_paths.core.constants.chains import (
    CHAIN_ID_BASE,
    CHAIN_ID_SOLANA,
    SVM_CHAIN_IDS,
)
from wayfinder_paths.core.utils.svm import (
    is_solana_chain,
    solana_client_from_chain_id,
)
from wayfinder_paths.core.utils.svm_tokens import (
    SOL_NATIVE_SENTINEL,
    WRAPPED_SOL_MINT,
    get_associated_token_address,
    get_sol_balance,
    get_solana_token_balance,
    get_spl_mint_decimals,
    get_spl_token_balance,
)
from wayfinder_paths.core.utils.svm_transaction import (
    confirm_solana_signature,
    send_solana_transaction,
)
from wayfinder_paths.core.utils.token_refs import looks_like_solana_address

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
OWNER = "4Nd1mBQtrMJVYVfKf2PJy9NZUZdTAsp7D4xWLs4gDB4T"
# Derived offline with solana-py's reference implementation.
EXPECTED_USDC_ATA = "F8biqkCRK2tHR6EncrcXDGgVTkGRrtojqyW39w41Qspn"
EXPECTED_USDC_ATA_2022 = "8UQrn3SEPVqkggQ7Y7QEpGxutSyYQgJVFsgSxzwge858"


@contextmanager
def _patch_client(fake_client):
    @asynccontextmanager
    async def fake_ctx(chain_id=CHAIN_ID_SOLANA, commitment=None):
        yield fake_client

    with (
        patch(
            "wayfinder_paths.core.utils.svm_tokens.solana_client_from_chain_id",
            fake_ctx,
        ),
        patch(
            "wayfinder_paths.core.utils.svm_transaction.solana_client_from_chain_id",
            fake_ctx,
        ),
    ):
        yield


# ---------------------------------------------------------------------------
# Chain / address predicates
# ---------------------------------------------------------------------------


def test_is_solana_chain():
    assert is_solana_chain(CHAIN_ID_SOLANA)
    assert is_solana_chain("900")
    assert CHAIN_ID_SOLANA in SVM_CHAIN_IDS
    assert not is_solana_chain(CHAIN_ID_BASE)
    assert not is_solana_chain(None)
    assert not is_solana_chain("solana")


def test_looks_like_solana_address():
    assert looks_like_solana_address(USDC_MINT)
    assert looks_like_solana_address(SOL_NATIVE_SENTINEL)
    assert looks_like_solana_address(WRAPPED_SOL_MINT)
    assert not looks_like_solana_address(None)
    assert not looks_like_solana_address("")
    assert not looks_like_solana_address("usdc")
    assert not looks_like_solana_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
    # base58 excludes 0, O, I, l
    assert not looks_like_solana_address("0" * 40)


# ---------------------------------------------------------------------------
# ATA derivation (fully offline)
# ---------------------------------------------------------------------------


def test_ata_derivation_known_vector():
    owner = Pubkey.from_string(OWNER)
    mint = Pubkey.from_string(USDC_MINT)
    assert str(get_associated_token_address(owner, mint)) == EXPECTED_USDC_ATA
    assert (
        str(get_associated_token_address(owner, mint, TOKEN_PROGRAM_ID))
        == EXPECTED_USDC_ATA
    )


def test_ata_derivation_token_2022_known_vector():
    owner = Pubkey.from_string(OWNER)
    mint = Pubkey.from_string(USDC_MINT)
    assert (
        str(get_associated_token_address(owner, mint, TOKEN_2022_PROGRAM_ID))
        == EXPECTED_USDC_ATA_2022
    )


def test_ata_derivation_matches_spl_reference():
    owner = Pubkey.from_string(OWNER)
    mint = Pubkey.from_string(WRAPPED_SOL_MINT)
    assert get_associated_token_address(owner, mint) == spl_reference_ata(owner, mint)
    assert get_associated_token_address(
        owner, mint, TOKEN_2022_PROGRAM_ID
    ) == spl_reference_ata(owner, mint, TOKEN_2022_PROGRAM_ID)


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_solana_client_rejects_evm_chain():
    with pytest.raises(ValueError, match="not a Solana chain"):
        async with solana_client_from_chain_id(CHAIN_ID_BASE):
            pass


@pytest.mark.asyncio
async def test_solana_client_uses_resolved_rpcs():
    with patch(
        "wayfinder_paths.core.utils.svm._get_rpcs_for_chain_id",
        return_value=["https://solana.example.com/rpc"],
    ) as mock_rpcs:
        async with solana_client_from_chain_id(CHAIN_ID_SOLANA) as client:
            assert client is not None
    mock_rpcs.assert_called_once_with(CHAIN_ID_SOLANA)


@pytest.mark.asyncio
async def test_solana_client_multi_rpc_no_leak():
    """With multiple resolved RPCs, only ONE client is built and it is closed."""
    fake_client = AsyncMock()
    with (
        patch(
            "wayfinder_paths.core.utils.svm._get_rpcs_for_chain_id",
            return_value=[
                "https://rpc-0.example.com/",
                "https://rpc-1.example.com/",
                "https://rpc-2.example.com/",
            ],
        ),
        patch(
            "wayfinder_paths.core.utils.svm._client_for_rpc",
            return_value=fake_client,
        ) as mock_factory,
    ):
        async with solana_client_from_chain_id(CHAIN_ID_SOLANA) as client:
            assert client is fake_client

    # Exactly one client constructed — for the first RPC only.
    mock_factory.assert_called_once()
    assert mock_factory.call_args.args[0] == "https://rpc-0.example.com/"
    # And that one client was closed on exit.
    fake_client.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# Balances (mocked RPC)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_sol_balance():
    fake = AsyncMock()
    fake.get_balance = AsyncMock(return_value=SimpleNamespace(value=1_500_000_000))
    with _patch_client(fake):
        assert await get_sol_balance(OWNER) == 1_500_000_000
    (pubkey,) = fake.get_balance.await_args.args
    assert pubkey == Pubkey.from_string(OWNER)


@pytest.mark.asyncio
async def test_get_spl_token_balance_missing_ata_returns_zero():
    fake = AsyncMock()
    mint_account = SimpleNamespace(owner=TOKEN_PROGRAM_ID, data=b"")
    fake.get_account_info = AsyncMock(
        side_effect=[
            SimpleNamespace(value=mint_account),  # mint lookup
            SimpleNamespace(value=None),  # ATA missing
        ]
    )
    with _patch_client(fake):
        assert await get_spl_token_balance(OWNER, USDC_MINT) == 0
    fake.get_token_account_balance.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_spl_token_balance_token_2022():
    fake = AsyncMock()
    mint_account = SimpleNamespace(owner=TOKEN_2022_PROGRAM_ID, data=b"")
    ata_account = SimpleNamespace(owner=TOKEN_2022_PROGRAM_ID, data=b"")
    fake.get_account_info = AsyncMock(
        side_effect=[
            SimpleNamespace(value=mint_account),
            SimpleNamespace(value=ata_account),
        ]
    )
    fake.get_token_account_balance = AsyncMock(
        return_value=SimpleNamespace(value=SimpleNamespace(amount="123456"))
    )
    with _patch_client(fake):
        assert await get_spl_token_balance(OWNER, USDC_MINT) == 123456

    # The balance must have been read from the Token-2022 ATA.
    (ata_pubkey,) = fake.get_token_account_balance.await_args.args
    expected = get_associated_token_address(
        Pubkey.from_string(OWNER),
        Pubkey.from_string(USDC_MINT),
        TOKEN_2022_PROGRAM_ID,
    )
    assert ata_pubkey == expected


@pytest.mark.asyncio
async def test_get_spl_token_balance_missing_mint_raises():
    fake = AsyncMock()
    fake.get_account_info = AsyncMock(return_value=SimpleNamespace(value=None))
    with _patch_client(fake):
        with pytest.raises(ValueError, match="mint account not found"):
            await get_spl_token_balance(OWNER, USDC_MINT)


@pytest.mark.asyncio
async def test_get_solana_token_balance_dispatch():
    fake = AsyncMock()
    fake.get_balance = AsyncMock(return_value=SimpleNamespace(value=42))
    with _patch_client(fake):
        assert await get_solana_token_balance(OWNER, SOL_NATIVE_SENTINEL) == 42
        assert await get_solana_token_balance(OWNER, "native") == 42
    fake.get_account_info.assert_not_awaited()

    fake_spl = AsyncMock()
    mint_account = SimpleNamespace(owner=TOKEN_PROGRAM_ID, data=b"")
    ata_account = SimpleNamespace(owner=TOKEN_PROGRAM_ID, data=b"")
    fake_spl.get_account_info = AsyncMock(
        side_effect=[
            SimpleNamespace(value=mint_account),
            SimpleNamespace(value=ata_account),
        ]
    )
    fake_spl.get_token_account_balance = AsyncMock(
        return_value=SimpleNamespace(value=SimpleNamespace(amount="7"))
    )
    with _patch_client(fake_spl):
        assert await get_solana_token_balance(OWNER, USDC_MINT) == 7


@pytest.mark.asyncio
async def test_get_spl_mint_decimals():
    mint_data = MINT_LAYOUT.build(
        {
            "mint_authority_option": 0,
            "mint_authority": bytes(32),
            "supply": 1_000_000,
            "decimals": 6,
            "is_initialized": 1,
            "freeze_authority_option": 0,
            "freeze_authority": bytes(32),
        }
    )
    fake = AsyncMock()
    fake.get_account_info = AsyncMock(
        return_value=SimpleNamespace(
            value=SimpleNamespace(owner=TOKEN_PROGRAM_ID, data=mint_data)
        )
    )
    with _patch_client(fake):
        assert await get_spl_mint_decimals(USDC_MINT) == 6
    # Native SOL needs no RPC call.
    assert await get_spl_mint_decimals(SOL_NATIVE_SENTINEL) == 9


# ---------------------------------------------------------------------------
# Send / confirm (mocked RPC)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_solana_transaction():
    raw = b"\x01" * 100
    serialized_b64 = base64.b64encode(raw).decode()
    signature = Signature.default()

    fake = AsyncMock()
    fake.send_raw_transaction = AsyncMock(return_value=SimpleNamespace(value=signature))
    with _patch_client(fake):
        out = await send_solana_transaction(serialized_b64)

    assert out == str(signature)
    args, kwargs = fake.send_raw_transaction.await_args
    assert args[0] == raw
    # Preflight simulation runs by default so send failures surface
    # immediately instead of as confirmation timeouts.
    assert kwargs["opts"].skip_preflight is False


@pytest.mark.asyncio
async def test_send_solana_transaction_skip_preflight_opt_out():
    fake = AsyncMock()
    fake.send_raw_transaction = AsyncMock(
        return_value=SimpleNamespace(value=Signature.default())
    )
    with _patch_client(fake):
        await send_solana_transaction(
            base64.b64encode(b"tx").decode(), skip_preflight=True
        )
    assert fake.send_raw_transaction.await_args.kwargs["opts"].skip_preflight is True


@pytest.mark.asyncio
async def test_confirm_solana_signature_success():
    sig = str(Signature.default())
    pending = SimpleNamespace(value=[None])
    confirmed = SimpleNamespace(
        value=[
            SimpleNamespace(
                slot=123,
                err=None,
                confirmation_status=TransactionConfirmationStatus.Confirmed,
            )
        ]
    )
    fake = AsyncMock()
    fake.get_signature_statuses = AsyncMock(side_effect=[pending, confirmed])
    with (
        _patch_client(fake),
        patch(
            "wayfinder_paths.core.utils.svm_transaction.asyncio.sleep", new=AsyncMock()
        ),
    ):
        out = await confirm_solana_signature(sig, timeout_s=30)

    assert out["confirmed"] is True
    assert out["slot"] == 123
    assert out["err"] is None
    assert out["confirmation_status"] == "confirmed"
    assert out["signature"] == sig
    # must search history so signatures older than the recent-status cache resolve
    for call in fake.get_signature_statuses.call_args_list:
        assert call.kwargs.get("search_transaction_history") is True


@pytest.mark.asyncio
async def test_confirm_solana_signature_error():
    sig = str(Signature.default())
    errored = SimpleNamespace(
        value=[
            SimpleNamespace(
                slot=99,
                err={"InstructionError": [0, "Custom"]},
                confirmation_status=TransactionConfirmationStatus.Processed,
            )
        ]
    )
    fake = AsyncMock()
    fake.get_signature_statuses = AsyncMock(return_value=errored)
    with _patch_client(fake):
        out = await confirm_solana_signature(sig, timeout_s=30)

    assert out["confirmed"] is False
    assert out["slot"] == 99
    assert "InstructionError" in out["err"]


@pytest.mark.asyncio
async def test_confirm_solana_signature_timeout():
    sig = str(Signature.default())
    fake = AsyncMock()
    fake.get_signature_statuses = AsyncMock(return_value=SimpleNamespace(value=[None]))
    with _patch_client(fake):
        with pytest.raises(TimeoutError, match="Timed out"):
            await confirm_solana_signature(sig, timeout_s=0)


# ---------------------------------------------------------------------------
# Token resolver integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_resolver_accepts_solana_mint():
    from wayfinder_paths.core.utils.token_resolver import TokenResolver

    chain_id, address = await TokenResolver.resolve_token(
        USDC_MINT, chain_id=CHAIN_ID_SOLANA
    )
    assert chain_id == CHAIN_ID_SOLANA
    assert address == USDC_MINT


@pytest.mark.asyncio
async def test_token_resolver_solana_native_sentinel():
    from wayfinder_paths.core.constants import ZERO_ADDRESS
    from wayfinder_paths.core.utils.token_resolver import TokenResolver

    chain_id, address = await TokenResolver.resolve_token(
        SOL_NATIVE_SENTINEL, chain_id=CHAIN_ID_SOLANA
    )
    assert chain_id == CHAIN_ID_SOLANA
    assert address == ZERO_ADDRESS


@pytest.mark.asyncio
async def test_token_resolver_meta_solana_mint():
    from wayfinder_paths.core.utils.token_resolver import TokenResolver

    with patch(
        "wayfinder_paths.core.utils.svm_tokens.get_spl_mint_decimals",
        new=AsyncMock(return_value=6),
    ):
        meta = await TokenResolver.resolve_token_meta(
            USDC_MINT, chain_id=CHAIN_ID_SOLANA
        )

    assert meta["chain_id"] == CHAIN_ID_SOLANA
    assert meta["address"] == USDC_MINT
    assert meta["decimals"] == 6
    assert meta["metadata"]["source"] == "address"
