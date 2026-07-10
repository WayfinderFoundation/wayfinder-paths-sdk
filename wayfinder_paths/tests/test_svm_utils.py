from __future__ import annotations

import base64
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import CompiledInstruction
from solders.keypair import Keypair
from solders.message import (
    Message,
    MessageAddressTableLookup,
    MessageHeader,
    MessageV0,
)
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction
from solders.transaction_status import TransactionConfirmationStatus
from spl.token._layouts import MINT_LAYOUT
from spl.token.constants import TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID
from spl.token.instructions import get_associated_token_address as spl_reference_ata

from wayfinder_paths.core.clients.WalletClient import WALLET_CLIENT
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
    COMPUTE_BUDGET_PROGRAM_ID,
    MAX_COMPUTE_UNIT_LIMIT,
    _send_sponsored_solana_transaction,
    apply_compute_budget,
    confirm_solana_signature,
    get_recent_priority_fee,
    send_solana_transaction,
    send_solana_versioned_transaction,
)
from wayfinder_paths.core.utils.token_refs import looks_like_solana_address
from wayfinder_paths.core.utils.transaction import (
    SponsorshipUnavailableError,
    TransactionRevertedError,
)

_SVM_TX_MODULE = "wayfinder_paths.core.utils.svm_transaction"

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


# ---------------------------------------------------------------------------
# Compute budget fixtures
# ---------------------------------------------------------------------------

# Real mainnet Jupiter v6 swap, fetched via getTransaction (base64 encoding):
# signature 2mwLo6ZzvtkiASJjZjrjwVeYaNmLEtdhQmvLMyd5t7MWZNcmaUm1xeQao55L4QdChmfqS2EzvubjpirnWBtbpiqF
# slot 432109218. v0 message with 18 static keys, 1 address table lookup
# (4 writable + 10 readonly entries), existing ComputeBudget limit+price
# instructions, and instructions referencing lookup-region indices (max 31).
_JUPITER_TX_FIXTURE = Path(__file__).parent / "fixtures" / "jupiter_swap_tx.b64"


def _load_jupiter_tx() -> VersionedTransaction:
    raw = base64.b64decode(_JUPITER_TX_FIXTURE.read_text().strip())
    return VersionedTransaction.from_bytes(raw)


def _fake_sim_client(units_consumed=100_000, err=None):
    fake = AsyncMock()
    fake.simulate_transaction = AsyncMock(
        return_value=SimpleNamespace(
            value=SimpleNamespace(err=err, units_consumed=units_consumed, logs=[])
        )
    )
    return fake


def _simple_transfer_tx(payer: Keypair) -> VersionedTransaction:
    ix = transfer(
        TransferParams(
            from_pubkey=payer.pubkey(),
            to_pubkey=Keypair().pubkey(),
            lamports=1_000,
        )
    )
    message = MessageV0.try_compile(payer.pubkey(), [ix], [], Hash.new_unique())
    return VersionedTransaction.populate(message, [Signature.default()])


def _tx_with_existing_compute_budget(payer: Keypair) -> VersionedTransaction:
    ixs = [
        set_compute_unit_limit(200_000),
        set_compute_unit_price(99),
        transfer(
            TransferParams(
                from_pubkey=payer.pubkey(),
                to_pubkey=Keypair().pubkey(),
                lamports=1_000,
            )
        ),
    ]
    message = MessageV0.try_compile(payer.pubkey(), ixs, [], Hash.new_unique())
    return VersionedTransaction.populate(message, [Signature.default()])


def _synthetic_alt_tx() -> VersionedTransaction:
    """v0 message with an address table lookup and NO ComputeBudget key.

    Combined account index space: static [payer, program] (0-1), then the
    lookup's writable entries (2-3), then its readonly entry (4).
    """
    payer, program, alt = Pubkey.new_unique(), Pubkey.new_unique(), Pubkey.new_unique()
    message = MessageV0(
        header=MessageHeader(
            num_required_signatures=1,
            num_readonly_signed_accounts=0,
            num_readonly_unsigned_accounts=1,
        ),
        account_keys=[payer, program],
        recent_blockhash=Hash.new_unique(),
        instructions=[
            CompiledInstruction(
                program_id_index=1, data=b"\x2a", accounts=bytes([0, 2, 3, 4])
            )
        ],
        address_table_lookups=[
            MessageAddressTableLookup(alt, bytes([0, 1]), bytes([2]))
        ],
    )
    return VersionedTransaction.populate(message, [Signature.default()])


