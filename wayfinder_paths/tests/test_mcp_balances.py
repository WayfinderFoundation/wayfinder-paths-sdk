from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.mcp.resources.wallets import get_wallet_balances


@pytest.fixture
def mock_wallet():
    return {"label": "test", "address": "0x000000000000000000000000000000000000dEaD"}


@pytest.mark.asyncio
async def test_get_wallet_balances_filters_solana(mock_wallet):
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
        patch("wayfinder_paths.mcp.resources.wallets.BALANCE_CLIENT", fake_client),
        patch(
            "wayfinder_paths.mcp.resources.wallets.find_wallet_by_label",
            return_value=mock_wallet,
        ),
    ):
        result = await get_wallet_balances("test")

    data = json.loads(result)
    assert "error" not in data
    balances_data = data["balances"]
    assert balances_data["total_balance_usd"] == pytest.approx(3.5)
    assert balances_data["chain_breakdown"]["base"] == pytest.approx(1.5)
    assert balances_data["chain_breakdown"]["arbitrum"] == pytest.approx(2.0)
    assert all(b["network"].lower() != "solana" for b in balances_data["balances"])


@pytest.mark.asyncio
async def test_get_wallet_balances_wallet_not_found():
    with patch(
        "wayfinder_paths.mcp.resources.wallets.find_wallet_by_label",
        return_value=None,
    ):
        result = await get_wallet_balances("nonexistent")

    data = json.loads(result)
    assert "error" in data
    assert "not found" in data["error"].lower()
