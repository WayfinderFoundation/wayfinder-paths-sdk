from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.adapters.hyperliquid_adapter import HyperliquidAdapter
from wayfinder_paths.core.constants.hyperliquid import HYPE_FEE_WALLET
from wayfinder_paths.mcp.tools.hyperliquid import (
    _resolve_builder_fee,
    hyperliquid_execute,
)


def test_resolve_builder_fee_rejects_wrong_builder_wallet():
    with pytest.raises(ValueError, match="config builder_fee\\.b must be"):
        _resolve_builder_fee(
            config={"builder_fee": {"b": "0x" + "00" * 20, "f": 10}},
            builder_fee_tenths_bp=None,
        )


def test_resolve_builder_fee_prefers_explicit_fee():
    fee = _resolve_builder_fee(config={}, builder_fee_tenths_bp=7)
    assert fee == {"b": HYPE_FEE_WALLET.lower(), "f": 7}


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
