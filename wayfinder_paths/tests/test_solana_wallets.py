from __future__ import annotations

import base64
from unittest.mock import AsyncMock, patch

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import Message, MessageV0, to_bytes_versioned
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction as SoldersLegacyTransaction
from solders.transaction import VersionedTransaction

from wayfinder_paths.core.clients.WalletClient import WALLET_CLIENT
from wayfinder_paths.core.utils.wallets import (
    create_remote_wallet,
    find_wallet_by_label,
    get_local_solana_sign_callback,
    get_wallet_sign_hash_callback,
    get_wallet_sign_typed_data_callback,
    get_wallet_signing_callback,
    load_remote_wallets,
    solana_keypair_from_base58,
    wallet_chain_type,
)


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def solana_keypair() -> Keypair:
    return Keypair()


def _unsigned_v0_transaction(payer: Keypair) -> VersionedTransaction:
    ix = transfer(
        TransferParams(
            from_pubkey=payer.pubkey(),
            to_pubkey=Keypair().pubkey(),
            lamports=1_000,
        )
    )
    message = MessageV0.try_compile(payer.pubkey(), [ix], [], Hash.default())
    return VersionedTransaction.populate(message, [Signature.default()])


def _unsigned_legacy_transaction(payer: Keypair) -> SoldersLegacyTransaction:
    ix = transfer(
        TransferParams(
            from_pubkey=payer.pubkey(),
            to_pubkey=Keypair().pubkey(),
            lamports=1_000,
        )
    )
    return SoldersLegacyTransaction.new_unsigned(Message([ix], payer.pubkey()))


def _solana_wallet(keypair: Keypair, label: str = "sol") -> dict:
    return {
        "label": label,
        "address": str(keypair.pubkey()),
        "chain_type": "solana",
        "private_key": str(keypair),  # base58-encoded 64-byte keypair
    }


# ---------------------------------------------------------------------------
# chain_type handling
# ---------------------------------------------------------------------------


def test_wallet_chain_type_defaults_to_ethereum():
    assert wallet_chain_type({}) == "ethereum"
    assert wallet_chain_type({"chain_type": None}) == "ethereum"
    assert wallet_chain_type({"chain_type": ""}) == "ethereum"
    assert wallet_chain_type({"chain_type": "Solana"}) == "solana"
    assert wallet_chain_type({"chain_type": "ethereum"}) == "ethereum"


def test_solana_keypair_from_base58_roundtrip(solana_keypair):
    kp = solana_keypair_from_base58(str(solana_keypair))
    assert kp.pubkey() == solana_keypair.pubkey()


# ---------------------------------------------------------------------------
# Local solana sign callback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_solana_sign_callback_versioned_transaction(solana_keypair):
    callback = get_local_solana_sign_callback(str(solana_keypair))
    assert callback.wallet_address is None
    assert callback.chain_type == "solana"

    unsigned = _unsigned_v0_transaction(solana_keypair)
    signed_bytes = await callback(unsigned)

    signed = VersionedTransaction.from_bytes(signed_bytes)
    sig = signed.signatures[0]
    assert sig != Signature.default()
    assert sig.verify(solana_keypair.pubkey(), to_bytes_versioned(signed.message))


@pytest.mark.asyncio
async def test_solana_sign_callback_accepts_bytes_and_base64(solana_keypair):
    callback = get_local_solana_sign_callback(str(solana_keypair))
    unsigned = _unsigned_v0_transaction(solana_keypair)

    from_bytes = await callback(bytes(unsigned))
    from_b64 = await callback(base64.b64encode(bytes(unsigned)).decode())

    assert from_bytes == from_b64
    signed = VersionedTransaction.from_bytes(from_bytes)
    assert signed.signatures[0].verify(
        solana_keypair.pubkey(), to_bytes_versioned(signed.message)
    )


