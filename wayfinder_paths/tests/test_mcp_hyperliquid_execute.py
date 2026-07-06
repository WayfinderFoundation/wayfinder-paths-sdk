from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from hyperliquid.utils.types import OUTCOME_ASSET_OFFSET

from wayfinder_paths.adapters.hyperliquid_adapter import HyperliquidAdapter
from wayfinder_paths.mcp.tools.hyperliquid import (
    hyperliquid_deposit_usdc,
    hyperliquid_get_state,
    hyperliquid_get_trade_asset,
    hyperliquid_place_limit_order,
    hyperliquid_place_market_order,
    hyperliquid_withdraw_usdc,
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


_FAKE_ASSET_IDS = {"BTC-USDC": 0, "xyz:SP500": 110000}


class _FakeExecutionAdapter:
    def __init__(
        self,
        *,
        user_state: dict[str, Any] | None = None,
        active_asset_data: dict[str, Any] | None = None,
        filled_size: str = "2.09",
        fill_price: str = "100",
        spot_state: dict[str, Any] | None = None,
        frontend_open_orders: list[dict[str, Any]] | None = None,
    ) -> None:
        self.frontend_open_orders = frontend_open_orders or []
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
        self.meta_and_asset_ctxs = [
            {
                "universe": [
                    {
                        "name": "BTC",
                        "szDecimals": 4,
                        "maxLeverage": 25,
                        "marginTableId": 55,
                    }
                ]
            },
            [
                {
                    "funding": "0.00001",
                    "openInterest": "123.45",
                    "dayNtlVlm": "98765.43",
                    "midPx": "100.1",
                    "oraclePx": "100.2",
                    "premium": "0.0002",
                    "impactPxs": ["99.9", "100.3"],
                }
            ],
        ]
        self.all_perp_metas = [
            {
                "collateralToken": 0,
                "universe": [
                    {
                        "name": "BTC",
                        "szDecimals": 4,
                        "maxLeverage": 25,
                        "marginTableId": 55,
                    }
                ],
            }
        ]
        self.spot_meta = {
            "tokens": [
                {
                    "index": 0,
                    "name": "USDC",
                    "fullName": None,
                    "tokenId": "0xusdc",
                    "evmContract": {"address": "0xusdc_evm"},
                },
                {
                    "index": 360,
                    "name": "USDH",
                    "fullName": "USDH",
                    "tokenId": "0xusdh",
                    "evmContract": {"address": "0xusdh_evm"},
                },
            ]
        }
        self.filled_size = filled_size
        self.fill_price = fill_price
        self.spot_state = spot_state or {
            "balances": [
                {
                    "coin": "USDC",
                    "token": 0,
                    "total": "21.50",
                    "hold": "20.57",
                    "entryNtl": "0.0",
                },
                {
                    "coin": "+41",
                    "token": 41,
                    "total": "2",
                    "hold": "0",
                    "entryNtl": "1.0",
                },
            ],
            "tokenToAvailableAfterMaintenance": [[0, "19.94"]],
        }

    def get_market_type(self, asset_name: str) -> str:
        return HyperliquidAdapter.get_market_type(asset_name)

    def active_asset_data_coin(self, asset_name: str) -> str:
        return HyperliquidAdapter.active_asset_data_coin(asset_name)

    def get_mid_price_key(self, asset_name: str, asset_id: int) -> list[str]:
        return HyperliquidAdapter.get_mid_price_key(asset_name, asset_id)

    async def get_asset_id(self, asset_name: str) -> int | None:
        if asset_name.startswith("#") and asset_name[1:].isdigit():
            return OUTCOME_ASSET_OFFSET + int(asset_name[1:])
        return _FAKE_ASSET_IDS.get(asset_name)

    async def get_user_state(self, _address: str):
        return True, self.user_state

    async def get_spot_user_state(self, _address: str):
        return True, self.spot_state

    async def get_user_abstraction(self, _address: str):
        return True, "unifiedAccount"

    async def get_frontend_open_orders(self, _address: str):
        return True, self.frontend_open_orders

    async def get_dex_collateral_mapping(self) -> dict[str, str]:
        return {"": "USDC", "xyz": "USDC"}

    async def get_active_asset_data(self, _address: str, _asset_name: str):
        return True, self.active_asset_data

    async def get_meta_and_asset_ctxs(self):
        return True, self.meta_and_asset_ctxs

    async def get_all_perp_metas(self):
        return True, self.all_perp_metas

    async def get_spot_meta(self):
        return True, self.spot_meta

    async def get_max_builder_fee(self, *, user: str, builder: str):
        return True, 100

    async def approve_builder_fee(self, **_kwargs):
        return True, {"status": "ok"}

    async def get_all_mid_prices(self):
        return True, {"BTC": 100.0}

    def get_valid_order_size(self, _asset_id: int, size: float) -> float:
        return float(size)

    def get_valid_order_price(self, _asset_id: int, price: float) -> float:
        return float(price)

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
async def test_hyperliquid_withdraw_usdc(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WAYFINDER_MCP_STATE_PATH", str(tmp_path / "mcp.sqlite3"))
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path / "runs"))

    wallet = {
        "address": "0x000000000000000000000000000000000000dEaD",
        "private_key_hex": "0x" + "11" * 32,
    }

    ensure_mock = AsyncMock(return_value=(True, "Unified account already enabled"))
    with (
        patch(
            "wayfinder_paths.core.utils.wallets.find_wallet_by_label",
            return_value=wallet,
        ),
        patch("wayfinder_paths.mcp.tools.hyperliquid.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.HyperliquidAdapter.unify_if_split_account",
            new=ensure_mock,
        ),
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.HyperliquidAdapter.withdraw",
            new=AsyncMock(return_value=(True, {"status": "ok"})),
        ),
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.HyperliquidAdapter.wait_for_withdrawal",
            new=AsyncMock(return_value=(True, {"status": "ok"})),
        ),
    ):
        out1 = await hyperliquid_withdraw_usdc(wallet_label="main", amount_usdc=10)
        assert out1["ok"] is True
        assert out1["result"]["status"] == "confirmed"
        labels = [e["label"] for e in out1["result"]["effects"]]
        # Unified conversion must run before the withdraw so split-mode
        # perp balances become withdrawable.
        assert labels == ["ensure_unified", "withdraw", "wait_for_withdrawal"]
        ensure_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_hyperliquid_withdraw_usdc_ensure_unified_is_advisory(
    tmp_path: Path, monkeypatch
):
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
            "wayfinder_paths.mcp.tools.hyperliquid.HyperliquidAdapter.unify_if_split_account",
            new=AsyncMock(return_value=(False, "Failed to enable unified account")),
        ),
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.HyperliquidAdapter.withdraw",
            new=AsyncMock(return_value=(True, {"status": "ok"})),
        ),
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.HyperliquidAdapter.wait_for_withdrawal",
            new=AsyncMock(return_value=(True, {"status": "ok"})),
        ),
    ):
        out = await hyperliquid_withdraw_usdc(wallet_label="main", amount_usdc=10)

    # A conversion hiccup must not report a successful withdraw as failed.
    assert out["result"]["status"] == "confirmed"


