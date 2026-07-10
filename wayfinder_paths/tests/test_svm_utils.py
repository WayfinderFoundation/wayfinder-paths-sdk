from __future__ import annotations

import base64
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from solders.hash import Hash
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import ID as SYS_PROGRAM_ID
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction
from solders.transaction_status import TransactionConfirmationStatus
from spl.token._layouts import MINT_LAYOUT
from spl.token.constants import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
)
from spl.token.instructions import get_associated_token_address as spl_reference_ata
from spl.token.instructions import transfer_checked
from spl.token.models import TransferCheckedParams

from wayfinder_paths.core.constants.chains import (
    CHAIN_ID_BASE,
    CHAIN_ID_SOLANA,
    SVM_CHAIN_IDS,
)
from wayfinder_paths.core.utils import tokens
from wayfinder_paths.core.utils.svm import (
    is_solana_chain,
    solana_client_from_chain_id,
)
from wayfinder_paths.core.utils.svm_tokens import (
    SOL_NATIVE_SENTINEL,
    WRAPPED_SOL_MINT,
    build_solana_send_transaction,
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
RECIPIENT = str(Pubkey.from_bytes(b"\x07" * 32))
# Derived offline with solana-py's reference implementation.
EXPECTED_USDC_ATA = "F8biqkCRK2tHR6EncrcXDGgVTkGRrtojqyW39w41Qspn"
EXPECTED_USDC_ATA_2022 = "8UQrn3SEPVqkggQ7Y7QEpGxutSyYQgJVFsgSxzwge858"


def _mint_data(decimals: int) -> bytes:
    return MINT_LAYOUT.build(
        {
            "mint_authority_option": 0,
            "mint_authority": bytes(32),
            "supply": 1_000_000,
            "decimals": decimals,
            "is_initialized": 1,
            "freeze_authority_option": 0,
            "freeze_authority": bytes(32),
        }
    )


def _blockhash_resp(last_valid_block_height: int = 250_000_000) -> SimpleNamespace:
    return SimpleNamespace(
        value=SimpleNamespace(
            blockhash=Hash.default(),
            last_valid_block_height=last_valid_block_height,
        )
    )


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
    mint_data = _mint_data(decimals=6)
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


# ---------------------------------------------------------------------------
# Transfer envelope builder (mocked RPC)
# ---------------------------------------------------------------------------


def _decode_envelope(envelope: dict) -> VersionedTransaction:
    assert envelope["chainType"] == "solana"
    assert envelope["chainId"] == CHAIN_ID_SOLANA
    return VersionedTransaction.from_bytes(
        base64.b64decode(envelope["serializedTransaction"])
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("token_address", [None, SOL_NATIVE_SENTINEL, "native"])
async def test_build_solana_send_transaction_native(token_address):
    fake = AsyncMock()
    fake.get_latest_blockhash = AsyncMock(return_value=_blockhash_resp())
    with _patch_client(fake):
        envelope = await build_solana_send_transaction(
            OWNER, RECIPIENT, token_address, 1_000_000
        )

    assert envelope["lastValidBlockHeight"] == 250_000_000
    tx = _decode_envelope(envelope)
    msg = tx.message

    # Sender is fee payer; single system transfer instruction.
    assert msg.account_keys[0] == Pubkey.from_string(OWNER)
    assert len(msg.instructions) == 1
    ix = msg.instructions[0]
    assert msg.account_keys[ix.program_id_index] == SYS_PROGRAM_ID
    expected = transfer(
        TransferParams(
            from_pubkey=Pubkey.from_string(OWNER),
            to_pubkey=Pubkey.from_string(RECIPIENT),
            lamports=1_000_000,
        )
    )
    assert bytes(ix.data) == bytes(expected.data)

    # Unsigned envelope: placeholder signatures only.
    assert len(tx.signatures) == msg.header.num_required_signatures
    assert all(sig == Signature.default() for sig in tx.signatures)
    # Native path needs no account reads.
    fake.get_account_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_build_solana_send_transaction_spl_existing_ata():
    mint_account = SimpleNamespace(owner=TOKEN_PROGRAM_ID, data=_mint_data(decimals=6))
    fake = AsyncMock()
    fake.get_account_info = AsyncMock(
        side_effect=[
            SimpleNamespace(value=mint_account),  # token program resolve
            SimpleNamespace(value=SimpleNamespace(owner=TOKEN_PROGRAM_ID)),  # dest ATA
            SimpleNamespace(value=mint_account),  # decimals
        ]
    )
    fake.get_latest_blockhash = AsyncMock(return_value=_blockhash_resp())
    with _patch_client(fake):
        envelope = await build_solana_send_transaction(
            OWNER, RECIPIENT, USDC_MINT, 2_500_000
        )

    tx = _decode_envelope(envelope)
    msg = tx.message

    owner = Pubkey.from_string(OWNER)
    recipient = Pubkey.from_string(RECIPIENT)
    mint = Pubkey.from_string(USDC_MINT)
    dest_ata = get_associated_token_address(recipient, mint, TOKEN_PROGRAM_ID)

    # Existing ATA: no create instruction, just transfer_checked.
    assert len(msg.instructions) == 1
    ix = msg.instructions[0]
    assert msg.account_keys[ix.program_id_index] == TOKEN_PROGRAM_ID
    expected = transfer_checked(
        TransferCheckedParams(
            program_id=TOKEN_PROGRAM_ID,
            source=get_associated_token_address(owner, mint, TOKEN_PROGRAM_ID),
            mint=mint,
            dest=dest_ata,
            owner=owner,
            amount=2_500_000,
            decimals=6,
        )
    )
    assert bytes(ix.data) == bytes(expected.data)
    assert dest_ata in list(msg.account_keys)
    # The existence probe hit the derived destination ATA.
    assert fake.get_account_info.await_args_list[1].args[0] == dest_ata


@pytest.mark.asyncio
async def test_build_solana_send_transaction_spl_missing_ata():
    mint_account = SimpleNamespace(owner=TOKEN_PROGRAM_ID, data=_mint_data(decimals=6))
    fake = AsyncMock()
    fake.get_account_info = AsyncMock(
        side_effect=[
            SimpleNamespace(value=mint_account),  # token program resolve
            SimpleNamespace(value=None),  # dest ATA missing
            SimpleNamespace(value=mint_account),  # decimals
        ]
    )
    fake.get_latest_blockhash = AsyncMock(return_value=_blockhash_resp())
    with _patch_client(fake):
        envelope = await build_solana_send_transaction(OWNER, RECIPIENT, USDC_MINT, 42)

    tx = _decode_envelope(envelope)
    msg = tx.message

    # Missing ATA: idempotent create for the recipient, then transfer_checked.
    assert len(msg.instructions) == 2
    create_ix, transfer_ix = msg.instructions
    assert msg.account_keys[create_ix.program_id_index] == ASSOCIATED_TOKEN_PROGRAM_ID
    assert bytes(create_ix.data) == bytes([1])  # CreateIdempotent discriminator
    assert msg.account_keys[transfer_ix.program_id_index] == TOKEN_PROGRAM_ID
    assert bytes(transfer_ix.data)[0] == 12  # TransferChecked discriminator

    # The create instruction's payer is the sender.
    assert msg.account_keys[create_ix.accounts[0]] == Pubkey.from_string(OWNER)


@pytest.mark.asyncio
async def test_build_solana_send_transaction_token_2022():
    mint_account = SimpleNamespace(
        owner=TOKEN_2022_PROGRAM_ID, data=_mint_data(decimals=9)
    )
    fake = AsyncMock()
    fake.get_account_info = AsyncMock(
        side_effect=[
            SimpleNamespace(value=mint_account),  # token program resolve
            SimpleNamespace(value=None),  # dest ATA missing
            SimpleNamespace(value=mint_account),  # decimals
        ]
    )
    fake.get_latest_blockhash = AsyncMock(return_value=_blockhash_resp())
    with _patch_client(fake):
        envelope = await build_solana_send_transaction(OWNER, RECIPIENT, USDC_MINT, 10)

    tx = _decode_envelope(envelope)
    msg = tx.message

    recipient = Pubkey.from_string(RECIPIENT)
    mint = Pubkey.from_string(USDC_MINT)

    assert len(msg.instructions) == 2
    create_ix, transfer_ix = msg.instructions
    # Transfer runs under Token-2022, and the ATA is derived under Token-2022.
    assert msg.account_keys[transfer_ix.program_id_index] == TOKEN_2022_PROGRAM_ID
    dest_ata_2022 = get_associated_token_address(recipient, mint, TOKEN_2022_PROGRAM_ID)
    assert dest_ata_2022 in list(msg.account_keys)
    assert get_associated_token_address(recipient, mint, TOKEN_PROGRAM_ID) not in list(
        msg.account_keys
    )
    assert bytes(create_ix.data) == bytes([1])
    assert bytes(transfer_ix.data)[9] == 9  # transfer_checked decimals byte


@pytest.mark.asyncio
async def test_build_solana_send_transaction_rejects_nonpositive_amount():
    with pytest.raises(ValueError, match="must be positive"):
        await build_solana_send_transaction(OWNER, RECIPIENT, None, 0)
    with pytest.raises(ValueError, match="must be positive"):
        await build_solana_send_transaction(OWNER, RECIPIENT, USDC_MINT, -1)


# ---------------------------------------------------------------------------
# tokens.py choke-point dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_token_balance_dispatches_to_solana():
    mock_solana = AsyncMock(return_value=42)
    with (
        patch(
            "wayfinder_paths.core.utils.svm_tokens.get_solana_token_balance",
            new=mock_solana,
        ),
        patch("wayfinder_paths.core.utils.tokens.web3_from_chain_id") as mock_web3,
    ):
        out = await tokens.get_token_balance(USDC_MINT, CHAIN_ID_SOLANA, OWNER)

    assert out == 42
    mock_solana.assert_awaited_once_with(OWNER, USDC_MINT, CHAIN_ID_SOLANA)
    mock_web3.assert_not_called()


@pytest.mark.asyncio
async def test_get_token_decimals_solana_native_is_nine():
    with patch("wayfinder_paths.core.utils.tokens.web3_from_chain_id") as mock_web3:
        assert await tokens.get_token_decimals(None, CHAIN_ID_SOLANA) == 9
        assert (
            await tokens.get_token_decimals(SOL_NATIVE_SENTINEL, CHAIN_ID_SOLANA) == 9
        )
        # The EVM-only default_native_decimals is ignored on the Solana branch.
        assert (
            await tokens.get_token_decimals(
                None, CHAIN_ID_SOLANA, default_native_decimals=18
            )
            == 9
        )
    mock_web3.assert_not_called()


@pytest.mark.asyncio
async def test_get_token_decimals_dispatches_to_solana_mint():
    mock_decimals = AsyncMock(return_value=6)
    with (
        patch(
            "wayfinder_paths.core.utils.svm_tokens.get_spl_mint_decimals",
            new=mock_decimals,
        ),
        patch("wayfinder_paths.core.utils.tokens.web3_from_chain_id") as mock_web3,
    ):
        assert await tokens.get_token_decimals(USDC_MINT, CHAIN_ID_SOLANA) == 6

    mock_decimals.assert_awaited_once_with(USDC_MINT, CHAIN_ID_SOLANA)
    mock_web3.assert_not_called()


@pytest.mark.asyncio
async def test_get_token_balance_with_decimals_dispatches_to_solana():
    with (
        patch(
            "wayfinder_paths.core.utils.svm_tokens.get_solana_token_balance",
            new=AsyncMock(return_value=123_456),
        ) as mock_balance,
        patch(
            "wayfinder_paths.core.utils.svm_tokens.get_spl_mint_decimals",
            new=AsyncMock(return_value=6),
        ) as mock_decimals,
        patch("wayfinder_paths.core.utils.tokens.web3_from_chain_id") as mock_web3,
    ):
        out = await tokens.get_token_balance_with_decimals(
            USDC_MINT, CHAIN_ID_SOLANA, OWNER
        )

    assert out == (123_456, 6)
    mock_balance.assert_awaited_once_with(OWNER, USDC_MINT, CHAIN_ID_SOLANA)
    mock_decimals.assert_awaited_once_with(USDC_MINT, CHAIN_ID_SOLANA)
    mock_web3.assert_not_called()


@pytest.mark.asyncio
async def test_build_send_transaction_dispatches_to_solana():
    envelope = {
        "chainType": "solana",
        "chainId": CHAIN_ID_SOLANA,
        "serializedTransaction": "AAo=",
        "lastValidBlockHeight": 1,
    }
    mock_build = AsyncMock(return_value=envelope)
    with (
        patch(
            "wayfinder_paths.core.utils.svm_tokens.build_solana_send_transaction",
            new=mock_build,
        ),
        patch("wayfinder_paths.core.utils.tokens.web3_from_chain_id") as mock_web3,
    ):
        out = await tokens.build_send_transaction(
            from_address=OWNER,
            to_address=RECIPIENT,
            token_address=USDC_MINT,
            chain_id=CHAIN_ID_SOLANA,
            amount=5,
        )

    assert out is envelope
    mock_build.assert_awaited_once_with(
        from_address=OWNER,
        to_address=RECIPIENT,
        token_address=USDC_MINT,
        amount=5,
        chain_id=CHAIN_ID_SOLANA,
    )
    mock_web3.assert_not_called()


@pytest.mark.asyncio
async def test_get_token_balance_evm_path_bypasses_solana_dispatch():
    fake_w3 = MagicMock()
    fake_w3.to_checksum_address = lambda addr: addr
    fake_w3.eth.get_balance = AsyncMock(return_value=5)
    with patch(
        "wayfinder_paths.core.utils.svm_tokens.get_solana_token_balance",
        new=AsyncMock(),
    ) as mock_solana:
        out = await tokens.get_token_balance(
            None, CHAIN_ID_BASE, "0xWallet", web3=fake_w3
        )

    assert out == 5
    mock_solana.assert_not_awaited()
    fake_w3.eth.get_balance.assert_awaited_once_with(
        "0xWallet", block_identifier="pending"
    )


# ---------------------------------------------------------------------------
# BalanceAdapter via the choke-point dispatch (mocked svm clients)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_balance_adapter_get_balance_solana():
    from wayfinder_paths.adapters.balance_adapter.adapter import BalanceAdapter

    adapter = BalanceAdapter(config={})
    mock_balance = AsyncMock(return_value=999)
    with patch(
        "wayfinder_paths.core.utils.svm_tokens.get_solana_token_balance",
        new=mock_balance,
    ):
        ok, balance = await adapter.get_balance(
            wallet_address=OWNER,
            token_address=USDC_MINT,
            chain_id=CHAIN_ID_SOLANA,
        )

    assert ok is True
    assert balance == 999
    mock_balance.assert_awaited_once_with(OWNER, USDC_MINT, CHAIN_ID_SOLANA)


@pytest.mark.asyncio
async def test_balance_adapter_get_balance_details_solana():
    from wayfinder_paths.adapters.balance_adapter.adapter import BalanceAdapter

    adapter = BalanceAdapter(config={})
    with (
        patch(
            "wayfinder_paths.core.utils.svm_tokens.get_solana_token_balance",
            new=AsyncMock(return_value=1_234_567),
        ),
        patch(
            "wayfinder_paths.core.utils.svm_tokens.get_spl_mint_decimals",
            new=AsyncMock(return_value=6),
        ),
    ):
        ok, out = await adapter.get_balance_details(
            wallet_address=OWNER,
            token_address=USDC_MINT,
            chain_id=CHAIN_ID_SOLANA,
        )

    assert ok is True
    assert isinstance(out, dict)
    assert out["chain_id"] == CHAIN_ID_SOLANA
    assert out["token_address"] == USDC_MINT
    assert out["wallet_address"] == OWNER
    assert out["balance_raw"] == 1_234_567
    assert out["decimals"] == 6
    assert out["balance_decimal"] == pytest.approx(1.234567)
