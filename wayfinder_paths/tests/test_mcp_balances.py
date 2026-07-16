from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.mcp.tools.wallets import core_get_wallets


@pytest.fixture
def mock_wallet():
    return {
        "label": "test",
        "address": "0x000000000000000000000000000000000000dEaD",
        "chain_type": "ethereum",
    }


@pytest.mark.asyncio
async def test_get_wallets_evm_uses_address_param(mock_wallet):
    fake_client = AsyncMock()
    fake_client.get_enriched_wallet_balances = AsyncMock(return_value={"balances": []})

    with (
        patch("wayfinder_paths.mcp.tools.wallets.BALANCE_CLIENT", fake_client),
        patch(
            "wayfinder_paths.mcp.tools.wallets.find_wallet_by_label",
            new=AsyncMock(return_value=mock_wallet),
        ),
    ):
        out = await core_get_wallets(label="test")

    assert out["ok"] is True
    kwargs = fake_client.get_enriched_wallet_balances.await_args.kwargs
    assert kwargs["wallet_address"] == mock_wallet["address"]
    assert "svm_address" not in kwargs


@pytest.mark.asyncio
async def test_get_wallets_solana_uses_svm_address_param():
    svm_wallet = {
        "label": "test",
        "address": "BTXGZD6APaEPLUnELUT3Q1HWUYaWatu42WXT3YCU1vxY",
        "chain_type": "solana",
    }
    fake_client = AsyncMock()
    fake_client.get_enriched_wallet_balances = AsyncMock(
        return_value={"balances": [{"chain": "solana", "symbol": "SOL", "value_usd": 8.0}]}
    )

    with (
        patch("wayfinder_paths.mcp.tools.wallets.BALANCE_CLIENT", fake_client),
        patch(
            "wayfinder_paths.mcp.tools.wallets.find_wallet_by_label",
            new=AsyncMock(return_value=svm_wallet),
        ),
    ):
        out = await core_get_wallets(label="test")

    assert out["ok"] is True
    kwargs = fake_client.get_enriched_wallet_balances.await_args.kwargs
    assert kwargs["svm_address"] == svm_wallet["address"]
    assert "wallet_address" not in kwargs
    balances = out["result"]["wallets"][0]["balances"]["balances"]
    assert balances[0]["symbol"] == "SOL"


@pytest.mark.asyncio
async def test_get_wallets_label_not_found():
    with patch(
        "wayfinder_paths.mcp.tools.wallets.find_wallet_by_label",
        new=AsyncMock(return_value=None),
    ):
        out = await core_get_wallets(label="nonexistent")

    assert out["ok"] is False
    assert out["error"]["code"] == "not_found"
    assert "not found" in out["error"]["message"].lower()
