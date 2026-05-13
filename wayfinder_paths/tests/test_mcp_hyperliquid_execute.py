from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.adapters.hyperliquid_adapter import HyperliquidAdapter
from wayfinder_paths.mcp.tools.hyperliquid import (
    hyperliquid_execute,
    hyperliquid_get_state,
)


class _StubAdapter(HyperliquidAdapter):
    """Adapter shell with stubbed coin_to_asset / spot_assets — keeps
    `resolve_coin` reachable without hitting the live HL info endpoint."""

    def __init__(self, coin_to_asset, spot_assets):
        # Skip parent __init__ — resolve_coin only needs the two attrs below.
        self._coin_to_asset = coin_to_asset
        self._spot_assets = spot_assets

    @property
    def coin_to_asset(self):
        return self._coin_to_asset

    async def get_spot_assets(self):
        return True, self._spot_assets


@pytest.mark.asyncio
async def test_get_asset_id_perp():
    adapter = _StubAdapter({"BTC": 0, "ETH": 1}, {})
    assert await adapter.get_asset_id("BTC-USDC") == 0


@pytest.mark.asyncio
async def test_get_asset_id_hip3_perp():
    adapter = _StubAdapter({"xyz:SP500": 110000}, {})
    assert await adapter.get_asset_id("xyz:SP500") == 110000


@pytest.mark.asyncio
async def test_get_asset_id_spot_pair():
    adapter = _StubAdapter({}, {"BTC/USDC": 10107, "USDC/USDH": 10211})
    assert await adapter.get_asset_id("USDC/USDH") == 10211


@pytest.mark.asyncio
async def test_get_asset_id_outcome():
    from hyperliquid.utils.types import OUTCOME_ASSET_OFFSET

    adapter = _StubAdapter({}, {})
    assert await adapter.get_asset_id("#41") == OUTCOME_ASSET_OFFSET + 41


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "asset_name",
    [
        "BTC",  # bare ticker
        "btc-usdc",  # case mismatch
        "BTC-usdc",  # partial case mismatch
        "BTC/usdc",  # spot case mismatch
        "BTC-USDT",  # wrong quote
        " BTC-USDC ",  # whitespace not tolerated
        "#",  # missing encoding
        "#abc",  # non-numeric encoding
        "",  # empty
    ],
)
async def test_get_asset_id_returns_none_on_bad_input(asset_name):
    adapter = _StubAdapter({"BTC": 0}, {"BTC/USDC": 10107})
    assert await adapter.get_asset_id(asset_name) is None


@pytest.mark.asyncio
async def test_hyperliquid_execute_withdraw(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WAYFINDER_MCP_STATE_PATH", str(tmp_path / "mcp.sqlite3"))
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path / "runs"))

    wallet = {
        "address": "0x000000000000000000000000000000000000dEaD",
        "private_key_hex": "0x" + "11" * 32,
    }

    with (
        patch(
            "wayfinder_paths.core.utils.wallets.find_wallet_by_label",
            return_value=wallet,
        ),
        patch("wayfinder_paths.mcp.tools.hyperliquid.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.HyperliquidAdapter.withdraw",
            new=AsyncMock(return_value=(True, {"status": "ok"})),
        ),
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.HyperliquidAdapter.wait_for_withdrawal",
            new=AsyncMock(return_value=(True, {"status": "ok"})),
        ),
    ):
        out1 = await hyperliquid_execute(
            "withdraw", wallet_label="main", amount_usdc=10
        )
        assert out1["ok"] is True


@pytest.mark.asyncio
async def test_hyperliquid_get_state_marks_unified_spot_usdc_as_perp_collateral():
    address = "0x000000000000000000000000000000000000dEaD"
    perp_state = {
        "assetPositions": [],
        "crossMarginSummary": {
            "accountValue": "0.0",
            "totalRawUsd": "0.0",
            "totalMarginUsed": "0.0",
        },
        "withdrawable": "0.0",
    }
    spot_state = {
        "balances": [
            {"coin": "USDC", "total": "11.07", "hold": "1.00"},
            {"coin": "+41", "total": "3", "hold": "0", "entryNtl": "1.5"},
        ]
    }

    with (
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.resolve_wallet_address",
            new=AsyncMock(return_value=(address, {})),
        ),
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.HyperliquidAdapter.get_user_abstraction_state",
            new=AsyncMock(return_value=(True, "unifiedAccount")),
        ),
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.HyperliquidAdapter.get_user_state",
            new=AsyncMock(return_value=(True, perp_state)),
        ),
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.HyperliquidAdapter.get_spot_user_state",
            new=AsyncMock(return_value=(True, spot_state)),
        ),
    ):
        out = await hyperliquid_get_state("main")

    assert out["ok"] is True
    result = out["result"]
    assert result["account"]["mode"] == "unifiedAccount"
    assert result["perp_collateral"] == {
        "account_mode_success": True,
        "account_mode": "unifiedAccount",
        "spot_usdc_usable_for_perp_orders": True,
        "perp_balance_source": "spotClearinghouseState.balances[USDC]",
        "spot_usdc_total": 11.07,
        "spot_usdc_hold": 1.0,
        "spot_usdc_available": pytest.approx(10.07),
        "perp_account_value": 0.0,
        "perp_withdrawable": 0.0,
        "estimated_usdc_available_for_perp_orders": pytest.approx(10.07),
        "guidance": result["perp_collateral"]["guidance"],
    }
    assert "Do not treat" in result["perp_collateral"]["guidance"]
    assert result["spot"]["state"]["balances"] == [
        {"coin": "USDC", "total": "11.07", "hold": "1.00"}
    ]
    assert result["outcomes"]["positions"] == [
        {
            "coin": "+41",
            "outcome_id": 4,
            "side": 1,
            "total": "3",
            "hold": "0",
            "entryNtl": "1.5",
        }
    ]


@pytest.mark.asyncio
async def test_hyperliquid_get_state_keeps_standard_spot_usdc_separate_from_perps():
    address = "0x000000000000000000000000000000000000dEaD"
    perp_state = {
        "assetPositions": [],
        "crossMarginSummary": {"accountValue": "6.0", "totalRawUsd": "6.0"},
        "withdrawable": "5.5",
    }
    spot_state = {"balances": [{"coin": "USDC", "total": "11.07", "hold": "0"}]}

    with (
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.resolve_wallet_address",
            new=AsyncMock(return_value=(address, {})),
        ),
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.HyperliquidAdapter.get_user_abstraction_state",
            new=AsyncMock(return_value=(True, "standard")),
        ),
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.HyperliquidAdapter.get_user_state",
            new=AsyncMock(return_value=(True, perp_state)),
        ),
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.HyperliquidAdapter.get_spot_user_state",
            new=AsyncMock(return_value=(True, spot_state)),
        ),
    ):
        out = await hyperliquid_get_state("main")

    assert out["ok"] is True
    collateral = out["result"]["perp_collateral"]
    assert collateral["account_mode"] == "standard"
    assert collateral["spot_usdc_usable_for_perp_orders"] is False
    assert collateral["perp_balance_source"] == "clearinghouseState"
    assert collateral["estimated_usdc_available_for_perp_orders"] == 5.5
    assert "explicit user approval" in collateral["guidance"]