def _compute_budget_values(tx: VersionedTransaction):
    """(cb_index, limits, prices) — asserts the CB key appears exactly once."""
    msg = tx.message
    positions = [
        i for i, k in enumerate(msg.account_keys) if k == COMPUTE_BUDGET_PROGRAM_ID
    ]
    assert len(positions) == 1
    cb_index = positions[0]
    cb_ixs = [ix for ix in msg.instructions if ix.program_id_index == cb_index]
    limits = [
        int.from_bytes(ix.data[1:5], "little") for ix in cb_ixs if ix.data[0] == 2
    ]
    prices = [
        int.from_bytes(ix.data[1:9], "little") for ix in cb_ixs if ix.data[0] == 3
    ]
    assert len(cb_ixs) == len(limits) + len(prices)
    return cb_index, limits, prices


def _assert_indices_in_bounds(tx: VersionedTransaction):
    msg = tx.message
    total = (
        len(msg.account_keys)
        + sum(len(lu.writable_indexes) for lu in msg.address_table_lookups)
        + sum(len(lu.readonly_indexes) for lu in msg.address_table_lookups)
    )
    for ix in msg.instructions:
        assert ix.program_id_index < len(msg.account_keys)
        for account_index in ix.accounts:
            assert account_index < total


# ---------------------------------------------------------------------------
# apply_compute_budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_compute_budget_appends_cb_program_to_simple_transfer():
    payer = Keypair()
    tx = _simple_transfer_tx(payer)
    fake = _fake_sim_client(units_consumed=100_000)

    with _patch_client(fake):
        out = await apply_compute_budget(tx, priority_fee_micro_lamports=7_500)

    msg, orig = out.message, tx.message
    # CB program appended at the END of static keys: existing indices stable.
    assert list(msg.account_keys)[:-1] == list(orig.account_keys)
    assert list(msg.account_keys)[-1] == COMPUTE_BUDGET_PROGRAM_ID
    # Appended key is readonly+unsigned; signer counts untouched.
    assert (
        msg.header.num_readonly_unsigned_accounts
        == orig.header.num_readonly_unsigned_accounts + 1
    )
    assert msg.header.num_required_signatures == orig.header.num_required_signatures
    assert (
        msg.header.num_readonly_signed_accounts
        == orig.header.num_readonly_signed_accounts
    )

    _, limits, prices = _compute_budget_values(out)
    assert limits == [120_000]  # 100_000 * 1.2
    assert prices == [7_500]
    # Original instruction preserved verbatim after the two CB instructions.
    assert list(msg.instructions)[2:] == list(orig.instructions)
    assert msg.recent_blockhash == orig.recent_blockhash
    assert out.signatures == tx.signatures
    # Simulation ran against the ORIGINAL transaction without sig verification.
    fake.simulate_transaction.assert_awaited_once()
    sim_args, sim_kwargs = fake.simulate_transaction.await_args
    assert sim_args[0] == tx
    assert sim_kwargs["sig_verify"] is False
    # Message survives a serialization round trip.
    assert VersionedTransaction.from_bytes(bytes(out)) == out


@pytest.mark.asyncio
async def test_apply_compute_budget_caps_unit_limit():
    tx = _simple_transfer_tx(Keypair())
    with _patch_client(_fake_sim_client(units_consumed=2_000_000)):
        out = await apply_compute_budget(tx, priority_fee_micro_lamports=1)
    _, limits, _ = _compute_budget_values(out)
    assert limits == [MAX_COMPUTE_UNIT_LIMIT]


@pytest.mark.asyncio
async def test_apply_compute_budget_strips_existing_cb_instructions():
    payer = Keypair()
    tx = _tx_with_existing_compute_budget(payer)
    assert len(tx.message.instructions) == 3  # limit + price + transfer

    with _patch_client(_fake_sim_client(units_consumed=150_000)):
        out = await apply_compute_budget(tx, priority_fee_micro_lamports=42)

    msg, orig = out.message, tx.message
    # CB key was already present: no key appended, header unchanged.
    assert list(msg.account_keys) == list(orig.account_keys)
    assert msg.header == orig.header
    # Old limit (200_000) and price (99) replaced — exactly one of each.
    cb_index, limits, prices = _compute_budget_values(out)
    assert limits == [180_000]  # 150_000 * 1.2
    assert prices == [42]
    # Non-CB instructions preserved verbatim, in order.
    original_rest = [ix for ix in orig.instructions if ix.program_id_index != cb_index]
    assert list(msg.instructions)[2:] == original_rest
    assert len(msg.instructions) == 3
    assert VersionedTransaction.from_bytes(bytes(out)) == out


