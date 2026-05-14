import inspect
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

import wayfinder_paths.adapters.polymarket_adapter.adapter as polymarket_adapter_module
from wayfinder_paths.adapters.polymarket_adapter.adapter import PolymarketAdapter
from wayfinder_paths.core.constants.polymarket import (
    POLYGON_P_USDC_PROXY_ADDRESS,
    POLYGON_USDC_ADDRESS,
    POLYGON_USDC_E_ADDRESS,
)


class TestPolymarketAdapter:
    @pytest.fixture
    async def adapter(self):
        adapter = PolymarketAdapter(config={})
        try:
            yield adapter
        finally:
            await adapter.close()

    def test_adapter_type(self, adapter):
        assert adapter.adapter_type == "POLYMARKET"

    def test_clob_client_python_v2_constructor_signature(self):
        params = inspect.signature(
            polymarket_adapter_module.ClobClient.__init__
        ).parameters

        assert list(params)[:3] == ["self", "host", "chain_id"]
        assert "chain" not in params
        assert "chainId" not in params
        assert "tickSizeTtlMs" not in params
        assert "geoBlockToken" not in params

    @pytest.mark.asyncio
    async def test_clob_client_is_configured_for_deposit_wallet(self, monkeypatch):
        calls = []

        class FakeClobClient:
            def __init__(self, *args, **kwargs):
                calls.append((args, kwargs))

        async def sign_hash_callback(_payload):
            return b""

        monkeypatch.setattr(polymarket_adapter_module, "ClobClient", FakeClobClient)
        adapter = PolymarketAdapter(
            config={},
            wallet_address="0x000000000000000000000000000000000000dEaD",
            sign_hash_callback=sign_hash_callback,
        )
        try:
            client = adapter.clob_client
            assert isinstance(client, FakeClobClient)
        finally:
            await adapter.close()

        assert len(calls) == 1
        args, kwargs = calls[0]
        assert args == ("https://clob.polymarket.com",)
        assert kwargs["chain_id"] == 137
        assert "chain" not in kwargs
        assert "chainId" not in kwargs
        assert "key" not in kwargs
        assert (
            kwargs["signature_type"]
            == polymarket_adapter_module.SignatureTypeV2.POLY_1271
        )
        assert kwargs["funder"] == polymarket_adapter_module.derive_deposit_wallet(
            "0x000000000000000000000000000000000000dEaD"
        )
        assert (
            kwargs["address_override"] == "0x000000000000000000000000000000000000dEaD"
        )
        assert kwargs["sign_callback_override"] is sign_hash_callback

    @pytest.mark.asyncio
    async def test_limit_order_uses_v2_order_args_without_legacy_fee_fields(
        self, adapter
    ):
        adapter.wallet_address = "0x000000000000000000000000000000000000dEaD"
        adapter.ensure_trading_setup = AsyncMock(return_value=(True, {}))
        adapter.ensure_api_creds = AsyncMock(return_value=(True, {}))

        class FakeClobClient:
            def __init__(self):
                self.created_order = None

            async def create_order(self, order_args):
                self.created_order = order_args
                return {"signed": True}

            def post_order(self, order, order_type, post_only):
                return {
                    "order": order,
                    "order_type": order_type,
                    "post_only": post_only,
                }

        fake_clob_client = FakeClobClient()
        adapter._clob_client = fake_clob_client

        ok, response = await adapter.place_limit_order(
            token_id="123",
            side="BUY",
            price=0.5,
            size=10.0,
            post_only=True,
        )

        assert ok is True
        assert response["order_type"] == "GTC"
        assert response["post_only"] is True
        order_args = fake_clob_client.created_order
        assert isinstance(order_args, polymarket_adapter_module.OrderArgsV2)
        assert (
            order_args.builder_code == polymarket_adapter_module.POLYMARKET_BUILDER_CODE
        )
        assert not hasattr(order_args, "fee_rate_bps")
        assert not hasattr(order_args, "feeRateBps")
        assert not hasattr(order_args, "nonce")
        assert not hasattr(order_args, "taker")

    @pytest.mark.asyncio
    async def test_market_order_uses_v2_order_args_without_manual_fee_fields(
        self, adapter
    ):
        adapter.wallet_address = "0x000000000000000000000000000000000000dEaD"
        adapter.ensure_trading_setup = AsyncMock(return_value=(True, {}))
        adapter.ensure_api_creds = AsyncMock(return_value=(True, {}))
        adapter.quote_market_order = AsyncMock(
            return_value=(
                True,
                {
                    "fully_fillable": True,
                    "worst_price": 0.55,
                    "book_meta": {"tick_size": "0.01"},
                },
            )
        )

        class FakeClobClient:
            def __init__(self):
                self.created_order = None

            async def create_market_order(self, order_args):
                self.created_order = order_args
                return {"signed": True}

            def post_order(self, order, order_type, post_only):
                return {
                    "order": order,
                    "order_type": order_type,
                    "post_only": post_only,
                }

        fake_clob_client = FakeClobClient()
        adapter._clob_client = fake_clob_client

        ok, response = await adapter.place_market_order(
            token_id="123",
            side="BUY",
            amount=10.0,
        )

        assert ok is True
        assert response["order_type"] == "FOK"
        assert response["post_only"] is False
        # 0.55 * (1 + 2/100) = 0.561 → ceil to next 0.01 tick = 0.57
        assert response["price_cap"] == pytest.approx(0.57)
        assert response["max_slippage_pct"] == 2.0
        order_args = fake_clob_client.created_order
        assert isinstance(order_args, polymarket_adapter_module.MarketOrderArgs)
        assert order_args.price == pytest.approx(0.57)
        assert (
            order_args.builder_code == polymarket_adapter_module.POLYMARKET_BUILDER_CODE
        )
        assert order_args.user_usdc_balance == 0
        assert not hasattr(order_args, "fee_rate_bps")
        assert not hasattr(order_args, "feeRateBps")
        assert not hasattr(order_args, "nonce")
        assert not hasattr(order_args, "taker")

    @pytest.mark.asyncio
    async def test_cancel_order_uses_v2_cancel_order(self, adapter):
        adapter.ensure_api_creds = AsyncMock(return_value=(True, {}))

        class FakeClobClient:
            def __init__(self):
                self.payload = None

            def cancel_order(self, payload):
                self.payload = payload
                return {"canceled": True}

        fake_clob_client = FakeClobClient()
        adapter._clob_client = fake_clob_client

        ok, response = await adapter.cancel_order(order_id="order-123")

        assert ok is True
        assert response == {"canceled": True}
        assert isinstance(
            fake_clob_client.payload, polymarket_adapter_module.OrderPayload
        )
        assert fake_clob_client.payload.orderID == "order-123"

    @pytest.mark.asyncio
    async def test_list_open_orders_uses_v2_get_open_orders(self, adapter):
        adapter.ensure_api_creds = AsyncMock(return_value=(True, {}))

        class FakeClobClient:
            def __init__(self):
                self.params = None

            def get_open_orders(self, params):
                self.params = params
                return [{"id": "open-1"}]

        fake_clob_client = FakeClobClient()
        adapter._clob_client = fake_clob_client

        ok, response = await adapter.list_open_orders(token_id="tok-123")

        assert ok is True
        assert response == [{"id": "open-1"}]
        assert isinstance(
            fake_clob_client.params, polymarket_adapter_module.OpenOrderParams
        )
        assert fake_clob_client.params.asset_id == "tok-123"

    @pytest.mark.asyncio
    async def test_list_markets(self, adapter, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = [
            {
                "slug": "test-market",
                "outcomes": '["Yes","No"]',
                "outcomePrices": "[0.5,0.5]",
                "clobTokenIds": '["tok1","tok2"]',
            }
        ]

        async def mock_get(*_args, **_kwargs):
            return mock_resp

        monkeypatch.setattr(adapter._gamma_http, "get", mock_get)
        ok, data = await adapter.list_markets(limit=1)
        assert ok is True
        assert isinstance(data, list)
        assert data[0]["slug"] == "test-market"

    @pytest.mark.asyncio
    async def test_public_search(self, adapter, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"events": [], "pagination": {}}

        async def mock_get(*_args, **_kwargs):
            return mock_resp

        monkeypatch.setattr(adapter._gamma_http, "get", mock_get)
        ok, data = await adapter.public_search(q="bitcoin", limit_per_type=1)
        assert ok is True
        assert isinstance(data, dict)
        assert "events" in data

    @pytest.mark.asyncio
    async def test_get_price(self, adapter, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"price": "0.5"}

        async def mock_get(*_args, **_kwargs):
            return mock_resp

        monkeypatch.setattr(adapter._clob_http, "get", mock_get)
        ok, data = await adapter.get_price(token_id="123", side="BUY")
        assert ok is True
        assert data["price"] == "0.5"

    @pytest.mark.asyncio
    async def test_quote_market_order_buy_uses_weighted_average(
        self, adapter, monkeypatch
    ):
        async def mock_get_order_book(*_args, **_kwargs):
            return True, {
                "market": "m1",
                "asset_id": "123",
                "timestamp": "1",
                "hash": "hash",
                "tick_size": "0.01",
                "min_order_size": "1",
                "neg_risk": False,
                "last_trade_price": "0.41",
                "asks": [
                    {"price": "0.50", "size": "100"},
                    {"price": "0.40", "size": "100"},
                ],
                "bids": [],
            }

        monkeypatch.setattr(adapter, "get_order_book", mock_get_order_book)

        ok, quote = await adapter.quote_market_order(
            token_id="123",
            side="BUY",
            amount=60.0,
        )

        assert ok is True
        assert quote["amount_kind"] == "usdc"
        assert quote["fully_fillable"] is True
        assert quote["best_price"] == pytest.approx(0.4)
        assert quote["worst_price"] == pytest.approx(0.5)
        assert quote["shares"] == pytest.approx(140.0)
        assert quote["notional_usdc"] == pytest.approx(60.0)
        assert quote["average_price"] == pytest.approx(60.0 / 140.0)
        assert quote["price_impact_bps"] == pytest.approx(
            (((60.0 / 140.0) - 0.4) / 0.4) * 10_000.0
        )
        assert quote["levels_consumed"] == 2
        assert quote["fills"] == [
            {"price": 0.4, "shares": 100.0, "notional_usdc": 40.0},
            {"price": 0.5, "shares": 40.0, "notional_usdc": 20.0},
        ]
        assert quote["book_meta"]["asset_id"] == "123"

    @pytest.mark.asyncio
    async def test_quote_market_order_sell_uses_weighted_average(
        self, adapter, monkeypatch
    ):
        async def mock_get_order_book(*_args, **_kwargs):
            return True, {
                "asset_id": "123",
                "bids": [
                    {"price": "0.50", "size": "100"},
                    {"price": "0.55", "size": "40"},
                ],
                "asks": [],
            }

        monkeypatch.setattr(adapter, "get_order_book", mock_get_order_book)

        ok, quote = await adapter.quote_market_order(
            token_id="123",
            side="SELL",
            amount=60.0,
        )

        assert ok is True
        assert quote["amount_kind"] == "shares"
        assert quote["fully_fillable"] is True
        assert quote["best_price"] == pytest.approx(0.55)
        assert quote["worst_price"] == pytest.approx(0.5)
        assert quote["shares"] == pytest.approx(60.0)
        assert quote["notional_usdc"] == pytest.approx(32.0)
        assert quote["average_price"] == pytest.approx(32.0 / 60.0)
        assert quote["price_impact_bps"] == pytest.approx(
            ((0.55 - (32.0 / 60.0)) / 0.55) * 10_000.0
        )
        assert quote["fills"] == [
            {"price": 0.55, "shares": 40.0, "notional_usdc": 22.0},
            {"price": 0.5, "shares": 20.0, "notional_usdc": 10.0},
        ]

    @pytest.mark.asyncio
    async def test_quote_market_order_handles_insufficient_depth(
        self, adapter, monkeypatch
    ):
        async def mock_get_order_book(*_args, **_kwargs):
            return True, {
                "asks": [
                    {"price": "0.40", "size": "10"},
                    {"price": "bad", "size": "20"},
                ],
                "bids": [],
            }

        monkeypatch.setattr(adapter, "get_order_book", mock_get_order_book)

        ok, quote = await adapter.quote_market_order(
            token_id="123",
            side="BUY",
            amount=10.0,
        )

        assert ok is True
        assert quote["fully_fillable"] is False
        assert quote["filled_amount"] == pytest.approx(4.0)
        assert quote["unfilled_amount"] == pytest.approx(6.0)
        assert quote["shares"] == pytest.approx(10.0)
        assert quote["levels_consumed"] == 1

    @pytest.mark.asyncio
    async def test_quote_market_order_handles_empty_book(self, adapter, monkeypatch):
        async def mock_get_order_book(*_args, **_kwargs):
            return True, {"asks": [], "bids": []}

        monkeypatch.setattr(adapter, "get_order_book", mock_get_order_book)

        ok, quote = await adapter.quote_market_order(
            token_id="123",
            side="BUY",
            amount=10.0,
        )

        assert ok is True
        assert quote["fully_fillable"] is False
        assert quote["filled_amount"] == pytest.approx(0.0)
        assert quote["unfilled_amount"] == pytest.approx(10.0)
        assert quote["best_price"] is None
        assert quote["average_price"] is None
        assert quote["levels_consumed"] == 0

    @pytest.mark.asyncio
    async def test_quote_prediction_resolves_market_slug(self, adapter, monkeypatch):
        async def mock_get_market_by_slug(*_args, **_kwargs):
            return True, {
                "slug": "test-market",
                "outcomes": ["Yes", "No"],
                "clobTokenIds": ["tok_yes", "tok_no"],
            }

        async def mock_get_order_book(*_args, **_kwargs):
            return True, {
                "asks": [{"price": "0.25", "size": "40"}],
                "bids": [],
            }

        monkeypatch.setattr(adapter, "get_market_by_slug", mock_get_market_by_slug)
        monkeypatch.setattr(adapter, "get_order_book", mock_get_order_book)

        ok, quote = await adapter.quote_prediction(
            market_slug="test-market",
            outcome="YES",
            side="BUY",
            amount=5.0,
        )

        assert ok is True
        assert quote["token_id"] == "tok_yes"
        assert quote["shares"] == pytest.approx(20.0)

    @pytest.mark.asyncio
    async def test_get_positions(self, adapter, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = []

        async def mock_get(*_args, **_kwargs):
            return mock_resp

        monkeypatch.setattr(adapter._data_http, "get", mock_get)
        ok, data = await adapter.get_positions(
            user="0x" + "11" * 20,
            limit=1,
        )
        assert ok is True
        assert data == []

    @pytest.mark.asyncio
    async def test_get_full_user_state_includes_positions_orders_and_pnl(
        self, adapter, monkeypatch
    ):
        sample_positions = [
            {
                "initialValue": 10,
                "currentValue": 12,
                "cashPnl": 2,
                "realizedPnl": 0.5,
                "redeemable": True,
                "mergeable": False,
                "negativeRisk": False,
            },
            {
                "initialValue": 5,
                "currentValue": 4,
                "cashPnl": -1,
                "realizedPnl": -0.1,
                "redeemable": False,
                "mergeable": True,
                "negativeRisk": True,
            },
        ]

        async def mock_get_positions(*_args, **_kwargs):
            return True, sample_positions

        async def mock_list_open_orders(*_args, **_kwargs):
            return True, [{"id": "order_1"}]

        mock_contract = MagicMock()
        mock_contract.functions.balanceOf.return_value = MagicMock(
            call=AsyncMock(side_effect=[7_890_000, 1_230_000, 4_560_000])
        )
        mock_web3 = MagicMock()
        mock_web3.eth.contract.return_value = mock_contract

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        monkeypatch.setattr(adapter, "get_positions", mock_get_positions)
        monkeypatch.setattr(adapter, "list_open_orders", mock_list_open_orders)
        monkeypatch.setattr(
            polymarket_adapter_module, "web3_from_chain_id", mock_web3_ctx
        )

        adapter.wallet_address = "0x" + "11" * 20
        account = adapter.deposit_wallet_address()

        ok, state = await adapter.get_full_user_state(account=account)
        assert ok is True
        assert state["protocol"] == "polymarket"
        assert state["positionsSummary"]["count"] == 2
        assert state["positionsSummary"]["redeemableCount"] == 1
        assert state["positionsSummary"]["mergeableCount"] == 1
        assert state["positionsSummary"]["negativeRiskCount"] == 1

        assert state["pnl"]["totalInitialValue"] == pytest.approx(15.0)
        assert state["pnl"]["totalCurrentValue"] == pytest.approx(16.0)
        assert state["pnl"]["totalCashPnl"] == pytest.approx(1.0)
        assert state["pnl"]["totalRealizedPnl"] == pytest.approx(0.4)
        assert state["pnl"]["totalUnrealizedPnl"] == pytest.approx(0.6)
        assert state["pnl"]["totalPercentPnl"] == pytest.approx((1.0 / 15.0) * 100.0)

        assert state["openOrders"] == [{"id": "order_1"}]
        assert state["orders"] == [{"id": "order_1"}]

        assert state["pusd_balance"] == pytest.approx(7.89)
        assert state["usdc_e_balance"] == pytest.approx(1.23)
        assert state["usdc_balance"] == pytest.approx(4.56)
        assert state["balances"]["pusd"]["address"] == POLYGON_P_USDC_PROXY_ADDRESS
        assert state["balances"]["pusd"]["amount_base_units"] == 7_890_000
        assert state["balances"]["usdc_e"]["amount_base_units"] == 1_230_000
        assert state["balances"]["usdc"]["amount_base_units"] == 4_560_000

    @pytest.mark.asyncio
    async def test_get_full_user_state_drops_zero_value_redeemable_losers(
        self, adapter, monkeypatch
    ):
        sample_positions = [
            {
                "initialValue": 10,
                "currentValue": 12,
                "cashPnl": 2,
                "realizedPnl": 0,
                "curPrice": 0.6,
                "redeemable": False,
            },
            {
                "initialValue": 8,
                "currentValue": 0,
                "cashPnl": -8,
                "realizedPnl": 0,
                "curPrice": 0,
                "redeemable": True,
            },
        ]

        async def mock_get_positions(*_args, **_kwargs):
            return True, sample_positions

        mock_contract = MagicMock()
        mock_contract.functions.balanceOf.return_value = MagicMock(
            call=AsyncMock(side_effect=[0, 0, 0])
        )
        mock_web3 = MagicMock()
        mock_web3.eth.contract.return_value = mock_contract

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        monkeypatch.setattr(adapter, "get_positions", mock_get_positions)
        monkeypatch.setattr(
            polymarket_adapter_module, "web3_from_chain_id", mock_web3_ctx
        )

        adapter.wallet_address = "0x" + "11" * 20
        account = adapter.deposit_wallet_address()

        ok, state = await adapter.get_full_user_state(
            account=account, include_orders=False
        )
        assert ok is True
        assert len(state["positions"]) == 1
        assert state["positions"][0]["redeemable"] is False
        assert state["positionsSummary"]["count"] == 1
        assert state["pnl"]["totalInitialValue"] == pytest.approx(10.0)
        assert state["pnl"]["totalCurrentValue"] == pytest.approx(12.0)

    @pytest.mark.asyncio
    async def test_bridge_deposit_prefers_brap_swap(self, adapter, monkeypatch):
        from_address = "0x000000000000000000000000000000000000dEaD"

        async def sign_cb(_tx: dict) -> bytes:
            return b""

        mock_web3 = MagicMock()

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        monkeypatch.setattr(adapter, "_require_signer", lambda: (from_address, sign_cb))
        monkeypatch.setattr(
            polymarket_adapter_module, "web3_from_chain_id", mock_web3_ctx
        )
        # get_token_balance is called three times: initial source-token check,
        # then USDC.e before/after leg 1 to compute the on-chain delta.
        monkeypatch.setattr(
            polymarket_adapter_module,
            "get_token_balance",
            AsyncMock(side_effect=[2_000_000, 50_000, 1_049_000]),
        )

        brap_swap = AsyncMock(
            side_effect=[
                (
                    True,
                    {
                        "from_amount": 1_000_000,
                        "to_amount": 999_000,
                        "tx_hash": "0xswap",
                        "ledger_record": {},
                    },
                ),
                (
                    True,
                    {
                        "from_amount": 999_000,
                        "to_amount": 998_500,
                        "tx_hash": "0xwrap",
                        "ledger_record": {},
                    },
                ),
            ]
        )
        monkeypatch.setattr(adapter, "_brap_swap_polygon", brap_swap)

        ok, res = await adapter.bridge_deposit(
            from_chain_id=137,
            from_token_address=POLYGON_USDC_ADDRESS,
            amount=1.0,
            recipient_address=from_address,
            token_decimals=6,
        )
        assert ok is True
        assert isinstance(res, dict)
        assert res["method"] == "brap_then_wrap"
        assert res["tx_hash"] == "0xwrap"
        assert res["from_token_address"].lower() == POLYGON_USDC_ADDRESS.lower()
        assert res["to_token_address"].lower() == POLYGON_P_USDC_PROXY_ADDRESS.lower()
        assert res["from_amount"] == 1_000_000
        assert res["to_amount"] == 998_500
        assert res["swap_tx_hash"] == "0xswap"
        assert res["wrap_tx_hash"] == "0xwrap"
        assert res["swap"]["tx_hash"] == "0xswap"
        assert res["wrap"]["tx_hash"] == "0xwrap"

        assert brap_swap.await_count == 2
        first_call_kwargs = brap_swap.await_args_list[0].kwargs
        assert first_call_kwargs["from_token_address"] == POLYGON_USDC_ADDRESS
        assert first_call_kwargs["to_token_address"] == POLYGON_USDC_E_ADDRESS
        assert first_call_kwargs["amount_base_unit"] == 1_000_000
        second_call_kwargs = brap_swap.await_args_list[1].kwargs
        assert second_call_kwargs["from_token_address"] == POLYGON_USDC_E_ADDRESS
        assert second_call_kwargs["to_token_address"] == POLYGON_P_USDC_PROXY_ADDRESS
        # Leg 2 must use the on-chain USDC.e delta (1_049_000 - 50_000),
        # not the leg-1 quote's optimistic `to_amount`.
        assert second_call_kwargs["amount_base_unit"] == 999_000
        assert second_call_kwargs["recipient_address"] == from_address

    @pytest.mark.asyncio
    async def test_bridge_deposit_wraps_usdce_to_pusd(self, adapter, monkeypatch):
        from_address = "0x000000000000000000000000000000000000dEaD"
        recipient_address = "0x000000000000000000000000000000000000bEEF"

        async def sign_cb(_tx: dict) -> bytes:
            return b""

        mock_web3 = MagicMock()

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        monkeypatch.setattr(adapter, "_require_signer", lambda: (from_address, sign_cb))
        monkeypatch.setattr(
            polymarket_adapter_module, "web3_from_chain_id", mock_web3_ctx
        )
        monkeypatch.setattr(
            polymarket_adapter_module,
            "get_token_balance",
            AsyncMock(return_value=2_000_000),
        )
        brap_swap = AsyncMock(
            return_value=(
                True,
                {
                    "from_amount": 1_000_000,
                    "to_amount": 1_000_000,
                    "tx_hash": "0xwrap",
                    "ledger_record": {},
                },
            )
        )
        monkeypatch.setattr(adapter, "_brap_swap_polygon", brap_swap)

        ok, res = await adapter.bridge_deposit(
            from_chain_id=137,
            from_token_address=POLYGON_USDC_E_ADDRESS,
            amount=1.0,
            recipient_address=recipient_address,
            token_decimals=6,
        )
        assert ok is True
        assert isinstance(res, dict)
        assert res["method"] == "pusd_wrap"
        assert res["tx_hash"] == "0xwrap"
        assert res["to_token_address"].lower() == POLYGON_P_USDC_PROXY_ADDRESS.lower()
        assert res["recipient_address"].lower() == recipient_address.lower()

        call_kwargs = brap_swap.await_args.kwargs
        assert call_kwargs["from_token_address"] == POLYGON_USDC_E_ADDRESS
        assert call_kwargs["to_token_address"] == POLYGON_P_USDC_PROXY_ADDRESS
        assert call_kwargs["amount_base_unit"] == 1_000_000
        assert call_kwargs["recipient_address"].lower() == recipient_address.lower()

    @pytest.mark.asyncio
    async def test_bridge_deposit_falls_back_to_polymarket_bridge(
        self, adapter, monkeypatch
    ):
        from_address = "0x000000000000000000000000000000000000dEaD"

        async def sign_cb(_tx: dict) -> bytes:
            return b""

        monkeypatch.setattr(adapter, "_require_signer", lambda: (from_address, sign_cb))
        monkeypatch.setattr(
            polymarket_adapter_module,
            "get_token_balance",
            AsyncMock(return_value=2_000_000),
        )
        monkeypatch.setattr(
            adapter,
            "_brap_swap_polygon",
            AsyncMock(return_value=(False, "no route")),
        )
        monkeypatch.setattr(
            adapter,
            "bridge_deposit_addresses",
            AsyncMock(
                return_value=(
                    True,
                    {"address": {"evm": "0x2222222222222222222222222222222222222222"}},
                )
            ),
        )
        monkeypatch.setattr(
            polymarket_adapter_module,
            "build_send_transaction",
            AsyncMock(
                return_value={
                    "to": "0x2222222222222222222222222222222222222222",
                    "from": from_address,
                    "data": "0x",
                    "chainId": 137,
                }
            ),
        )
        monkeypatch.setattr(
            polymarket_adapter_module,
            "send_transaction",
            AsyncMock(return_value="0xtransfer"),
        )

        ok, res = await adapter.bridge_deposit(
            from_chain_id=137,
            from_token_address=POLYGON_USDC_ADDRESS,
            amount=1.0,
            recipient_address=from_address,
            token_decimals=6,
        )
        # Polygon USDC path now returns the BRAP swap failure directly rather
        # than falling through to the async bridge.
        assert ok is False
        assert "no route" in str(res)

    @pytest.mark.asyncio
    async def test_bridge_deposit_polymarket_bridge_supports_non_polygon_from_chain(
        self, adapter, monkeypatch
    ):
        from_address = "0x000000000000000000000000000000000000dEaD"

        async def sign_cb(_tx: dict) -> bytes:
            return b""

        monkeypatch.setattr(adapter, "_require_signer", lambda: (from_address, sign_cb))
        monkeypatch.setattr(
            polymarket_adapter_module,
            "get_token_balance",
            AsyncMock(return_value=2_000_000),
        )
        monkeypatch.setattr(
            adapter,
            "bridge_deposit_addresses",
            AsyncMock(
                return_value=(
                    True,
                    {"address": {"evm": "0x2222222222222222222222222222222222222222"}},
                )
            ),
        )
        build_send = AsyncMock(
            return_value={
                "to": "0x2222222222222222222222222222222222222222",
                "from": from_address,
                "data": "0x",
                "chainId": 42161,
            }
        )
        monkeypatch.setattr(
            polymarket_adapter_module, "build_send_transaction", build_send
        )
        monkeypatch.setattr(
            polymarket_adapter_module,
            "send_transaction",
            AsyncMock(return_value="0xtransfer"),
        )

        ok, res = await adapter.bridge_deposit(
            from_chain_id=42161,
            from_token_address=POLYGON_USDC_ADDRESS,
            amount=1.0,
            recipient_address=from_address,
            token_decimals=6,
        )
        assert ok is True
        assert isinstance(res, dict)
        assert res["method"] == "polymarket_bridge"
        assert build_send.await_args.kwargs["chain_id"] == 42161

    @pytest.mark.asyncio
    async def test_preflight_redeem_prefers_zero_parent_without_log_scan(
        self, adapter, monkeypatch
    ):
        condition_id = "0x" + "11" * 32
        holder = "0x" + "22" * 20

        monkeypatch.setattr(
            adapter, "_outcome_index_sets", AsyncMock(return_value=[1, 2])
        )

        mock_ctf = MagicMock()
        mock_ctf.functions.getCollectionId.return_value = MagicMock(
            call=AsyncMock(side_effect=[b"\x01" * 32, b"\x02" * 32])
        )
        mock_ctf.functions.getPositionId.return_value = MagicMock(
            call=AsyncMock(side_effect=[100, 200])
        )
        mock_ctf.functions.balanceOf.return_value = MagicMock(
            call=AsyncMock(side_effect=[10, 0])
        )
        mock_web3 = MagicMock()
        mock_web3.eth.contract.return_value = mock_ctf

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        parent_scan = AsyncMock(
            side_effect=ValueError({"code": -32005, "message": "too many"})
        )
        monkeypatch.setattr(adapter, "_find_parent_collection_id", parent_scan)
        monkeypatch.setattr(
            polymarket_adapter_module, "web3_from_chain_id", mock_web3_ctx
        )

        ok, path = await adapter.preflight_redeem(
            condition_id=condition_id, holder=holder
        )
        assert ok is True
        assert isinstance(path, dict)
        assert path["indexSets"] == [1]
        assert parent_scan.await_count == 0

    @pytest.mark.asyncio
    async def test_preflight_redeem_checks_pusd_candidate(self, adapter, monkeypatch):
        condition_id = "0x" + "11" * 32
        holder = "0x" + "22" * 20

        monkeypatch.setattr(
            adapter, "_outcome_index_sets", AsyncMock(return_value=[1, 2])
        )
        monkeypatch.setattr(
            adapter,
            "_find_parent_collection_id",
            AsyncMock(side_effect=ValueError({"code": -32005, "message": "too many"})),
        )

        helper = AsyncMock(
            side_effect=[
                [b"\x01" * 32, b"\x02" * 32],
                [100, 200],
                [0, 0],
                [b"\x03" * 32, b"\x04" * 32],
                [300, 400],
                [10, 0],
            ]
        )
        monkeypatch.setattr(
            polymarket_adapter_module,
            "read_only_calls_multicall_or_gather",
            helper,
        )

        mock_web3 = MagicMock()
        mock_web3.eth.contract.return_value = MagicMock()

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        monkeypatch.setattr(
            polymarket_adapter_module, "web3_from_chain_id", mock_web3_ctx
        )

        ok, path = await adapter.preflight_redeem(
            condition_id=condition_id, holder=holder
        )

        assert ok is True
        assert isinstance(path, dict)
        assert path["collateral"].lower() == POLYGON_P_USDC_PROXY_ADDRESS.lower()
        assert path["indexSets"] == [1]

    @pytest.mark.asyncio
    async def test_preflight_redeem_ignores_log_scan_errors(self, adapter, monkeypatch):
        condition_id = "0x" + "11" * 32
        holder = "0x" + "22" * 20

        monkeypatch.setattr(
            adapter, "_outcome_index_sets", AsyncMock(return_value=[1, 2])
        )

        mock_ctf = MagicMock()
        # 4 collaterals × 2 index_sets = 8 calls per function
        mock_ctf.functions.getCollectionId.return_value = MagicMock(
            call=AsyncMock(return_value=b"\x01" * 32)
        )
        mock_ctf.functions.getPositionId.return_value = MagicMock(
            call=AsyncMock(return_value=100)
        )
        mock_ctf.functions.balanceOf.return_value = MagicMock(
            call=AsyncMock(return_value=0)
        )
        mock_web3 = MagicMock()
        mock_web3.eth.contract.return_value = mock_ctf

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        monkeypatch.setattr(
            polymarket_adapter_module, "web3_from_chain_id", mock_web3_ctx
        )
        monkeypatch.setattr(
            adapter,
            "_find_parent_collection_id",
            AsyncMock(side_effect=ValueError({"code": -32005, "message": "too many"})),
        )

        ok, msg = await adapter.preflight_redeem(
            condition_id=condition_id, holder=holder
        )
        assert ok is False
        assert isinstance(msg, str)

    @pytest.mark.asyncio
    async def test_bridge_withdraw_prefers_brap_swap(self, adapter, monkeypatch):
        from_address = "0x000000000000000000000000000000000000dEaD"

        async def sign_cb(_tx: dict) -> bytes:
            return b""

        monkeypatch.setattr(adapter, "_require_signer", lambda: (from_address, sign_cb))
        # get_token_balance is called twice: USDC.e before/after the unwrap.
        monkeypatch.setattr(
            polymarket_adapter_module,
            "get_token_balance",
            AsyncMock(side_effect=[50_000, 1_050_000]),
        )

        brap_swap = AsyncMock(
            side_effect=[
                (
                    True,
                    {
                        "from_amount": 1_000_000,
                        "to_amount": 1_000_000,
                        "tx_hash": "0xunwrap",
                        "ledger_record": {},
                    },
                ),
                (
                    True,
                    {
                        "from_amount": 1_000_000,
                        "to_amount": 999_000,
                        "tx_hash": "0xswap",
                        "ledger_record": {},
                    },
                ),
            ]
        )
        monkeypatch.setattr(adapter, "_brap_swap_polygon", brap_swap)

        ok, res = await adapter.bridge_withdraw(
            amount_pusd=1.0,
            to_chain_id=137,
            to_token_address=POLYGON_USDC_ADDRESS,
            recipient_addr=from_address,
            token_decimals=6,
        )
        assert ok is True
        assert isinstance(res, dict)
        assert res["method"] == "unwrap_then_brap"
        assert res["tx_hash"] == "0xswap"
        assert res["from_token_address"].lower() == POLYGON_P_USDC_PROXY_ADDRESS.lower()
        assert res["to_token_address"].lower() == POLYGON_USDC_ADDRESS.lower()
        assert res["from_amount"] == 1_000_000
        assert res["to_amount"] == 999_000
        assert res["unwrap_tx_hash"] == "0xunwrap"
        assert res["swap_tx_hash"] == "0xswap"
        assert res["unwrap"]["tx_hash"] == "0xunwrap"
        assert res["swap"]["tx_hash"] == "0xswap"

        assert brap_swap.await_count == 2
        first_call_kwargs = brap_swap.await_args_list[0].kwargs
        assert first_call_kwargs["from_token_address"] == POLYGON_P_USDC_PROXY_ADDRESS
        assert first_call_kwargs["to_token_address"] == POLYGON_USDC_E_ADDRESS
        assert first_call_kwargs["amount_base_unit"] == 1_000_000
        second_call_kwargs = brap_swap.await_args_list[1].kwargs
        assert second_call_kwargs["from_token_address"] == POLYGON_USDC_E_ADDRESS
        assert second_call_kwargs["to_token_address"] == POLYGON_USDC_ADDRESS
        assert second_call_kwargs["amount_base_unit"] == 1_000_000

    @pytest.mark.asyncio
    async def test_bridge_withdraw_unwraps_to_polygon_usdce(self, adapter, monkeypatch):
        from_address = "0x000000000000000000000000000000000000dEaD"

        async def sign_cb(_tx: dict) -> bytes:
            return b""

        monkeypatch.setattr(adapter, "_require_signer", lambda: (from_address, sign_cb))
        monkeypatch.setattr(
            polymarket_adapter_module,
            "get_token_balance",
            AsyncMock(return_value=0),
        )
        brap_swap = AsyncMock(
            return_value=(
                True,
                {
                    "from_amount": 1_000_000,
                    "to_amount": 1_000_000,
                    "tx_hash": "0xunwrap",
                    "ledger_record": {},
                },
            )
        )
        monkeypatch.setattr(adapter, "_brap_swap_polygon", brap_swap)

        ok, res = await adapter.bridge_withdraw(
            amount_pusd=1.0,
            to_chain_id=137,
            to_token_address=POLYGON_USDC_E_ADDRESS,
            recipient_addr=from_address,
            token_decimals=6,
        )
        assert ok is True
        assert isinstance(res, dict)
        assert res["method"] == "pusd_unwrap"
        assert res["tx_hash"] == "0xunwrap"
        assert res["to_token_address"].lower() == POLYGON_USDC_E_ADDRESS.lower()

        call_kwargs = brap_swap.await_args.kwargs
        assert call_kwargs["from_token_address"] == POLYGON_P_USDC_PROXY_ADDRESS
        assert call_kwargs["to_token_address"] == POLYGON_USDC_E_ADDRESS
        assert call_kwargs["amount_base_unit"] == 1_000_000

    @pytest.mark.asyncio
    async def test_bridge_withdraw_falls_back_to_polymarket_bridge(
        self, adapter, monkeypatch
    ):
        from_address = "0x000000000000000000000000000000000000dEaD"
        recipient_addr = "0x000000000000000000000000000000000000bEEF"

        async def sign_cb(_tx: dict) -> bytes:
            return b""

        monkeypatch.setattr(adapter, "_require_signer", lambda: (from_address, sign_cb))
        monkeypatch.setattr(
            polymarket_adapter_module,
            "get_token_balance",
            AsyncMock(return_value=0),
        )
        monkeypatch.setattr(
            adapter,
            "_brap_swap_polygon",
            AsyncMock(
                return_value=(
                    True,
                    {
                        "from_amount": 1_000_000,
                        "to_amount": 1_000_000,
                        "tx_hash": "0xunwrap",
                        "ledger_record": {},
                    },
                )
            ),
        )
        monkeypatch.setattr(
            adapter,
            "bridge_withdraw_addresses",
            AsyncMock(
                return_value=(
                    True,
                    {"address": {"evm": "0x3333333333333333333333333333333333333333"}},
                )
            ),
        )
        monkeypatch.setattr(
            polymarket_adapter_module,
            "build_send_transaction",
            AsyncMock(
                return_value={
                    "to": "0x3333333333333333333333333333333333333333",
                    "from": from_address,
                    "data": "0x",
                    "chainId": 137,
                }
            ),
        )
        monkeypatch.setattr(
            polymarket_adapter_module,
            "send_transaction",
            AsyncMock(return_value="0xtransfer"),
        )

        # Cross-recipient withdrawal: pUSD unwraps to sender, then bridge handles
        # the cross-recipient transfer.
        ok, res = await adapter.bridge_withdraw(
            amount_pusd=1.0,
            to_chain_id=137,
            to_token_address=POLYGON_USDC_ADDRESS,
            recipient_addr=recipient_addr,
            token_decimals=6,
        )
        assert ok is True
        assert isinstance(res, dict)
        assert res["method"] == "polymarket_bridge"
        assert res["tx_hash"] == "0xtransfer"
        assert res["unwrap"]["tx_hash"] == "0xunwrap"
