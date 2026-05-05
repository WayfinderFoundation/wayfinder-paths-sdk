from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.mcp.tools.wallets import core_get_wallets


@pytest.fixture
def mock_wallet():
    return {"label": "test", "address": "0x000000000000000000000000000000000000dEaD"}


@pytest.mark.asyncio
async def test_get_wallets_filters_solana(mock_wallet):
    fake_client = AsyncMock()
    fake_client.get_enriched_wallet_balances = AsyncMock(
        return_value={
            "balances": [
                {"network": "base", "balanceUSD": 1.5},
                {"network": "solana", "balanceUSD": 999.0},
                {"network": "arbitrum", "balanceUSD": 2.0},
            ],
            "total_balance_usd": 1002.5,
        }
    )

    with (
        patch("wayfinder_paths.mcp.tools.wallets.BALANCE_CLIENT", fake_client),
        patch(
            "wayfinder_paths.mcp.tools.wallets.find_wallet_by_label",
            new=AsyncMock(return_value=mock_wallet),
        ),
    ):
        result = await core_get_wallets(label="test")

    data = json.loads(result)
    assert "error" not in data
    assert len(data["wallets"]) == 1
    balances_data = data["wallets"][0]["balances"]
    assert balances_data["total_balance_usd"] == pytest.approx(3.5)
    assert balances_data["chain_breakdown"]["base"] == pytest.approx(1.5)
    assert balances_data["chain_breakdown"]["arbitrum"] == pytest.approx(2.0)
    assert all(b["network"].lower() != "solana" for b in balances_data["balances"])


@pytest.mark.asyncio
async def test_get_wallets_label_not_found():
    with patch(
        "wayfinder_paths.mcp.tools.wallets.find_wallet_by_label",
        new=AsyncMock(return_value=None),
    ):
        result = await core_get_wallets(label="nonexistent")

    data = json.loads(result)
    assert "error" in data
    assert "not found" in data["error"].lower()