@pytest.mark.asyncio
async def test_apply_compute_budget_fetches_priority_fee_when_not_given():
    tx = _simple_transfer_tx(Keypair())
    with (
        _patch_client(_fake_sim_client(units_consumed=50_000)),
        patch(
            f"{_SVM_TX_MODULE}.get_recent_priority_fee",
            new=AsyncMock(return_value=1_234),
        ) as mock_fee,
    ):
        out = await apply_compute_budget(tx)
    _, _, prices = _compute_budget_values(out)
    assert prices == [1_234]
    mock_fee.assert_awaited_once_with(chain_id=CHAIN_ID_SOLANA)


@pytest.mark.asyncio
async def test_apply_compute_budget_synthetic_alt_shifts_lookup_indices():
    tx = _synthetic_alt_tx()
    orig = tx.message

    with _patch_client(_fake_sim_client(units_consumed=300_000)):
        out = await apply_compute_budget(tx, priority_fee_micro_lamports=11)

    msg = out.message
    # CB appended at static index 2; static indices 0-1 untouched.
    assert list(msg.account_keys) == [
        *list(orig.account_keys),
        COMPUTE_BUDGET_PROGRAM_ID,
    ]
    assert msg.header.num_readonly_unsigned_accounts == 2
    # Lookups and blockhash preserved bit-for-bit.
    assert msg.address_table_lookups == orig.address_table_lookups
    assert msg.recent_blockhash == orig.recent_blockhash
    # The original instruction's lookup-region indices (>= 2) shifted up by
    # one to make room for the CB key; the static index 0 did not move.
    moved = list(msg.instructions)[2]
    assert moved.program_id_index == 1
    assert list(moved.accounts) == [0, 3, 4, 5]
    assert moved.data == b"\x2a"
    _, limits, prices = _compute_budget_values(out)
    assert limits == [360_000]
    assert prices == [11]
    _assert_indices_in_bounds(out)
    assert VersionedTransaction.from_bytes(bytes(out)) == out


@pytest.mark.asyncio
async def test_apply_compute_budget_real_jupiter_swap():
    """Strip path against a real mainnet Jupiter v6 swap (see fixture note)."""
    tx = _load_jupiter_tx()
    orig = tx.message
    assert len(orig.account_keys) == 18
    assert len(orig.address_table_lookups) == 1
    orig_cb_index, orig_limits, orig_prices = _compute_budget_values(tx)
    assert len(orig_limits) == 1 and len(orig_prices) == 1  # Jupiter set both

    with _patch_client(_fake_sim_client(units_consumed=250_000)):
        out = await apply_compute_budget(tx, priority_fee_micro_lamports=42)

    msg = out.message
    # CB key was already static — no append, no index shifts anywhere.
    assert list(msg.account_keys) == list(orig.account_keys)
    assert msg.header == orig.header
    assert msg.address_table_lookups == orig.address_table_lookups
    assert msg.recent_blockhash == orig.recent_blockhash
    assert out.signatures == tx.signatures

    cb_index, limits, prices = _compute_budget_values(out)
    assert cb_index == orig_cb_index
    assert limits == [300_000]  # 250_000 * 1.2, Jupiter's own limit replaced
    assert prices == [42]
    # Jupiter's 5 non-CB instructions preserved verbatim, in order.
    original_rest = [
        ix for ix in orig.instructions if ix.program_id_index != orig_cb_index
    ]
    assert list(msg.instructions)[2:] == original_rest
    _assert_indices_in_bounds(out)
    assert VersionedTransaction.from_bytes(bytes(out)) == out