def _deposit_patches(wallet, *, wait_result, ensure_mock):
    return (
        patch(
            "wayfinder_paths.core.utils.wallets.find_wallet_by_label",
            return_value=wallet,
        ),
        patch("wayfinder_paths.mcp.tools.hyperliquid.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.build_send_transaction",
            new=AsyncMock(return_value={"to": "0xbridge"}),
        ),
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.send_transaction",
            new=AsyncMock(return_value="0x" + "ab" * 32),
        ),
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.HyperliquidAdapter.wait_for_deposit",
            new=AsyncMock(return_value=wait_result),
        ),
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.HyperliquidAdapter.unify_if_split_account",
            new=ensure_mock,
        ),
    )


@pytest.mark.asyncio
async def test_hyperliquid_deposit_usdc_confirms_and_unifies(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("WAYFINDER_MCP_STATE_PATH", str(tmp_path / "mcp.sqlite3"))
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path / "runs"))

    wallet = {
        "address": "0x000000000000000000000000000000000000dEaD",
        "private_key_hex": "0x" + "11" * 32,
    }
    ensure_mock = AsyncMock(return_value=(True, "Unified account enabled"))

    with ExitStack() as stack:
        for p in _deposit_patches(
            wallet, wait_result=(True, 60.0), ensure_mock=ensure_mock
        ):
            stack.enter_context(p)
        out = await hyperliquid_deposit_usdc(wallet_label="main", amount_usdc=60)

    assert out["ok"] is True
    assert out["result"]["status"] == "confirmed"
    labels = [e["label"] for e in out["result"]["effects"]]
    assert labels == ["deposit", "wait_for_credit", "ensure_unified"]
    ensure_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_hyperliquid_deposit_usdc_unconfirmed_when_credit_not_observed(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("WAYFINDER_MCP_STATE_PATH", str(tmp_path / "mcp.sqlite3"))
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path / "runs"))

    wallet = {
        "address": "0x000000000000000000000000000000000000dEaD",
        "private_key_hex": "0x" + "11" * 32,
    }
    ensure_mock = AsyncMock()

    with ExitStack() as stack:
        for p in _deposit_patches(
            wallet, wait_result=(False, 0.0), ensure_mock=ensure_mock
        ):
            stack.enter_context(p)
        out = await hyperliquid_deposit_usdc(wallet_label="main", amount_usdc=60)

    assert out["ok"] is True
    # Bridge tx succeeded but the credit wasn't observed: NOT a failure.
    assert out["result"]["status"] == "unconfirmed"
    assert "still in flight" in out["result"]["note"]
    ensure_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_hyperliquid_deposit_usdc_ensure_unified_is_advisory(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("WAYFINDER_MCP_STATE_PATH", str(tmp_path / "mcp.sqlite3"))
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path / "runs"))

    wallet = {
        "address": "0x000000000000000000000000000000000000dEaD",
        "private_key_hex": "0x" + "11" * 32,
    }

    with ExitStack() as stack:
        for p in _deposit_patches(
            wallet,
            wait_result=(True, 60.0),
            ensure_mock=AsyncMock(
                return_value=(False, "Failed to enable unified account")
            ),
        ):
            stack.enter_context(p)
        out = await hyperliquid_deposit_usdc(wallet_label="main", amount_usdc=60)

    # The deposit itself succeeded; a conversion hiccup must not flip status.
    assert out["result"]["status"] == "confirmed"


