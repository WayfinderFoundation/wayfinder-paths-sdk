from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.core.constants.hyperliquid import HYPE_FEE_WALLET
from wayfinder_paths.mcp.tools.hyperliquid import (
    _format_perp_market,
    _resolve_builder_fee,
    _resolve_coin,
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


def test_format_perp_market_appends_usdc_for_core_perp():
    assert _format_perp_market("BTC") == "BTC-USDC"
    assert _format_perp_market("xyz:SP500") == "xyz:SP500"


class _StubAdapter:
    def __init__(self, coin_to_asset, spot_assets):
        self.coin_to_asset = coin_to_asset
        self._spot_assets = spot_assets

    async def get_spot_assets(self):
        return True, self._spot_assets


@pytest.mark.asyncio
async def test_resolve_coin_perp():
    adapter = _StubAdapter({"BTC": 0, "ETH": 1}, {})
    ok, res = await _resolve_coin(adapter, coin="BTC-USDC")
    assert ok and res["market_type"] == "perp" and res["asset_id"] == 0
    assert res["coin_clean"] == "BTC"


@pytest.mark.asyncio
async def test_resolve_coin_hip3_perp():
    adapter = _StubAdapter({"xyz:SP500": 110000}, {})
    ok, res = await _resolve_coin(adapter, coin="xyz:SP500")
    assert ok and res["market_type"] == "perp" and res["asset_id"] == 110000
    assert res["coin_clean"] == "xyz:SP500"


@pytest.mark.asyncio
async def test_resolve_coin_spot_pair():
    adapter = _StubAdapter({}, {"BTC/USDC": 10107, "USDC/USDH": 10211})
    ok, res = await _resolve_coin(adapter, coin="USDC/USDH")
    assert ok and res["market_type"] == "spot" and res["asset_id"] == 10211
    assert res["coin_clean"] == "USDC/USDH"


@pytest.mark.asyncio
async def test_resolve_coin_outcome():
    adapter = _StubAdapter({}, {})
    ok, res = await _resolve_coin(adapter, coin="#41")
    assert ok and res["market_type"] == "outcome"
    assert res["outcome_id"] == 4 and res["side"] == 1


@pytest.mark.asyncio
async def test_resolve_coin_rejects_bare_ticker():
    adapter = _StubAdapter({"BTC": 0}, {})
    ok, res = await _resolve_coin(adapter, coin="BTC")
    assert ok is False
    assert res["code"] == "invalid_request"


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
    ):
        out1 = await hyperliquid_execute(
            "withdraw", wallet_label="main", amount_usdc=10
        )
        assert out1["ok"] is True
