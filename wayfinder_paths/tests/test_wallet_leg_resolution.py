from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.core.constants.chains import CHAIN_ID_SOLANA
from wayfinder_paths.core.utils.wallets import (
    find_wallet_leg_for_chain,
    get_wallet_signing_callback_for_chain,
)

EVM_ADDR = "0x000000000000000000000000000000000000dEaD"
SVM_ADDR = "BTXGZD6APaEPLUnELUT3Q1HWUYaWatu42WXT3YCU1vxY"
ARBITRUM = 42161

RING = [
    {"address": EVM_ADDR, "label": "main", "type": "remote", "chain_type": "ethereum"},
    {"address": SVM_ADDR, "label": "main", "type": "remote", "chain_type": "solana"},
]


def _patch_wallets(wallets):
    return patch(
        "wayfinder_paths.core.utils.wallets.load_wallets",
        new=AsyncMock(return_value=wallets),
    )


@pytest.mark.asyncio
async def test_leg_for_evm_chain_returns_evm_leg():
    with _patch_wallets(RING):
        leg = await find_wallet_leg_for_chain("main", ARBITRUM)
    assert leg["address"] == EVM_ADDR


@pytest.mark.asyncio
async def test_leg_for_solana_chain_returns_svm_leg():
    with _patch_wallets(RING):
        leg = await find_wallet_leg_for_chain("main", CHAIN_ID_SOLANA)
    assert leg["address"] == SVM_ADDR


@pytest.mark.asyncio
async def test_leg_absent_for_chain_returns_none():
    evm_only = [RING[0]]
    with _patch_wallets(evm_only):
        assert await find_wallet_leg_for_chain("main", CHAIN_ID_SOLANA) is None


@pytest.mark.asyncio
async def test_signing_callback_falls_back_to_default_leg_when_absent():
    # No SVM leg loaded: the chain-aware callback falls back to the label default.
    evm_only = [RING[0]]
    with (
        _patch_wallets(evm_only),
        patch(
            "wayfinder_paths.core.utils.wallets.find_wallet_by_label",
            new=AsyncMock(return_value=RING[0]),
        ),
        patch(
            "wayfinder_paths.core.utils.wallets.get_remote_sign_callback",
            return_value=lambda tx: b"",
        ),
    ):
        _, address = await get_wallet_signing_callback_for_chain(
            "main", CHAIN_ID_SOLANA
        )
    assert address == EVM_ADDR