@pytest.mark.asyncio
async def test_hyperliquid_get_state_returns_compact_account_state():
    stop_loss = {
        "coin": "HYPE",
        "oid": 42,
        "side": "A",
        "sz": "196.28",
        "limitPx": "58.0",
        "triggerPx": "60.0",
        "isTrigger": True,
        "orderType": "Stop Market",
        "isPositionTpsl": True,
        "reduceOnly": True,
    }
    resting_limit = {
        "coin": "BTC",
        "oid": 43,
        "side": "B",
        "sz": "0.001",
        "limitPx": "55000",
        "isTrigger": False,
        "orderType": "Limit",
        "reduceOnly": False,
    }
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
        },
        frontend_open_orders=[stop_loss, resting_limit],
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
        out = await hyperliquid_get_state(label="main")

    assert out["ok"] is True
    result = out["result"]
    assert "trade_context" not in result
    assert "account_collateral" not in result
    assert result["account_abstraction"]["state"] == "unifiedAccount"
    assert result["perp"]["state"]["assetPositions"][0]["position"]["coin"] == "BTC"
    assert [bal["coin"] for bal in result["spot"]["state"]["balances"]] == ["USDC"]
    # Trigger (TP/SL) orders and resting limits surface directly in state —
    # agents must not need a second call to discover them.
    assert result["open_orders"]["success"] is True
    assert result["open_orders"]["orders"] == [stop_loss, resting_limit]
    assert result["outcomes"]["positions"] == [
        {
            "coin": "+41",
            "outcome_id": 4,
            "side": 1,
            "total": "2",
            "hold": "0",
            "entryNtl": "1.0",
        }
    ]


