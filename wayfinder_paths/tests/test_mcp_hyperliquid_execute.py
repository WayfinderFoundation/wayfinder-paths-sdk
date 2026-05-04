from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.core.constants.hyperliquid import HYPE_FEE_WALLET
from wayfinder_paths.mcp.tools.hyperliquid import (
    _resolve_builder_fee,
    _resolve_perp_asset_id,
    _resolve_spot_asset_id,
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


def test_resolve_perp_asset_id_accepts_coin_and_strips_perp_suffix():
    class StubAdapter:
        coin_to_asset = {"HYPE": 7}

    ok, res = _resolve_perp_asset_id(StubAdapter(), coin="HYPE-perp", asset_id=None)
    assert ok is True
    assert res == 7


class _SpotStubAdapter:
    def __init__(self, assets: dict[str, int]):
        self._assets = assets

    async def get_spot_assets(self):
        return True, self._assets


@pytest.mark.asyncio
async def test_resolve_spot_asset_id_accepts_explicit_pair():
    adapter = _SpotStubAdapter(
        {"BTC/USDC": 10142, "BTC/USDH": 10999, "USDH/USDC": 10230}
    )

    ok, res = await _resolve_spot_asset_id(adapter, coin="BTC/USDC")
    assert ok is True
    assert res == 10142

    ok, res = await _resolve_spot_asset_id(adapter, coin="BTC/USDH")
    assert ok is True
    assert res == 10999

    ok, res = await _resolve_spot_asset_id(adapter, coin="usdh/usdc")
    assert ok is True
    assert res == 10230


@pytest.mark.asyncio
async def test_resolve_spot_asset_id_rejects_bare_token():
    adapter = _SpotStubAdapter({"BTC/USDC": 10142, "BTC/USDH": 10999})

    ok, err = await _resolve_spot_asset_id(adapter, coin="BTC")
    assert ok is False
    assert err["code"] == "invalid_request"
    assert "BTC/USDC" in err["message"] or "full pair" in err["message"]


@pytest.mark.asyncio
async def test_resolve_spot_asset_id_accepts_asset_id():
    adapter = _SpotStubAdapter({})

    ok, res = await _resolve_spot_asset_id(adapter, coin=None, asset_id=10230)
    assert ok is True
    assert res == 10230


@pytest.mark.asyncio
async def test_resolve_spot_asset_id_rejects_perp_asset_id():
    adapter = _SpotStubAdapter({})

    ok, err = await _resolve_spot_asset_id(adapter, coin=None, asset_id=7)
    assert ok is False
    assert err["code"] == "invalid_request"
    assert ">= 10000" in err["message"]


@pytest.mark.asyncio
async def test_resolve_spot_asset_id_unknown_pair():
    adapter = _SpotStubAdapter({"BTC/USDC": 10142})

    ok, err = await _resolve_spot_asset_id(adapter, coin="DOGE/USDC")
    assert ok is False
    assert err["code"] == "not_found"
    assert "DOGE/USDC" in err["message"]


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