@pytest.mark.asyncio
async def test_apply_compute_budget_accepts_base64_input():
    tx = _simple_transfer_tx(Keypair())
    with _patch_client(_fake_sim_client(units_consumed=100_000)):
        out = await apply_compute_budget(
            base64.b64encode(bytes(tx)).decode(), priority_fee_micro_lamports=5
        )
    _, limits, prices = _compute_budget_values(out)
    assert limits == [120_000]
    assert prices == [5]


@pytest.mark.asyncio
async def test_apply_compute_budget_simulation_error_raises():
    tx = _simple_transfer_tx(Keypair())
    with _patch_client(_fake_sim_client(err="BlockhashNotFound")):
        with pytest.raises(RuntimeError, match="simulation failed"):
            await apply_compute_budget(tx, priority_fee_micro_lamports=1)


@pytest.mark.asyncio
async def test_apply_compute_budget_no_units_raises():
    tx = _simple_transfer_tx(Keypair())
    with _patch_client(_fake_sim_client(units_consumed=None)):
        with pytest.raises(RuntimeError, match="no compute units"):
            await apply_compute_budget(tx, priority_fee_micro_lamports=1)


@pytest.mark.asyncio
async def test_apply_compute_budget_rejects_legacy_message():
    payer = Keypair()
    ix = transfer(
        TransferParams(
            from_pubkey=payer.pubkey(),
            to_pubkey=Keypair().pubkey(),
            lamports=1_000,
        )
    )
    legacy = VersionedTransaction.populate(
        Message([ix], payer.pubkey()), [Signature.default()]
    )
    with pytest.raises(TypeError, match="v0 transaction message"):
        await apply_compute_budget(legacy, priority_fee_micro_lamports=1)


# ---------------------------------------------------------------------------
# get_recent_priority_fee
# ---------------------------------------------------------------------------


def _fee_client(fees: list[int]):
    fake = AsyncMock()
    fake.get_recent_prioritization_fees = AsyncMock(
        return_value=SimpleNamespace(
            value=[
                SimpleNamespace(slot=i, prioritization_fee=f)
                for i, f in enumerate(fees)
            ]
        )
    )
    return fake


@pytest.mark.asyncio
async def test_priority_fee_percentile_ignores_zero_samples():
    # 30 idle-slot zeros + 20 nonzero samples 100..2000. Nearest-rank 85th
    # percentile of the NONZERO samples: ceil(0.85 * 20) = 17th -> 1700.
    fees = [0] * 30 + [100 * i for i in range(1, 21)]
    fake = _fee_client(fees)
    with _patch_client(fake):
        assert await get_recent_priority_fee() == 1_700


@pytest.mark.asyncio
async def test_priority_fee_clamped_to_floor_and_ceiling():
    with _patch_client(_fee_client([1, 2, 3])):
        assert await get_recent_priority_fee() == 1_000  # floor
    with _patch_client(_fee_client([10_000_000, 20_000_000])):
        assert await get_recent_priority_fee() == 3_000_000  # ceiling
    with _patch_client(_fee_client([5, 6])):
        assert await get_recent_priority_fee(floor=2, ceiling=4) == 4


@pytest.mark.asyncio
async def test_priority_fee_no_nonzero_samples_returns_floor():
    with _patch_client(_fee_client([])):
        assert await get_recent_priority_fee() == 1_000
    with _patch_client(_fee_client([0, 0, 0])):
        assert await get_recent_priority_fee(floor=777) == 777


@pytest.mark.asyncio
async def test_priority_fee_converts_writable_accounts():
    fake = _fee_client([0])
    with _patch_client(fake):
        await get_recent_priority_fee(writable_accounts=[USDC_MINT])
    (addresses,) = fake.get_recent_prioritization_fees.await_args.args
    assert addresses == [Pubkey.from_string(USDC_MINT)]


class _FakeHttpResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHttpClient:
    payload: object = None
    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        _FakeHttpClient.calls.append((url, json))
        if isinstance(_FakeHttpClient.payload, Exception):
            raise _FakeHttpClient.payload
        return _FakeHttpResponse(_FakeHttpClient.payload)