@pytest.mark.asyncio
async def test_solana_sign_callback_legacy_transaction(solana_keypair):
    callback = get_local_solana_sign_callback(str(solana_keypair))
    unsigned = _unsigned_legacy_transaction(solana_keypair)

    signed_bytes = await callback(unsigned)

    signed = SoldersLegacyTransaction.from_bytes(signed_bytes)
    sig = signed.signatures[0]
    assert sig != Signature.default()
    assert sig.verify(solana_keypair.pubkey(), bytes(signed.message))


@pytest.mark.asyncio
async def test_solana_sign_callback_rejects_non_signer(solana_keypair):
    other = Keypair()
    callback = get_local_solana_sign_callback(str(other))
    unsigned = _unsigned_v0_transaction(solana_keypair)

    with pytest.raises(ValueError, match="not a required signer"):
        await callback(unsigned)


@pytest.mark.asyncio
async def test_solana_sign_callback_rejects_bad_input(solana_keypair):
    callback = get_local_solana_sign_callback(str(solana_keypair))
    with pytest.raises(TypeError, match="Unsupported Solana transaction type"):
        await callback(12345)


# ---------------------------------------------------------------------------
# Wallet resolution → signing callback dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_wallet_signing_callback_local_solana(solana_keypair):
    wallet = _solana_wallet(solana_keypair)
    with patch(
        "wayfinder_paths.core.utils.wallets.find_wallet_by_label",
        new=AsyncMock(return_value=wallet),
    ):
        callback, address = await get_wallet_signing_callback("sol")

    assert address == str(solana_keypair.pubkey())
    assert callback.chain_type == "solana"
    assert callback.wallet_address is None

    unsigned = _unsigned_v0_transaction(solana_keypair)
    signed = VersionedTransaction.from_bytes(await callback(unsigned))
    assert signed.signatures[0].verify(
        solana_keypair.pubkey(), to_bytes_versioned(signed.message)
    )


@pytest.mark.asyncio
async def test_get_wallet_signing_callback_remote_solana_unsupported(solana_keypair):
    wallet = {
        "label": "sol-remote",
        "address": str(solana_keypair.pubkey()),
        "chain_type": "solana",
        "type": "remote",
    }
    with patch(
        "wayfinder_paths.core.utils.wallets.find_wallet_by_label",
        new=AsyncMock(return_value=wallet),
    ):
        with pytest.raises(ValueError, match="remote Solana wallet signing"):
            await get_wallet_signing_callback("sol-remote")


@pytest.mark.asyncio
async def test_get_wallet_signing_callback_solana_missing_key(solana_keypair):
    wallet = {
        "label": "sol-nokey",
        "address": str(solana_keypair.pubkey()),
        "chain_type": "solana",
    }
    with patch(
        "wayfinder_paths.core.utils.wallets.find_wallet_by_label",
        new=AsyncMock(return_value=wallet),
    ):
        with pytest.raises(ValueError, match="missing private_key"):
            await get_wallet_signing_callback("sol-nokey")


@pytest.mark.asyncio
async def test_get_wallet_signing_callback_evm_unchanged():
    """EVM wallets without chain_type behave exactly as before."""
    wallet = {
        "label": "evm",
        "address": "0x000000000000000000000000000000000000dEaD",
        "private_key_hex": "0x" + "11" * 32,
    }
    with patch(
        "wayfinder_paths.core.utils.wallets.find_wallet_by_label",
        new=AsyncMock(return_value=wallet),
    ):
        callback, address = await get_wallet_signing_callback("evm")

    assert address == wallet["address"]
    assert callback.wallet_address is None
    assert callback.chain_type == "ethereum"


@pytest.mark.asyncio
async def test_typed_data_and_hash_callbacks_reject_solana(solana_keypair):
    wallet = _solana_wallet(solana_keypair)
    with patch(
        "wayfinder_paths.core.utils.wallets.find_wallet_by_label",
        new=AsyncMock(return_value=wallet),
    ):
        with pytest.raises(ValueError, match="EVM-only"):
            await get_wallet_sign_typed_data_callback("sol")
        with pytest.raises(ValueError, match="EVM-only"):
            await get_wallet_sign_hash_callback("sol")


