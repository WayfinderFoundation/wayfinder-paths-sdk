from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.adapters.hyperliquid_adapter import HyperliquidAdapter
from wayfinder_paths.mcp.tools.hyperliquid import (
    hyperliquid_get_state,
    hyperliquid_place_market_order,
    hyperliquid_withdraw,
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


class _FakeExecutionAdapter:
    def __init__(
        self,
        *,
        user_state: dict[str, Any] | None = None,
        active_asset_data: dict[str, Any] | None = None,
        filled_size: str = "2.09",
        fill_price: str = "100",
    ) -> None:
        self.user_state = user_state or {
            "assetPositions": [],
            "marginSummary": {"accountValue": "20.56"},
        }
        self.active_asset_data = active_asset_data or {
            "availableToTrade": ["12.34", "56.78"],
            "leverage": {"type": "cross", "value": 5},
            "markPx": "100",
            "maxTradeSzs": ["0.12", "0.56"],
        }
        self.filled_size = filled_size
        self.fill_price = fill_price

    def get_market_type(self, asset_name: str) -> str:
        return HyperliquidAdapter.get_market_type(asset_name)

    def active_asset_data_coin(self, asset_name: str) -> str:
        return HyperliquidAdapter.active_asset_data_coin(asset_name)

    def get_mid_price_key(self, asset_name: str, asset_id: int) -> list[str]:
        return HyperliquidAdapter.get_mid_price_key(asset_name, asset_id)

    async def get_asset_id(self, asset_name: str) -> int | None:
        if asset_name == "BTC-USDC":
            return 0
        return None

    async def get_user_state(self, _address: str):
        return True, self.user_state

    async def get_spot_user_state(self, _address: str):
        return True, {"balances": []}

    async def get_active_asset_data(self, _address: str, _asset_name: str):
        return True, self.active_asset_data

    async def get_max_builder_fee(self, *, user: str, builder: str):
        return True, 100

    async def approve_builder_fee(self, **_kwargs):
        return True, {"status": "ok"}

    async def get_all_mid_prices(self):
        return True, {"BTC": 100.0}

    def get_valid_order_size(self, _asset_id: int, size: float) -> float:
        return float(size)

    def get_sz_decimals(self, _asset_id: int) -> int:
        return 4

    async def place_market_order(self, *_args, **_kwargs):
        return True, {
            "status": "ok",
            "response": {
                "data": {
                    "statuses": [
                        {
                            "filled": {
                                "totalSz": self.filled_size,
                                "avgPx": self.fill_price,
                            }
                        }
                    ]
                }
            },
        }


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
async def test_hyperliquid_withdraw(tmp_path: Path, monkeypatch):
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
        out1 = await hyperliquid_withdraw(wallet_label="main", amount_usdc=10)
        assert out1["ok"] is True


@pytest.mark.asyncio
async def test_hyperliquid_get_state_includes_active_asset_trade_context():
    fake = _FakeExecutionAdapter(
        user_state={
            "assetPositions": [
                {
                    "position": {
                        "coin": "BTC",
                        "szi": "-0.25",
                        "entryPx": "100",
                        "positionValue": "25",
                        "marginUsed": "5",
                        "leverage": {"type": "cross", "value": 5},
                    }
                }
            ]
        }
    )

    with (
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.resolve_wallet_address",
            new=AsyncMock(return_value=("0x1234", None)),
        ),
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.HyperliquidAdapter",
            return_value=fake,
        ),
    ):
        out = await hyperliquid_get_state(label="main", asset_name="BTC-USDC")

    assert out["ok"] is True
    context = out["result"]["trade_context"]
    assert context["available_to_trade_long_usd"] == 12.34
    assert context["available_to_trade_short_usd"] == 56.78
    assert context["position"]["side"] == "short"
    assert context["position"]["margin_mode"] == "cross"


@pytest.mark.asyncio
async def test_hyperliquid_market_order_requires_reduce_only_for_opposite_position():
    fake = _FakeExecutionAdapter(
        user_state={
            "assetPositions": [
                {"position": {"coin": "BTC", "szi": "-0.25", "leverage": {}}}
            ]
        }
    )

    with (
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid._make_hl_adapter",
            new=AsyncMock(return_value=(fake, "0x1234")),
        ),
        patch("wayfinder_paths.mcp.tools.hyperliquid._annotate_hl_profile"),
    ):
        out = await hyperliquid_place_market_order(
            wallet_label="main",
            asset_name="BTC-USDC",
            is_buy=True,
            usd_amount=20,
        )

    assert out["ok"] is False
    assert out["error"]["code"] == "reduce_only_required"


@pytest.mark.asyncio
async def test_hyperliquid_market_order_reports_material_underfill_as_partial():
    fake = _FakeExecutionAdapter(filled_size="2.09", fill_price="100")

    with (
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid._make_hl_adapter",
            new=AsyncMock(return_value=(fake, "0x1234")),
        ),
        patch("wayfinder_paths.mcp.tools.hyperliquid._annotate_hl_profile"),
    ):
        out = await hyperliquid_place_market_order(
            wallet_label="main",
            asset_name="BTC-USDC",
            is_buy=True,
            usd_amount=10_000,
        )

    assert out["ok"] is True
    result = out["result"]
    assert result["status"] == "partial"
    assert result["order"]["fill"]["filled_notional_usd"] == 209.0
    assert result["order"]["fill"]["fill_ratio"] == pytest.approx(0.0209)