@pytest.mark.asyncio
async def test_priority_fee_quicknode_escape_hatch():
    _FakeHttpClient.payload = {
        "result": {"per_compute_unit": {"percentiles": {"85": 12_345}}}
    }
    _FakeHttpClient.calls = []
    fake_rpc = _fee_client([500])
    with (
        patch(
            f"{_SVM_TX_MODULE}.get_solana_priority_fee_rpc",
            return_value="https://qn.example.com/rpc",
        ),
        patch("httpx.AsyncClient", _FakeHttpClient),
        _patch_client(fake_rpc),
    ):
        assert await get_recent_priority_fee() == 12_345

    # QuikNode answered: the generic RPC estimate was never consulted.
    fake_rpc.get_recent_prioritization_fees.assert_not_awaited()
    (url, payload) = _FakeHttpClient.calls[0]
    assert url == "https://qn.example.com/rpc"
    assert payload["method"] == "qn_estimatePriorityFees"


@pytest.mark.asyncio
async def test_priority_fee_quicknode_failure_falls_back_to_generic():
    _FakeHttpClient.payload = RuntimeError("qn exploded")
    _FakeHttpClient.calls = []
    fees = [0] * 5 + [100 * i for i in range(1, 21)]
    with (
        patch(
            f"{_SVM_TX_MODULE}.get_solana_priority_fee_rpc",
            return_value="https://qn.example.com/rpc",
        ),
        patch("httpx.AsyncClient", _FakeHttpClient),
        _patch_client(_fee_client(fees)),
    ):
        assert await get_recent_priority_fee() == 1_700


# ---------------------------------------------------------------------------
# send_solana_versioned_transaction
# ---------------------------------------------------------------------------


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.com")
    return httpx.HTTPStatusError(
        f"{status}", request=request, response=httpx.Response(status, request=request)
    )


def _remote_sign_callback(address="RemoteSolWallet1111111111111111111111111111"):
    async def sign_callback(transaction):
        sign_callback.signed_with = transaction
        return b"signed-bytes"

    sign_callback.signed_with = None
    sign_callback.wallet_address = address
    sign_callback.chain_type = "solana"
    return sign_callback


def _local_sign_callback():
    callback = _remote_sign_callback(address=None)
    callback.wallet_address = None
    return callback


_CONFIRMED = {
    "signature": "sig",
    "slot": 1,
    "err": None,
    "confirmation_status": "confirmed",
    "confirmed": True,
}


@pytest.mark.asyncio
async def test_send_versioned_sponsored_branch_sends_envelope():
    tx = _simple_transfer_tx(Keypair())
    sig = str(Signature.default())
    callback = _remote_sign_callback()

    with (
        patch(
            f"{_SVM_TX_MODULE}.sponsorship_enabled", new=AsyncMock(return_value=True)
        ),
        patch.object(
            WALLET_CLIENT,
            "send_privy_transaction_sponsored",
            new=AsyncMock(return_value={"hash": sig, "transaction_id": "t-1"}),
        ) as mock_sponsored,
        patch(f"{_SVM_TX_MODULE}.apply_compute_budget", new=AsyncMock()) as mock_budget,
        patch(
            f"{_SVM_TX_MODULE}.confirm_solana_signature",
            new=AsyncMock(return_value={**_CONFIRMED, "signature": sig}),
        ) as mock_confirm,
    ):
        out = await send_solana_versioned_transaction(tx, callback)

    assert out == sig
    address, envelope = mock_sponsored.await_args.args
    assert address == callback.wallet_address
    assert envelope == {
        "chainId": CHAIN_ID_SOLANA,
        "chainType": "solana",
        "serializedTransaction": base64.b64encode(bytes(tx)).decode(),
    }
    # Backend signs and broadcasts: no local compute budget, no local signing.
    mock_budget.assert_not_awaited()
    assert callback.signed_with is None
    mock_confirm.assert_awaited_once_with(sig, chain_id=CHAIN_ID_SOLANA)


@pytest.mark.asyncio
async def test_send_versioned_sponsored_accepts_base64_input():
    tx = _simple_transfer_tx(Keypair())
    serialized_b64 = base64.b64encode(bytes(tx)).decode()
    callback = _remote_sign_callback()

    with (
        patch(
            f"{_SVM_TX_MODULE}.sponsorship_enabled", new=AsyncMock(return_value=True)
        ),
        patch.object(
            WALLET_CLIENT,
            "send_privy_transaction_sponsored",
            new=AsyncMock(return_value={"hash": "sig", "transaction_id": "t-1"}),
        ) as mock_sponsored,
    ):
        out = await send_solana_versioned_transaction(
            serialized_b64, callback, wait_for_confirmation=False
        )

    assert out == "sig"
    _, envelope = mock_sponsored.await_args.args
    assert envelope["serializedTransaction"] == serialized_b64


