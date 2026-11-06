import inspect
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from eth_utils.address import to_checksum_address

import wayfinder_paths.adapters.polymarket_adapter.adapter as polymarket_adapter_module
from wayfinder_paths.adapters.polymarket_adapter.adapter import (
    PolymarketAdapter,
    map_data_api_v2_row,
)
from wayfinder_paths.core.constants.polymarket import (
    POLYGON_P_USDC_PROXY_ADDRESS,
    POLYGON_USDC_ADDRESS,
    POLYGON_USDC_E_ADDRESS,
    POLYMARKET_DATA_BASE_URL,
    derive_legacy_deposit_wallet,
)
from wayfinder_paths.core.utils import polymarket_wallet


@pytest.fixture(autouse=True)
def _seed_deposit_wallet_cache():
    """deposit_wallet_address() resolves on-chain since the 2026-06-29 factory
    upgrade; seed the cache so unit tests never hit RPC. Seeding the legacy
    derivation keeps the historical expected values meaningful."""
    owners = ["0x000000000000000000000000000000000000dEaD", "0x" + "11" * 20]
    for owner in owners:
        polymarket_wallet._RESOLVED[to_checksum_address(owner)] = (
            derive_legacy_deposit_wallet(owner)
        )
    yield
    polymarket_wallet._RESOLVED.clear()


