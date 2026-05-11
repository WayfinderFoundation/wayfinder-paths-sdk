from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.adapters.hyperliquid_adapter import HyperliquidAdapter
from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_execute


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


class _FastOrderAdapter:
    def __init__(self):
        self.wallet_address = "0x000000000000000000000000000000000000bEEF"
        self.get_all_mid_prices_calls = 0
        self.get_max_builder_fee_calls = 0
        self.place_market_order_call: dict[str, Any] | None = None

    async def get_asset_id(self, asset_name: str):
        return 0 if asset_name == "BTC-USDC" else None

    def get_market_type(self, asset_name: str):
        return HyperliquidAdapter.get_market_type(asset_name)

    def get_mid_price_key(self, asset_name: str, asset_id: int):
        return HyperliquidAdapter.get_mid_price_key(asset_name, asset_id)

    async def get_all_mid_prices(self):
        self.get_all_mid_prices_calls += 1
        return True, {"BTC": 50000.0}

    async def get_max_builder_fee(self, user: str, builder: str):
        self.get_max_builder_fee_calls += 1
        return True, 30

    async def approve_builder_fee(self, builder: str, max_fee_rate: str, address: str):
        raise AssertionError("builder fee should already be approved")

    def get_valid_order_size(self, asset_id: int, size: float):
        return size

    async def place_market_order(self, *args: Any, **kwargs: Any):
        self.place_market_order_call = {"args": args, "kwargs": kwargs}
        return True, {"status": "ok"}


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
async def test_hyperliquid_execute_reuses_mid_price_and_builder_check(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("WAYFINDER_MCP_STATE_PATH", str(tmp_path / "mcp.sqlite3"))
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path / "runs"))

    adapter = _FastOrderAdapter()
    with (
        patch("wayfinder_paths.mcp.tools.hyperliquid.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.get_adapter",
            new=AsyncMock(return_value=adapter),
        ),
    ):
        out = await hyperliquid_execute(
            "place_order",
            wallet_label="main",
            asset_name="BTC-USDC",
            order_type="market",
            is_buy=False,
            usd_amount=100,
            usd_amount_kind="notional",
            slippage=0.01,
        )

    assert out["ok"] is True
    result = out["result"]
    assert adapter.get_all_mid_prices_calls == 1
    assert adapter.get_max_builder_fee_calls == 1
    assert adapter.place_market_order_call is not None
    assert adapter.place_market_order_call["args"][:5] == (
        0,
        False,
        0.01,
        pytest.approx(0.002),
        adapter.wallet_address,
    )
    assert adapter.place_market_order_call["kwargs"]["mid_price"] == 50000.0
    assert adapter.place_market_order_call["kwargs"]["builder_fee_preapproved"] is True
    assert result["order"]["sizing"]["price_used"] == 50000.0
    assert result["effects"][0]["label"] == "get_max_builder_fee"
    assert result["effects"][1]["label"] == "place_market_order"