@pytest.mark.asyncio
async def test_send_versioned_sponsored_4xx_falls_back_to_local():
    tx = _simple_transfer_tx(Keypair())
    budgeted = _simple_transfer_tx(Keypair())
    callback = _remote_sign_callback()

    with (
        patch(
            f"{_SVM_TX_MODULE}.sponsorship_enabled", new=AsyncMock(return_value=True)
        ),
        patch.object(
            WALLET_CLIENT,
            "send_privy_transaction_sponsored",
            new=AsyncMock(side_effect=_http_status_error(402)),
        ),
        patch(
            f"{_SVM_TX_MODULE}.apply_compute_budget",
            new=AsyncMock(return_value=budgeted),
        ) as mock_budget,
        patch(
            f"{_SVM_TX_MODULE}.send_solana_transaction",
            new=AsyncMock(return_value="local-sig"),
        ) as mock_send,
        patch(
            f"{_SVM_TX_MODULE}.confirm_solana_signature",
            new=AsyncMock(return_value=_CONFIRMED),
        ),
    ):
        out = await send_solana_versioned_transaction(tx, callback)

    assert out == "local-sig"
    mock_budget.assert_awaited_once_with(
        tx, chain_id=CHAIN_ID_SOLANA, cu_limit_multiplier=1.2
    )
    # The callback signs the BUDGETED transaction, not the original.
    assert callback.signed_with is budgeted
    (sent_b64,) = mock_send.await_args.args
    assert sent_b64 == base64.b64encode(b"signed-bytes").decode()


@pytest.mark.asyncio
async def test_send_versioned_sponsored_5xx_stays_fatal():
    tx = _simple_transfer_tx(Keypair())
    callback = _remote_sign_callback()
    with (
        patch(
            f"{_SVM_TX_MODULE}.sponsorship_enabled", new=AsyncMock(return_value=True)
        ),
        patch.object(
            WALLET_CLIENT,
            "send_privy_transaction_sponsored",
            new=AsyncMock(side_effect=_http_status_error(500)),
        ),
    ):
        with pytest.raises(httpx.HTTPStatusError):
            await send_solana_versioned_transaction(tx, callback)


@pytest.mark.asyncio
async def test_send_versioned_local_path_when_sponsorship_disabled():
    tx = _simple_transfer_tx(Keypair())
    budgeted = _simple_transfer_tx(Keypair())
    callback = _remote_sign_callback()

    with (
        patch(
            f"{_SVM_TX_MODULE}.sponsorship_enabled", new=AsyncMock(return_value=False)
        ),
        patch.object(
            WALLET_CLIENT, "send_privy_transaction_sponsored", new=AsyncMock()
        ) as mock_sponsored,
        patch(
            f"{_SVM_TX_MODULE}.apply_compute_budget",
            new=AsyncMock(return_value=budgeted),
        ),
        patch(
            f"{_SVM_TX_MODULE}.send_solana_transaction",
            new=AsyncMock(return_value="local-sig"),
        ),
        patch(
            f"{_SVM_TX_MODULE}.confirm_solana_signature",
            new=AsyncMock(return_value=_CONFIRMED),
        ),
    ):
        out = await send_solana_versioned_transaction(tx, callback)

    assert out == "local-sig"
    mock_sponsored.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_versioned_local_wallet_never_hits_sponsored():
    tx = _simple_transfer_tx(Keypair())
    budgeted = _simple_transfer_tx(Keypair())
    callback = _local_sign_callback()

    with (
        patch(f"{_SVM_TX_MODULE}.sponsorship_enabled", new=AsyncMock()) as mock_enabled,
        patch.object(
            WALLET_CLIENT, "send_privy_transaction_sponsored", new=AsyncMock()
        ) as mock_sponsored,
        patch(
            f"{_SVM_TX_MODULE}.apply_compute_budget",
            new=AsyncMock(return_value=budgeted),
        ),
        patch(
            f"{_SVM_TX_MODULE}.send_solana_transaction",
            new=AsyncMock(return_value="local-sig"),
        ),
        patch(
            f"{_SVM_TX_MODULE}.confirm_solana_signature",
            new=AsyncMock(return_value=_CONFIRMED),
        ),
    ):
        out = await send_solana_versioned_transaction(tx, callback)

    assert out == "local-sig"
    mock_enabled.assert_not_awaited()
    mock_sponsored.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_versioned_confirmation_failure_raises():
    tx = _simple_transfer_tx(Keypair())
    failed = {**_CONFIRMED, "confirmed": False, "err": "InstructionError(...)"}
    with (
        patch(
            f"{_SVM_TX_MODULE}.apply_compute_budget",
            new=AsyncMock(return_value=tx),
        ),
        patch(
            f"{_SVM_TX_MODULE}.send_solana_transaction",
            new=AsyncMock(return_value="bad-sig"),
        ),
        patch(
            f"{_SVM_TX_MODULE}.confirm_solana_signature",
            new=AsyncMock(return_value=failed),
        ),
    ):
        with pytest.raises(TransactionRevertedError, match="Solana transaction failed"):
            await send_solana_versioned_transaction(tx, _local_sign_callback())


