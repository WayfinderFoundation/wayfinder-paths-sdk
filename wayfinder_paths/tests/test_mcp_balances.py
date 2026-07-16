from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.mcp.tools.wallets import core_get_wallets


@pytest.fixture
def mock_wallet():
    return {"label": "test", "address": "0x000000000000000000000000000000000000dEaD"}


@pytest.mark.asyncio
async def test_get_wallets_includes_solana(mock_wallet):
    """Solana is a supported chain now: balances must NOT be stripped."""
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
        out = await core_get_wallets(label="test")

    assert out["ok"] is True
    data = out["result"]
    assert len(data["wallets"]) == 1
    balances_data = data["wallets"][0]["balances"]
    networks = [b["network"].lower() for b in balances_data["balances"]]
    assert "solana" in networks
    assert balances_data["total_balance_usd"] == pytest.approx(1002.5)
    solana_entry = next(
        b for b in balances_data["balances"] if b["network"].lower() == "solana"
    )
    assert solana_entry["balanceUSD"] == pytest.approx(999.0)


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
