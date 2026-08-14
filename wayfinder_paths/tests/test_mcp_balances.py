from __future__ import annotations

from unittest.mock import AsyncMock, call, patch

import pytest

from wayfinder_paths.mcp.tools.wallets import core_get_wallets


@pytest.fixture
def mock_wallet_ring():
    return [
        {
            "label": "test",
            "address": "0x000000000000000000000000000000000000dEaD",
            "chain_type": "ethereum",
        },
        {
            "label": "test",
            "address": "BTXGZD6APaEPLUnELUT3Q1HWUYaWatu42WXT3YCU1vxY",
            "chain_type": "solana",
        },
    ]


@pytest.mark.asyncio
async def test_get_wallets_label_fetches_all_ring_legs(mock_wallet_ring):
    fake_client = AsyncMock()
    fake_client.get_enriched_wallet_balances = AsyncMock(
        side_effect=[
            {"balances": [{"chain": "base", "symbol": "ETH", "value_usd": 1.0}]},
            {"balances": [{"chain": "solana", "symbol": "SOL", "value_usd": 8.0}]},
        ]
    )

    with (
        patch("wayfinder_paths.mcp.tools.wallets.BALANCE_CLIENT", fake_client),
        patch(
            "wayfinder_paths.mcp.tools.wallets.load_wallet_ring",
            new=AsyncMock(return_value=mock_wallet_ring),
        ),
    ):
        out = await core_get_wallets(label="test")

    assert out["ok"] is True
    assert fake_client.get_enriched_wallet_balances.await_args_list == [
        call(
            wallet_address=mock_wallet_ring[0]["address"],
            exclude_spam_tokens=True,
        ),
        call(
            svm_address=mock_wallet_ring[1]["address"],
            exclude_spam_tokens=True,
        ),
    ]
    wallets = out["result"]["wallets"]
    assert [(wallet["address"], wallet["chain_type"]) for wallet in wallets] == [
        (mock_wallet_ring[0]["address"], "ethereum"),
        (mock_wallet_ring[1]["address"], "solana"),
    ]
    assert wallets[0]["balances"]["balances"][0]["symbol"] == "ETH"
    assert wallets[1]["balances"]["balances"][0]["symbol"] == "SOL"


@pytest.mark.asyncio
async def test_get_wallets_label_not_found():
    with patch(
        "wayfinder_paths.mcp.tools.wallets.load_wallet_ring",
        new=AsyncMock(return_value=[]),
    ):
        out = await core_get_wallets(label="nonexistent")

    assert out["ok"] is False
    assert out["error"]["code"] == "not_found"
    assert "not found" in out["error"]["message"].lower()