@pytest.mark.asyncio
async def test_send_versioned_skips_confirmation_when_disabled():
    tx = _simple_transfer_tx(Keypair())
    with (
        patch(
            f"{_SVM_TX_MODULE}.apply_compute_budget",
            new=AsyncMock(return_value=tx),
        ),
        patch(
            f"{_SVM_TX_MODULE}.send_solana_transaction",
            new=AsyncMock(return_value="sig"),
        ),
        patch(
            f"{_SVM_TX_MODULE}.confirm_solana_signature", new=AsyncMock()
        ) as mock_confirm,
    ):
        out = await send_solana_versioned_transaction(
            tx, _local_sign_callback(), wait_for_confirmation=False
        )
    assert out == "sig"
    mock_confirm.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_versioned_requires_sign_callback():
    with pytest.raises(ValueError, match="sign_callback must be provided"):
        await send_solana_versioned_transaction(_simple_transfer_tx(Keypair()), None)


# ---------------------------------------------------------------------------
# _send_sponsored_solana_transaction rejection mapping / polling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 402, 403, 429])
async def test_sponsored_solana_rejection_maps_to_fallback_error(status):
    tx = _simple_transfer_tx(Keypair())
    with patch.object(
        WALLET_CLIENT,
        "send_privy_transaction_sponsored",
        new=AsyncMock(side_effect=_http_status_error(status)),
    ):
        with pytest.raises(SponsorshipUnavailableError):
            await _send_sponsored_solana_transaction("addr", tx, CHAIN_ID_SOLANA)


@pytest.mark.asyncio
async def test_sponsored_solana_polls_until_hash_lands():
    tx = _simple_transfer_tx(Keypair())
    with (
        patch.object(
            WALLET_CLIENT,
            "send_privy_transaction_sponsored",
            new=AsyncMock(return_value={"hash": None, "transaction_id": "t-9"}),
        ),
        patch.object(
            WALLET_CLIENT,
            "get_privy_transaction_status",
            new=AsyncMock(
                side_effect=[
                    {"status": "pending", "hash": None},
                    {"status": "confirmed", "hash": "landed-sig"},
                ]
            ),
        ) as mock_status,
        patch(f"{_SVM_TX_MODULE}.asyncio.sleep", new=AsyncMock()),
    ):
        out = await _send_sponsored_solana_transaction("addr", tx, CHAIN_ID_SOLANA)
    assert out == "landed-sig"
    assert mock_status.await_count == 2


@pytest.mark.asyncio
async def test_sponsored_solana_failed_status_maps_to_fallback_error():
    tx = _simple_transfer_tx(Keypair())
    with (
        patch.object(
            WALLET_CLIENT,
            "send_privy_transaction_sponsored",
            new=AsyncMock(return_value={"hash": None, "transaction_id": "t-9"}),
        ),
        patch.object(
            WALLET_CLIENT,
            "get_privy_transaction_status",
            new=AsyncMock(return_value={"status": "failed", "hash": None}),
        ),
        patch(f"{_SVM_TX_MODULE}.asyncio.sleep", new=AsyncMock()),
    ):
        with pytest.raises(
            SponsorshipUnavailableError, match="failed before broadcast"
        ):
            await _send_sponsored_solana_transaction("addr", tx, CHAIN_ID_SOLANA)
