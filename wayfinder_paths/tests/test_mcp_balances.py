from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.mcp.resources.wallets import (
    get_wallet_activity,
    get_wallet_balances,
    get_wallet_balances_full,
)


@pytest.fixture
def mock_wallet():
    return {"label": "test", "address": "0x000000000000000000000000000000000000dEaD"}


@pytest.mark.asyncio
async def test_get_wallet_balances_returns_compact_summary(mock_wallet):
    fake_client = AsyncMock()
    fake_client.get_enriched_wallet_balances = AsyncMock(
        return_value={
            "balances": [
                {
                    "network": "base",
                    "balanceUSD": 1.5,
                    "symbol": "USDC",
                    "balance": "1.5",
                    "address": "0x1",
                },
                {
                    "network": "solana",
                    "balanceUSD": 999.0,
                    "symbol": "SOL",
                    "balance": "1",
                    "address": "0x2",
                },
                {
                    "network": "arbitrum",
                    "balanceUSD": 2.0,
                    "symbol": "ETH",
                    "balance": "0.001",
                    "address": "0x3",
                },
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
    balances_data = data["balance_summary"]
    assert balances_data["total_balance_usd"] == pytest.approx(3.5)
    assert balances_data["chain_breakdown"]["base"] == pytest.approx(1.5)
    assert balances_data["chain_breakdown"]["arbitrum"] == pytest.approx(2.0)
    assert "balances" not in data
    assert "balances" not in balances_data
    assert balances_data["position_count"] == 2
    assert len(balances_data["top_positions"]) == 2


@pytest.mark.asyncio
async def test_get_wallet_balances_full_keeps_filtered_positions(mock_wallet):
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
        result = await get_wallet_balances_full("test")

    data = json.loads(result)
    assert "error" not in data
    balances_data = data["balances"]
    assert balances_data["total_balance_usd"] == pytest.approx(3.5)
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


@pytest.mark.asyncio
async def test_get_wallet_activity_returns_compact_events(mock_wallet):
    fake_client = AsyncMock()
    fake_client.get_wallet_activity = AsyncMock(
        return_value={
            "activity": [
                {
                    "type": "swap",
                    "network": "base",
                    "amount": "1.0",
                    "symbol": "ETH",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "direction": "out",
                    "hash": "0xabc123",
                    "extra": "ignored",
                }
            ],
            "next_offset": "cursor",
        }
    )

    with (
        patch("wayfinder_paths.mcp.resources.wallets.BALANCE_CLIENT", fake_client),
        patch(
            "wayfinder_paths.mcp.resources.wallets.find_wallet_by_label",
            return_value=mock_wallet,
        ),
    ):
        result = await get_wallet_activity("test")

    data = json.loads(result)
    assert data["activity"][0]["type"] == "swap"
    assert data["activity"][0]["hash"] == "0xabc123"
    assert "extra" not in data["activity"][0]