# ---------------------------------------------------------------------------
# Remote wallet listing / creation (chain_type passthrough)
# ---------------------------------------------------------------------------


def _remote_listing_patches(raw: list[dict]):
    return (
        patch(
            "wayfinder_paths.core.utils.wallets.get_api_key",
            return_value="test-key",
        ),
        patch(
            "wayfinder_paths.core.utils.wallets.is_opencode_instance",
            return_value=True,
        ),
        patch(
            "wayfinder_paths.core.utils.wallets.get_opencode_instance_id",
            return_value="inst-1",
        ),
        patch.object(WALLET_CLIENT, "list_wallets", new=AsyncMock(return_value=raw)),
    )


@pytest.mark.asyncio
async def test_load_remote_wallets_solana_shape(solana_keypair):
    sol_address = str(solana_keypair.pubkey())
    raw = [
        {
            "wallet_address": sol_address,
            "label": "sol-remote",
            "chain_type": "solana",
            "wallet_type": "session",
            "session_expires_at": "2026-07-10T00:00:00Z",
            "session_expires_in": 3600,
        },
        {
            "wallet_address": "0x000000000000000000000000000000000000dEaD",
            "label": "evm-remote",
        },
    ]
    p1, p2, p3, p4 = _remote_listing_patches(raw)
    with p1, p2, p3, p4:
        wallets = await load_remote_wallets()

    assert len(wallets) == 2
    sol = wallets[0]
    assert sol == {
        "address": sol_address,
        "label": "sol-remote",
        "type": "remote",
        "chain_type": "solana",
        "wallet_type": "session",
        "session_expires_at": "2026-07-10T00:00:00Z",
        "session_expires_in": 3600,
    }
    # Missing chain_type defaults to ethereum (legacy remote wallets).
    assert wallets[1]["chain_type"] == "ethereum"


@pytest.mark.asyncio
async def test_find_wallet_by_label_solana_round_trip(solana_keypair):
    sol_address = str(solana_keypair.pubkey())
    raw = [
        {
            "wallet_address": sol_address,
            "label": "sol-remote",
            "chain_type": "solana",
            "wallet_type": "session",
        }
    ]
    p1, p2, p3, p4 = _remote_listing_patches(raw)
    with (
        p1,
        p2,
        p3,
        p4,
        patch(
            "wayfinder_paths.core.utils.wallets._load_local_wallets",
            return_value=[],
        ),
    ):
        wallet = await find_wallet_by_label("sol-remote")

    assert wallet is not None
    assert wallet["address"] == sol_address
    assert wallet["type"] == "remote"
    assert wallet_chain_type(wallet) == "solana"


@pytest.mark.asyncio
async def test_create_remote_wallet_sends_solana_chain_type(solana_keypair):
    sol_address = str(solana_keypair.pubkey())
    mock_request = AsyncMock(
        return_value=_Response(
            {
                "wallet_address": sol_address,
                "label": "sol-remote",
                "chain_type": "solana",
            }
        )
    )
    with (
        patch(
            "wayfinder_paths.core.clients.WalletClient.get_api_base_url",
            return_value="https://example.com/api/v1",
        ),
        patch.object(WALLET_CLIENT, "_authed_request", new=mock_request),
        patch(
            "wayfinder_paths.core.utils.wallets.is_opencode_instance",
            return_value=False,
        ),
    ):
        result = await create_remote_wallet(
            label="sol-remote", wallet_type="session", chain_type="solana"
        )

    assert result["wallet_address"] == sol_address
    mock_request.assert_awaited_once()
    args, kwargs = mock_request.await_args
    assert args == ("POST", "https://example.com/api/v1/wallets/")
    body = kwargs["json"]
    assert body["chain_type"] == "solana"
    assert body["wallet_type"] == "session"
    assert body["label"] == "sol-remote"
    assert body["policies"]  # session policy auto-built when none provided