@pytest.mark.asyncio
async def test_hyperliquid_get_trade_asset_uses_active_asset_available_to_trade():
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
        out = await hyperliquid_get_trade_asset(label="main", asset_name="BTC-USDC")

    assert out["ok"] is True
    result = out["result"]
    assert result["long"]["available_margin_usd"] == 12.34
    assert result["short"]["available_margin_usd"] == 56.78
    assert result["long"]["max_order_notional_usd"] == pytest.approx(12.0)
    assert result["position"]["side"] == "short"
    assert result["position"]["margin_mode"] == "cross"
    assert result["market_type"] == "perp"
    assert result["max_leverage"] == 25
    assert result["compatible_margin_modes"] == ["cross", "isolated"]
    assert result["market"]["size_decimals"] == 4
    assert result["market"]["margin_table_id"] == 55
    assert result["market"]["funding_rate_hourly"] == pytest.approx(0.00001)
    assert result["market"]["funding_apr"] == pytest.approx(0.0876)
    assert result["market"]["open_interest"] == pytest.approx(123.45)
    assert result["market"]["day_notional_volume_usd"] == pytest.approx(98765.43)
    assert result["collateral"]["token_index"] == 0
    assert result["collateral"]["symbol"] == "USDC"
    assert result["collateral"]["dex"] == {
        "index": 0,
        "name": "",
        "kind": "validator",
    }
    assert result["market"]["collateral"]["symbol"] == "USDC"


@pytest.mark.asyncio
async def test_hyperliquid_get_trade_asset_reports_hip3_collateral_token():
    fake = _FakeExecutionAdapter()
    fake.all_perp_metas = [
        {"collateralToken": 0, "universe": []},
        {
            "collateralToken": 360,
            "universe": [
                {
                    "name": "flx:NVDA",
                    "szDecimals": 2,
                    "maxLeverage": 10,
                    "marginTableId": 10,
                }
            ],
        },
    ]
    fake.meta_and_asset_ctxs = [
        {"universe": [{"name": "flx:NVDA"}]},
        [{"funding": "0.0"}],
    ]

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
        out = await hyperliquid_get_trade_asset(label="main", asset_name="flx:NVDA")

    assert out["ok"] is True
    result = out["result"]
    assert result["market_type"] == "hip3"
    assert result["collateral"]["token_index"] == 360
    assert result["collateral"]["symbol"] == "USDH"
    assert result["collateral"]["dex"] == {
        "index": 1,
        "name": "flx",
        "kind": "hip3",
    }
    assert "activeAssetData.availableToTrade" in result["collateral"]["balance_source"]


@pytest.mark.asyncio
async def test_hyperliquid_get_trade_asset_reports_isolated_only_metadata():
    fake = _FakeExecutionAdapter()
    isolated_market = {
        "name": "BTC",
        "szDecimals": 0,
        "maxLeverage": 3,
        "marginMode": "noCross",
        "onlyIsolated": True,
        "marginTableId": 53,
        "isDelisted": True,
    }
    fake.all_perp_metas = [
        {
            "collateralToken": 0,
            "universe": [isolated_market],
        }
    ]
    fake.meta_and_asset_ctxs = [
        {"universe": [isolated_market]},
        [{}],
    ]

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
        out = await hyperliquid_get_trade_asset(label="main", asset_name="BTC-USDC")

    assert out["ok"] is True
    result = out["result"]
    assert result["max_leverage"] == 3
    assert result["compatible_margin_modes"] == ["isolated"]
    assert result["margin_mode_restriction"] == "noCross"
    assert result["can_remove_isolated_margin"] is True
    assert result["market"]["is_delisted"] is True