def _gamma_response(status_code: int, endpoint: str, text: str = "") -> httpx.Response:
    return httpx.Response(
        status_code,
        request=httpx.Request("GET", f"https://gamma-api.polymarket.com{endpoint}"),
        text=text,
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

    @pytest.mark.asyncio
    async def test_history_pages_explicit_windows_without_interval_or_boundary_duplicates(
        self, adapter, monkeypatch
    ):
        week = 7 * 86400
        calls = []

        async def get(path, *, params):
            calls.append(params)
            assert path == "/prices-history"
            assert "interval" not in params
            assert 0 < params["endTs"] - params["startTs"] <= week
            return httpx.Response(
                200,
                request=httpx.Request("GET", "https://example.test"),
                json={
                    "history": [
                        {"t": params["endTs"], "p": 0.5},
                        {"t": params["startTs"], "p": 0.4},
                    ]
                },
            )

        monkeypatch.setattr(adapter._clob_http, "get", get)
        success, result = await adapter.get_prices_history(
            token_id="123", start_ts=1, end_ts=2 * week + 2, fidelity=60
        )
        assert success
        assert len(calls) == 3
        assert [r["t"] for r in result["history"]] == [1, week + 1, 2 * week + 1]
        assert all(p["fidelity"] == 60 for p in calls)

    @pytest.mark.asyncio
    async def test_history_does_not_present_failed_pages_as_complete(
        self, adapter, monkeypatch
    ):
        monkeypatch.setattr(
            adapter._clob_http,
            "get",
            AsyncMock(
                return_value=httpx.Response(
                    503, request=httpx.Request("GET", "https://example.test")
                )
            ),
        )
        success, result = await adapter.get_prices_history(
            token_id="123", start_ts=1, end_ts=90 * 86400, interval=None
        )
        assert not success
        assert "503" in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize("end", [0, -1, 367 * 86400])
    async def test_history_rejects_invalid_or_unbounded_windows(self, adapter, end):
        success, result = await adapter.get_prices_history(
            token_id="123", start_ts=0, end_ts=end
        )
        assert not success
        assert "366 days" in result

    @pytest.mark.asyncio
    async def test_fund_deposit_wallet_deploys_before_transfer(
        self, adapter, monkeypatch
    ):
        """Deploy-first funding: the wallet must be deployed and code-verified
        BEFORE any pUSD transfer — transferring to a codeless counterfactual
        address is how the 2026-06-29 factory upgrade stranded funds."""
        adapter.wallet_address = "0x000000000000000000000000000000000000dEaD"
        adapter.sign_callback = AsyncMock()
        order: list[str] = []

        async def fake_ensure(deposit_wallet: str):
            order.append("deploy")
            return None

        async def fake_build(**_kwargs):
            order.append("build")
            return {"tx": 1}

        async def fake_send(_tx, _cb, confirmations=1):
            order.append("send")
            return "0xhash"

        monkeypatch.setattr(adapter, "_ensure_deposit_wallet_deployed", fake_ensure)
        monkeypatch.setattr(
            polymarket_adapter_module, "build_send_transaction", fake_build
        )
        monkeypatch.setattr(polymarket_adapter_module, "send_transaction", fake_send)

        ok, out = await adapter.fund_deposit_wallet(amount_raw=1_000_000)
        assert ok is True
        assert order == ["deploy", "build", "send"]
        assert out["tx_hash"] == "0xhash"

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
        assert kwargs["funder"] == derive_legacy_deposit_wallet(
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

    def test_normalize_market_accepts_json_strings_and_parsed_lists(self):
        parsed = PolymarketAdapter._normalize_market(
            {
                "slug": "parsed-market",
                "outcomes": ["Yes", "No"],
                "outcomePrices": [0.4, 0.6],
                "clobTokenIds": ["tok_yes", "tok_no"],
            }
        )
        encoded = PolymarketAdapter._normalize_market(
            {
                "slug": "encoded-market",
                "outcomes": '["Yes","No"]',
                "outcomePrices": "[0.4,0.6]",
                "clobTokenIds": '["tok_yes","tok_no"]',
            }
        )

        assert parsed["outcomes"] == ["Yes", "No"]
        assert parsed["outcomePrices"] == [0.4, 0.6]
        assert parsed["clobTokenIds"] == ["tok_yes", "tok_no"]
        assert encoded["outcomes"] == ["Yes", "No"]
        assert encoded["outcomePrices"] == [0.4, 0.6]
        assert encoded["clobTokenIds"] == ["tok_yes", "tok_no"]

    @pytest.mark.asyncio
    async def test_get_market_by_slug_returns_structured_event_slug_hint(
        self, adapter, monkeypatch
    ):
        endpoint = "/markets/slug/world-cup-winner"

        async def mock_get(*_args, **_kwargs):
            return _gamma_response(404, endpoint, "not found")

        monkeypatch.setattr(adapter._gamma_http, "get", mock_get)

        ok, error = await adapter.get_market_by_slug("world-cup-winner")

        assert ok is False
        assert isinstance(error, dict)
        assert error["code"] == "gamma_http_error"
        assert error["statusCode"] == 404
        assert error["endpoint"] == endpoint
        assert error["slug"] == "world-cup-winner"
        assert "get_event_by_slug" in error["hint"]

    @pytest.mark.asyncio
    async def test_get_event_by_slug_returns_structured_market_slug_hint(
        self, adapter, monkeypatch
    ):
        endpoint = "/events/slug/btc-above-100k"

        async def mock_get(*_args, **_kwargs):
            return _gamma_response(404, endpoint, "not found")

        monkeypatch.setattr(adapter._gamma_http, "get", mock_get)

        ok, error = await adapter.get_event_by_slug("btc-above-100k")

        assert ok is False
        assert isinstance(error, dict)
        assert error["code"] == "gamma_http_error"
        assert error["statusCode"] == 404
        assert error["endpoint"] == endpoint
        assert error["slug"] == "btc-above-100k"
        assert "get_market_by_slug" in error["hint"]

    @pytest.mark.asyncio
    async def test_list_markets_returns_structured_rate_limit_hint(
        self, adapter, monkeypatch
    ):
        async def mock_get(*_args, **_kwargs):
            return _gamma_response(429, "/markets", "rate limited")

        monkeypatch.setattr(adapter._gamma_http, "get", mock_get)

        ok, error = await adapter.list_markets(limit=1)

        assert ok is False
        assert isinstance(error, dict)
        assert error["code"] == "gamma_http_error"
        assert error["statusCode"] == 429
        assert error["endpoint"] == "/markets"
        assert "rate limited" in error["hint"]

    @pytest.mark.asyncio
    async def test_get_event_by_slug_normalizes_contained_markets(
        self, adapter, monkeypatch
    ):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "slug": "event",
            "markets": [
                {
                    "slug": "market",
                    "outcomes": '["Yes","No"]',
                    "outcomePrices": "[0.4,0.6]",
                    "clobTokenIds": '["tok_yes","tok_no"]',
                }
            ],
        }

        async def mock_get(*_args, **_kwargs):
            return mock_resp

        monkeypatch.setattr(adapter._gamma_http, "get", mock_get)

        ok, event = await adapter.get_event_by_slug("event")

        assert ok is True
        assert event["markets"][0]["outcomes"] == ["Yes", "No"]
        assert event["markets"][0]["clobTokenIds"] == ["tok_yes", "tok_no"]

    @pytest.mark.asyncio
    async def test_search_markets_delegates_to_polymarket_client(
        self, adapter, monkeypatch
    ):
        captured: dict = {}

        async def fake_search_markets(**kwargs):
            captured.update(kwargs)
            return [{"slug": "btc-updown-5m-1", "symbol": "Bitcoin Up or Down — 5m"}]

        monkeypatch.setattr(
            polymarket_adapter_module.POLYMARKET_CLIENT,
            "search_markets",
            fake_search_markets,
        )

        ok, rows = await adapter.search_markets(
            query="btc 5 min", limit=5, sort="trending", status="active"
        )
        assert ok is True
        assert isinstance(rows, list)
        assert rows[0]["slug"] == "btc-updown-5m-1"
        assert captured == {
            "query": "btc 5 min",
            "limit": 5,
            "sort": "trending",
            "status": "active",
        }

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

    async def _mock_data_api(self, adapter, pages, seen):
        """Serve `pages` (keyed by the request's cursor, None for page 1) from
        the real data base URL; record every request in `seen`."""

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            cursor = request.url.params.get("cursor")
            body = pages[cursor]
            if isinstance(body, httpx.Response):
                return body
            return httpx.Response(200, json=body)

        await adapter._data_http.aclose()
        adapter._data_http = httpx.AsyncClient(
            base_url=POLYMARKET_DATA_BASE_URL,
            transport=httpx.MockTransport(handler),
        )

    @staticmethod
    def _page(rows, next_cursor=None, has_more=None):
        return {
            "data": rows,
            "pagination": {
                "limit": len(rows),
                "offset": 0,
                "has_more": bool(next_cursor) if has_more is None else has_more,
                "next_cursor": next_cursor,
            },
        }

    def test_map_data_api_v2_position_row(self):
        row = {
            "proxy_wallet": "0xabc",
            "token_id": "111",
            "condition_id": "0xcond",
            "current_size": 11.1111,
            "avg_price": 0.0899,
            "entry_cost_usdc": 0.9999,
            "entry_fees_usdc": 0.0455,
            "total_cost_usdc": 1.0454,
            "current_price": 0.495,
            "current_value": 5.5,
            "total_size": 11.1111,
            "realized_pnl": 0.0,
            "unrealized_pnl": 4.5,
            "total_pnl": 4.5,
            "percent_pnl": 450.0,
            "percent_realized_pnl": 450.0,
            "status": "OPEN",
            "redeemable": False,
            "mergeable": False,
            "negative_risk": True,
            "event_id": "1138941",
            "event_slug": "ev",
            "outcome_index": 0,
            "opposite_outcome": "No",
            "opposite_token_id": "222",
            "end_date": "2026-12-07",
        }
        mapped = map_data_api_v2_row("positions", row)
        assert mapped == {
            "proxyWallet": "0xabc",
            "asset": "111",
            "conditionId": "0xcond",
            "size": 11.1111,
            "avgPrice": 0.0899,
            "initialValue": 0.9999,
            "entryFeesUsdc": 0.0455,
            "totalCostUsdc": 1.0454,
            "curPrice": 0.495,
            "currentValue": 5.5,
            "totalBought": 11.1111,
            "realizedPnl": 0.0,
            "unrealizedPnl": 4.5,
            "cashPnl": 4.5,
            "percentPnl": 450.0,
            "percentRealizedPnl": 450.0,
            "status": "OPEN",
            "redeemable": False,
            "mergeable": False,
            "negativeRisk": True,
            "eventId": "1138941",
            "eventSlug": "ev",
            "outcomeIndex": 0,
            "oppositeOutcome": "No",
            "oppositeAsset": "222",
            "endDate": "2026-12-07",
        }

    def test_map_data_api_v2_activity_and_trade_rows(self):
        row = {
            "proxy_wallet": "0xabc",
            "timestamp": 1793907570,
            "condition_id": "0xcond",
            "type": "TRADE",
            "size": 10.0,
            "usdc_size": 1.045,
            "transaction_hash": "0xtx",
            "price": 0.1,
            "token_id": "111",
            "side": "BUY",
            "outcome_index": 0,
            "event_slug": "ev",
            "profile_image": "",
            "profile_image_optimized": "",
        }
        expected = {
            "proxyWallet": "0xabc",
            "timestamp": 1793907570,
            "conditionId": "0xcond",
            "type": "TRADE",
            "size": 10.0,
            "usdcSize": 1.045,
            "transactionHash": "0xtx",
            "price": 0.1,
            "asset": "111",
            "side": "BUY",
            "outcomeIndex": 0,
            "eventSlug": "ev",
            "profileImage": "",
            "profileImageOptimized": "",
        }
        assert map_data_api_v2_row("activity", row) == expected
        trade = {k: v for k, v in row.items() if k != "usdc_size"}
        assert map_data_api_v2_row("trades", trade) == {
            k: v for k, v in expected.items() if k != "usdcSize"
        }

    def test_data_base_url_is_v2(self):
        assert POLYMARKET_DATA_BASE_URL.rstrip("/").endswith("/v2")

    @pytest.mark.asyncio
    async def test_get_positions_unwraps_v2_envelope(self, adapter):
        seen: list[httpx.Request] = []
        rows = [{"token_id": "1", "current_price": 0.5, "negative_risk": False}]
        await self._mock_data_api(adapter, {None: self._page(rows)}, seen)

        ok, data = await adapter.get_positions(
            user="0x" + "11" * 20, limit=1, market="0xcond", redeemable=None
        )

        assert ok is True
        assert data == [{"asset": "1", "curPrice": 0.5, "negativeRisk": False}]
        assert len(seen) == 1
        assert seen[0].url.path == "/v2/positions"
        params = dict(seen[0].url.params)
        assert params == {
            "limit": "1",
            "user": to_checksum_address("0x" + "11" * 20),
            "condition": "0xcond",
        }

    @pytest.mark.asyncio
    async def test_get_positions_rejects_non_envelope(self, adapter):
        await self._mock_data_api(adapter, {None: [{"asset": "1"}]}, [])
        ok, data = await adapter.get_positions(user="0x" + "11" * 20)
        assert ok is False
        assert "Unexpected /positions response" in data

    @pytest.mark.asyncio
    async def test_data_api_follows_cursor_pages(self, adapter):
        seen: list[httpx.Request] = []
        pages = {
            None: self._page([{"token_id": "1"}, {"token_id": "2"}], "c1"),
            "c1": self._page([{"token_id": "3"}, {"token_id": "4"}], "c2"),
            "c2": self._page([{"token_id": "5"}], None, has_more=False),
        }
        await self._mock_data_api(adapter, pages, seen)

        ok, data = await adapter.get_activity(
            user="0x" + "11" * 20, limit=10, page_size=2
        )

        assert ok is True
        assert [r["asset"] for r in data] == ["1", "2", "3", "4", "5"]
        assert [r.url.params.get("cursor") for r in seen] == [None, "c1", "c2"]
        assert all("offset" not in r.url.params for r in seen)
        assert all(r.url.params["limit"] == "2" for r in seen)

    @pytest.mark.asyncio
    async def test_data_api_stops_when_has_more_false(self, adapter):
        seen: list[httpx.Request] = []
        pages = {
            None: self._page([{"token_id": "1"}], "c1", has_more=False),
        }
        await self._mock_data_api(adapter, pages, seen)

        ok, data = await adapter.get_trades(limit=10, page_size=1)

        assert ok is True
        assert [r["asset"] for r in data] == ["1"]
        assert len(seen) == 1
        assert "user" not in seen[0].url.params

    @pytest.mark.asyncio
    async def test_data_api_repeated_cursor_is_an_error(self, adapter):
        seen: list[httpx.Request] = []
        pages = {
            None: self._page([{"token_id": "1"}], "c1"),
            "c1": self._page([{"token_id": "2"}], "c1"),
        }
        await self._mock_data_api(adapter, pages, seen)

        ok, data = await adapter.get_activity(
            user="0x" + "11" * 20, limit=10, page_size=1
        )

        assert ok is False
        assert "repeated cursor" in data
        assert len(seen) == 2

    @pytest.mark.asyncio
    async def test_data_api_offset_is_emulated_with_cursors(self, adapter):
        seen: list[httpx.Request] = []
        pages = {
            None: self._page([{"token_id": "1"}, {"token_id": "2"}], "c1"),
            "c1": self._page([{"token_id": "3"}, {"token_id": "4"}], "c2"),
            "c2": self._page([{"token_id": "5"}], None),
        }
        await self._mock_data_api(adapter, pages, seen)

        ok, data = await adapter.get_activity(
            user="0x" + "11" * 20, limit=2, offset=1, page_size=2
        )

        assert ok is True
        assert [r["asset"] for r in data] == ["2", "3"]
        # Stops as soon as offset + limit rows are in hand.
        assert len(seen) == 2

    @pytest.mark.asyncio
    async def test_data_api_page_cap_and_size_clamp(self, adapter):
        seen: list[httpx.Request] = []
        pages = {
            None: self._page([{"token_id": "1"}], "c1"),
            "c1": self._page([{"token_id": "2"}], "c2"),
            "c2": self._page([{"token_id": "3"}], "c3"),
        }
        await self._mock_data_api(adapter, pages, seen)

        ok, data = await adapter.get_positions(
            user="0x" + "11" * 20, limit=5000, max_pages=2
        )

        assert ok is True
        assert [r["asset"] for r in data] == ["1", "2"]
        assert len(seen) == 2
        assert seen[0].url.params["limit"] == "1000"

    @pytest.mark.asyncio
    async def test_data_api_retries_rate_limit(self, adapter, monkeypatch):
        sleeps: list[float] = []

        async def fake_sleep(s):
            sleeps.append(s)

        monkeypatch.setattr(polymarket_adapter_module.asyncio, "sleep", fake_sleep)
        responses = [
            httpx.Response(429, headers={"retry-after": "0.5"}, json={}),
            httpx.Response(200, json=self._page([{"token_id": "1"}])),
        ]
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return responses.pop(0)

        await adapter._data_http.aclose()
        adapter._data_http = httpx.AsyncClient(
            base_url=POLYMARKET_DATA_BASE_URL, transport=httpx.MockTransport(handler)
        )

        ok, data = await adapter.get_activity(user="0x" + "11" * 20, limit=1)

        assert ok is True
        assert [r["asset"] for r in data] == ["1"]
        assert len(seen) == 2
        assert sleeps == [0.5]

    @pytest.mark.asyncio
    async def test_data_api_http_error(self, adapter):
        err = httpx.Response(410, json={"error": "Data API v1 has been retired"})
        await self._mock_data_api(adapter, {None: err}, [])
        ok, data = await adapter.get_positions(user="0x" + "11" * 20)
        assert ok is False
        assert "410" in data

    @pytest.mark.asyncio
    async def test_get_full_user_state_pages_positions_by_cursor(
        self, adapter, monkeypatch
    ):
        seen: list[httpx.Request] = []
        pages = {
            None: self._page(
                [
                    {"token_id": "1", "redeemable": True, "current_price": 0},
                    {"token_id": "2", "entry_cost_usdc": 2, "total_pnl": 1},
                ],
                "c1",
            ),
            "c1": self._page([{"token_id": "3", "entry_cost_usdc": 3}], None),
        }
        await self._mock_data_api(adapter, pages, seen)

        @asynccontextmanager
        async def broken_web3(_chain_id):
            raise RuntimeError("no rpc in unit tests")
            yield

        monkeypatch.setattr(
            polymarket_adapter_module, "web3_from_chain_id", broken_web3
        )

        ok, state = await adapter.get_full_user_state(
            account="0x" + "22" * 20,
            include_orders=False,
            positions_limit=2,
            max_positions_pages=5,
        )

        assert ok is True
        # Lost redeemable rows (curPrice == 0) are still filtered out.
        assert [p["asset"] for p in state["positions"]] == ["2", "3"]
        assert state["pnl"]["totalInitialValue"] == pytest.approx(5.0)
        assert state["pnl"]["totalCashPnl"] == pytest.approx(1.0)
        assert [r.url.params.get("cursor") for r in seen] == [None, "c1"]
        assert all(r.url.params["limit"] == "2" for r in seen)

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


class TestNegRiskRedeemUnwrap:
    """2026-07-03 incident: neg-risk (WCOL-collateralized) redemptions left
    winnings stranded as WCOL because the unwrap leg was gated on a stale
    post-redeem balance read. The unwrap must ride in the SAME batch with a
    pre-computed amount."""

    DW = to_checksum_address("0x0ab274fBf20Ca7c688aDAEFC93040669B1C68fdC")
    COND = "0xb81d9a63be774f9c3db1b96af4d0e869128643a29c6eb922c0d8a20137880120"

    def test_compute_ctf_redeem_payout_matches_incident(self):
        # Argentina-win market: YES (indexSet 1) lost, NO (indexSet 2) won 1/1.
        payout = polymarket_adapter_module.compute_ctf_redeem_payout(
            [1, 2], [5_080_000, 10_200_000], [0, 1], 1
        )
        assert payout == 10_200_000

    def test_compute_ctf_redeem_payout_floors_per_index_set(self):
        # floor(3/2) + floor(5/2) = 1 + 2, not floor(8/2) = 4 — mirrors the
        # CTF's per-set integer division.
        payout = polymarket_adapter_module.compute_ctf_redeem_payout(
            [1, 2], [3, 5], [1, 1], 2
        )
        assert payout == 3

    @pytest.fixture
    async def adapter(self):
        adapter = PolymarketAdapter(config={})
        try:
            yield adapter
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_redeem_unwraps_wcol_in_same_batch(self, adapter, monkeypatch):
        from wayfinder_paths.core.constants.polymarket import (
            POLYGON_P_USDC_PROXY_ADDRESS,
            POLYMARKET_ADAPTER_COLLATERAL_ADDRESS,
            POLYMARKET_CONDITIONAL_TOKENS_ADDRESS,
        )

        wcol_before = 15_353_790
        monkeypatch.setattr(adapter, "deposit_wallet_address", lambda: self.DW)

        async def fake_preflight(**_kwargs):
            return True, {
                "collateral": POLYMARKET_ADAPTER_COLLATERAL_ADDRESS,
                "parentCollectionId": "0x" + "00" * 32,
                "conditionId": self.COND,
                "indexSets": [2],
                "balances": [10_200_000],
            }

        monkeypatch.setattr(adapter, "preflight_redeem", fake_preflight)

        pusd_reads = {"count": 0}

        async def fake_get_token_balance(token, _chain, _holder, **_kwargs):
            if token == POLYGON_P_USDC_PROXY_ADDRESS:
                pusd_reads["count"] += 1
                # First read = pre-redeem baseline; later poll reads show the
                # payout landed so the 5s poll exits immediately.
                return 100 if pusd_reads["count"] == 1 else 999_999_999
            if token == POLYMARKET_ADAPTER_COLLATERAL_ADDRESS:
                return wcol_before
            return 0

        monkeypatch.setattr(
            polymarket_adapter_module, "get_token_balance", fake_get_token_balance
        )

        unwrap_encodes: list[list] = []

        class FakeContract:
            def __init__(self, address):
                self.address = address

                class _Numerators:
                    def __call__(_s, _cond, index):
                        value = [0, 1][index]

                        class _Call:
                            async def call(_c):
                                return value

                        return _Call()

                class _Denominator:
                    def __call__(_s, _cond):
                        class _Call:
                            async def call(_c):
                                return 1

                        return _Call()

                class _Functions:
                    payoutNumerators = _Numerators()
                    payoutDenominator = _Denominator()

                self.functions = _Functions()

            def encode_abi(self, fn_name, args):
                if fn_name == "unwrap":
                    unwrap_encodes.append(list(args))
                return "0x" + fn_name.encode().hex()

        class FakeEth:
            def contract(self, *, address, abi):
                return FakeContract(address)

        class FakeWeb3:
            eth = FakeEth()

        @asynccontextmanager
        async def fake_web3(_chain_id):
            yield FakeWeb3()

        monkeypatch.setattr(polymarket_adapter_module, "web3_from_chain_id", fake_web3)

        batches: list[list[dict]] = []

        async def fake_submit(*, calls):
            batches.append(calls)
            return {"tx_hash": "0xatomic"}

        monkeypatch.setattr(adapter, "_submit_wallet_batch", fake_submit)

        ok, res = await adapter.redeem_positions(condition_id=self.COND)

        assert ok is True
        # ONE batch containing redeem + unwrap — never a raced second batch.
        assert len(batches) == 1
        assert len(batches[0]) == 2
        assert batches[0][0]["target"] == POLYMARKET_CONDITIONAL_TOKENS_ADDRESS
        assert batches[0][1]["target"] == POLYMARKET_ADAPTER_COLLATERAL_ADDRESS
        # unwrap(depositWallet, resident WCOL + exact CTF payout)
        assert unwrap_encodes == [[self.DW, wcol_before + 10_200_000]]
        assert res["unwrap_tx_hash"] == "0xatomic"

    @pytest.mark.asyncio
    async def test_redeem_sweeps_stranded_wcol_when_nothing_redeemable(
        self, adapter, monkeypatch
    ):
        """Re-running a redeem after a half-done one (positions burned, WCOL
        stranded) must recover the WCOL instead of failing preflight."""
        monkeypatch.setattr(adapter, "deposit_wallet_address", lambda: self.DW)

        async def fake_preflight(**_kwargs):
            return (
                False,
                "No redeemable balance detected for the provided condition_id.",
            )

        monkeypatch.setattr(adapter, "preflight_redeem", fake_preflight)

        swept = {"called": False}

        async def fake_sweep():
            swept["called"] = True
            return True, {
                "deposit_wallet": self.DW,
                "swept": 15_353_790,
                "unwrap_tx_hash": "0xunwrap",
                "wrap_tx_hash": "0xwrap",
                "wrap_error": None,
            }

        monkeypatch.setattr(adapter, "sweep_wrapped_collateral", fake_sweep)

        ok, res = await adapter.redeem_positions(condition_id=self.COND)
        assert ok is True
        assert swept["called"] is True
        assert res["swept"] == 15_353_790
        assert "recovered wrapped collateral" in res["message"]

    @pytest.mark.asyncio
    async def test_redeem_still_fails_when_nothing_redeemable_and_no_wcol(
        self, adapter, monkeypatch
    ):
        monkeypatch.setattr(adapter, "deposit_wallet_address", lambda: self.DW)

        async def fake_preflight(**_kwargs):
            return (
                False,
                "No redeemable balance detected for the provided condition_id.",
            )

        monkeypatch.setattr(adapter, "preflight_redeem", fake_preflight)

        async def fake_sweep():
            return True, {
                "deposit_wallet": self.DW,
                "swept": 0,
                "message": "No wrapped collateral to sweep.",
            }

        monkeypatch.setattr(adapter, "sweep_wrapped_collateral", fake_sweep)

        ok, res = await adapter.redeem_positions(condition_id=self.COND)
        assert ok is False
        assert "No redeemable balance" in res
