from __future__ import annotations

import pytest

from wayfinder_paths.adapters.avantis_adapter.adapter import AvantisAdapter
from wayfinder_paths.core.constants.contracts import (
    AVANTIS_AVUSDC,
    AVANTIS_VAULT_MANAGER,
    BASE_USDC,
)


@pytest.fixture
def adapter():
    return AvantisAdapter(
        config={"strategy_wallet": {"address": "0x1234567890123456789012345678901234567890"}}
    )


def test_adapter_type(adapter):
    assert adapter.adapter_type == "AVANTIS"


def test_default_addresses(adapter):
    assert adapter.chain_id == 8453
    assert adapter.vault == AVANTIS_AVUSDC
    assert adapter.vault_manager == AVANTIS_VAULT_MANAGER
    assert adapter.underlying == BASE_USDC


@pytest.mark.asyncio
async def test_borrow_and_repay_unsupported(adapter):
    ok, msg = await adapter.borrow()
    assert ok is False
    assert "does not support" in msg.lower()

    ok, msg = await adapter.repay()
    assert ok is False
    assert "does not support" in msg.lower()


@pytest.mark.asyncio
async def test_lend_requires_signing_callback(adapter):
    ok, msg = await adapter.lend(amount=1)
    assert ok is False
    assert "signing callback" in str(msg).lower()