@pytest.mark.asyncio
async def test_hyperliquid_get_trade_asset_derives_capacity_from_max_trade_size_fallback():
    fake = _FakeExecutionAdapter(
        active_asset_data={
            "leverage": {"type": "cross", "value": 5},
            "markPx": "100",
            "maxTradeSzs": ["0.2", "0.4"],
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
        out = await hyperliquid_get_trade_asset(label="main", asset_name="BTC-USDC")

    assert out["ok"] is True
    long = out["result"]["long"]
    assert "capacity_source" not in long
    assert long["available_margin_usd"] == pytest.approx(4.0)
    assert long["max_order_notional_usd"] == pytest.approx(20.0)
    assert long["max_base_size"] == pytest.approx(0.2)


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
async def test_hyperliquid_market_order_rejects_when_core_perp_has_zero_usdc():
    fake = _FakeExecutionAdapter(
        active_asset_data={
            "availableToTrade": ["0", "0"],
            "leverage": {"type": "cross", "value": 5},
            "markPx": "100",
            "maxTradeSzs": ["0", "0"],
        },
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
    assert out["error"]["code"] == "insufficient_collateral"
    assert "USDC" in out["error"]["message"]
    assert out["error"]["details"]["collateral_coin"] == "USDC"


@pytest.mark.asyncio
async def test_hyperliquid_limit_order_rejects_when_hip3_dex_has_zero_collateral():
    fake = _FakeExecutionAdapter(
        active_asset_data={
            "availableToTrade": ["0", "0"],
            "leverage": {"type": "cross", "value": 5},
            "markPx": "5000",
            "maxTradeSzs": ["0", "0"],
        },
    )

    with (
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid._make_hl_adapter",
            new=AsyncMock(return_value=(fake, "0x1234")),
        ),
        patch("wayfinder_paths.mcp.tools.hyperliquid._annotate_hl_profile"),
    ):
        out = await hyperliquid_place_limit_order(
            wallet_label="main",
            asset_name="xyz:SP500",
            is_buy=True,
            price=5000.0,
            usd_amount=100,
        )

    assert out["ok"] is False
    assert out["error"]["code"] == "insufficient_collateral"
    assert "USDC" in out["error"]["message"]
    assert out["error"]["details"] == {
        "asset_name": "xyz:SP500",
        "market_type": "hip3",
        "collateral_coin": "USDC",
    }


@pytest.mark.asyncio
async def test_hyperliquid_market_order_reports_material_underfill_as_partial():
    fake = _FakeExecutionAdapter(
        active_asset_data={
            "availableToTrade": ["5000", "5000"],
            "leverage": {"type": "cross", "value": 5},
            "markPx": "100",
            "maxTradeSzs": ["200", "200"],
        },
        filled_size="2.09",
        fill_price="100",
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
            usd_amount=10_000,
        )

    assert out["ok"] is True
    result = out["result"]
    assert result["status"] == "partial"
    assert result["order"]["fill"]["filled_notional_usd"] == 209.0
    assert result["order"]["fill"]["fill_ratio"] == pytest.approx(0.0209)


@pytest.mark.asyncio
async def test_hyperliquid_market_order_rejects_notional_over_available_margin():
    fake = _FakeExecutionAdapter(
        active_asset_data={
            "availableToTrade": ["1", "1"],
            "leverage": {"type": "cross", "value": 5},
            "markPx": "100",
            "maxTradeSzs": ["100", "100"],
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
            usd_amount=10,
        )

    assert out["ok"] is False
    assert out["error"]["code"] == "insufficient_hyperliquid_margin"
    details = out["error"]["details"]
    assert details["available_to_trade_margin_usd"] == 1.0
    assert details["available_margin_usd"] == 1.0
    assert details["required_margin_usd"] == 2.0
    assert details["max_order_notional_usd"] == 5.0


@pytest.mark.asyncio
async def test_hyperliquid_market_order_uses_max_trade_size_capacity_fallback():
    fake = _FakeExecutionAdapter(
        active_asset_data={
            "leverage": {"type": "cross", "value": 5},
            "markPx": "100",
            "maxTradeSzs": ["0.2", "0.2"],
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
            usd_amount=25,
        )

    assert out["ok"] is False
    assert out["error"]["code"] == "insufficient_hyperliquid_margin"
    details = out["error"]["details"]
    assert details["available_to_trade_margin_usd"] == pytest.approx(4.0)
    assert details["max_order_notional_usd"] == pytest.approx(20.0)
    assert details["max_trade_size"] == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_hyperliquid_reduce_only_rejects_size_above_live_position():
    fake = _FakeExecutionAdapter(
        user_state={
            "assetPositions": [
                {"position": {"coin": "BTC", "szi": "-0.25", "leverage": {}}}
            ]
        },
        active_asset_data={
            "availableToTrade": ["100", "100"],
            "leverage": {"type": "cross", "value": 5},
            "markPx": "100",
            "maxTradeSzs": ["100", "100"],
        },
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
            size=0.5,
            reduce_only=True,
        )

    assert out["ok"] is False
    assert out["error"]["code"] == "reduce_only_size_exceeds_position"
    assert out["error"]["details"]["closeable_size"] == 0.25
