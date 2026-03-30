from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

import wayfinder_paths.adapters.polymarket_adapter.adapter as polymarket_adapter_module
from wayfinder_paths.adapters.polymarket_adapter.adapter import PolymarketAdapter
from wayfinder_paths.core.constants.polymarket import (
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
            call=AsyncMock(side_effect=[1_230_000, 4_560_000])
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

        account = "0x" + "11" * 20
        adapter._funder_override = account  # allow open-order fetch for this account

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

        assert state["usdc_e_balance"] == pytest.approx(1.23)
        assert state["usdc_balance"] == pytest.approx(4.56)
        assert state["balances"]["usdc_e"]["amount_base_units"] == 1_230_000
        assert state["balances"]["usdc"]["amount_base_units"] == 4_560_000

    @pytest.mark.asyncio
    async def test_bridge_deposit_prefers_brap_swap(self, adapter, monkeypatch):
        from_address = "0x000000000000000000000000000000000000dEaD"

        async def sign_cb(_tx: dict) -> bytes:
            return b""

        monkeypatch.setattr(adapter, "_require_signer", lambda: (from_address, sign_cb))
        monkeypatch.setattr(
            polymarket_adapter_module,
            "get_token_balance",
            AsyncMock(return_value=2_000_000),
        )

        best_quote = {
            "provider": "enso",
            "input_amount": 1_000_000,
            "output_amount": 999_000,
            "fee_estimate": {"fee_total_usd": 0.01, "fee_breakdown": []},
            "calldata": {
                "to": "0x1111111111111111111111111111111111111111",
                "data": "0xdeadbeef",
                "value": "0",
                "chainId": 137,
            },
        }
        monkeypatch.setattr(
            polymarket_adapter_module.BRAP_CLIENT,
            "get_quote",
            AsyncMock(return_value={"best_quote": best_quote, "quotes": []}),
        )
        monkeypatch.setattr(
            polymarket_adapter_module,
            "ensure_allowance",
            AsyncMock(return_value=(True, "0xapprove")),
        )
        monkeypatch.setattr(
            polymarket_adapter_module,
            "send_transaction",
            AsyncMock(return_value="0xswap"),
        )

        ok, res = await adapter.bridge_deposit(
            from_chain_id=137,
            from_token_address=POLYGON_USDC_ADDRESS,
            amount=1.0,
            recipient_address=from_address,
            token_decimals=6,
        )
        assert ok is True
        assert isinstance(res, dict)
        assert res["method"] == "brap"
        assert res["from_token_address"].lower() == POLYGON_USDC_ADDRESS.lower()
        assert res["to_token_address"].lower() == POLYGON_USDC_E_ADDRESS.lower()

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
            polymarket_adapter_module.BRAP_CLIENT,
            "get_quote",
            AsyncMock(side_effect=Exception("no route")),
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
        assert ok is True
        assert isinstance(res, dict)
        assert res["method"] == "polymarket_bridge"
        assert res["tx_hash"] == "0xtransfer"

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
    async def test_preflight_redeem_ignores_log_scan_errors(self, adapter, monkeypatch):
        condition_id = "0x" + "11" * 32
        holder = "0x" + "22" * 20

        monkeypatch.setattr(
            adapter, "_outcome_index_sets", AsyncMock(return_value=[1, 2])
        )

        mock_ctf = MagicMock()
        # 3 collaterals × 2 index_sets = 6 calls per function
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

        best_quote = {
            "provider": "enso",
            "input_amount": 1_000_000,
            "output_amount": 999_000,
            "fee_estimate": {"fee_total_usd": 0.01, "fee_breakdown": []},
            "calldata": {
                "to": "0x1111111111111111111111111111111111111111",
                "data": "0xdeadbeef",
                "value": "0",
                "chainId": 137,
            },
        }
        monkeypatch.setattr(
            polymarket_adapter_module.BRAP_CLIENT,
            "get_quote",
            AsyncMock(return_value={"best_quote": best_quote, "quotes": []}),
        )
        monkeypatch.setattr(
            polymarket_adapter_module,
            "ensure_allowance",
            AsyncMock(return_value=(True, "0xapprove")),
        )
        monkeypatch.setattr(
            polymarket_adapter_module,
            "send_transaction",
            AsyncMock(return_value="0xswap"),
        )

        ok, res = await adapter.bridge_withdraw(
            amount_usdce=1.0,
            to_chain_id=137,
            to_token_address=POLYGON_USDC_ADDRESS,
            recipient_addr=from_address,
            token_decimals=6,
        )
        assert ok is True
        assert isinstance(res, dict)
        assert res["method"] == "brap"
        assert res["from_token_address"].lower() == POLYGON_USDC_E_ADDRESS.lower()
        assert res["to_token_address"].lower() == POLYGON_USDC_ADDRESS.lower()

    @pytest.mark.asyncio
    async def test_bridge_withdraw_falls_back_to_polymarket_bridge(
        self, adapter, monkeypatch
    ):
        from_address = "0x000000000000000000000000000000000000dEaD"

        async def sign_cb(_tx: dict) -> bytes:
            return b""

        monkeypatch.setattr(adapter, "_require_signer", lambda: (from_address, sign_cb))
        monkeypatch.setattr(
            polymarket_adapter_module.BRAP_CLIENT,
            "get_quote",
            AsyncMock(side_effect=Exception("no route")),
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

        ok, res = await adapter.bridge_withdraw(
            amount_usdce=1.0,
            to_chain_id=137,
            to_token_address=POLYGON_USDC_ADDRESS,
            recipient_addr=from_address,
            token_decimals=6,
        )
        assert ok is True
        assert isinstance(res, dict)
        assert res["method"] == "polymarket_bridge"
        assert res["tx_hash"] == "0xtransfer"

    # ------------------------------------------------------------------
    # Wallet trade history (paginated /activity)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_wallet_trade_history_paginates(self, adapter, monkeypatch):
        page_num = 0

        async def mock_get_activity(*, user, limit, offset, **kwargs):
            nonlocal page_num
            page_num += 1
            if page_num == 1:
                return True, [
                    {"timestamp": "1700000000", "type": "TRADE", "side": "BUY"},
                    {"timestamp": "1700003600", "type": "TRADE", "side": "SELL"},
                ]
            return True, []  # second page empty → stop

        monkeypatch.setattr(adapter, "get_activity", mock_get_activity)

        ok, records = await adapter.get_wallet_trade_history(
            user="0x" + "11" * 20,
            start_ts=1700000000,
            end_ts=1700010000,
            page_size=2,
        )
        assert ok is True
        assert len(records) == 2
        assert page_num == 2

    @pytest.mark.asyncio
    async def test_get_wallet_trade_history_filters_by_time(self, adapter, monkeypatch):
        async def mock_get_activity(*, user, limit, offset, **kwargs):
            return True, [
                {"timestamp": "1699999000", "type": "TRADE"},  # before start
                {"timestamp": "1700005000", "type": "TRADE"},  # in range
                {"timestamp": "1700020000", "type": "TRADE"},  # after end
            ]

        monkeypatch.setattr(adapter, "get_activity", mock_get_activity)

        ok, records = await adapter.get_wallet_trade_history(
            user="0x" + "11" * 20,
            start_ts=1700000000,
            end_ts=1700010000,
        )
        assert ok is True
        assert len(records) == 1
        assert records[0]["timestamp"] == "1700005000"

    @pytest.mark.asyncio
    async def test_get_wallet_trade_history_handles_iso_timestamps(
        self, adapter, monkeypatch
    ):
        async def mock_get_activity(*, user, limit, offset, **kwargs):
            return True, [
                {"timestamp": "2025-10-01T12:00:00Z", "type": "TRADE"},
            ]

        monkeypatch.setattr(adapter, "get_activity", mock_get_activity)

        ok, records = await adapter.get_wallet_trade_history(
            user="0x" + "11" * 20,
            start_ts=0,
            end_ts=2000000000,
        )
        assert ok is True
        assert len(records) == 1

    @pytest.mark.asyncio
    async def test_get_wallet_trade_history_propagates_error(
        self, adapter, monkeypatch
    ):
        async def mock_get_activity(*, user, limit, offset, **kwargs):
            return False, "rate limited"

        monkeypatch.setattr(adapter, "get_activity", mock_get_activity)

        ok, msg = await adapter.get_wallet_trade_history(
            user="0x" + "11" * 20,
        )
        assert ok is False
        assert "rate limited" in msg

    # ------------------------------------------------------------------
    # Batch prices history
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_batch_prices_history_merges_results(self, adapter, monkeypatch):
        call_log: list[dict] = []

        async def mock_get(*_args, **kwargs):
            params = kwargs.get("params", {})
            call_log.append(params)
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            token = params.get("market", "")
            mock_resp.json.return_value = {
                "history": [
                    {"t": 1700000000, "p": 0.5 if token == "tok_A" else 0.3},
                    {"t": 1700003600, "p": 0.6 if token == "tok_A" else 0.4},
                ]
            }
            return mock_resp

        monkeypatch.setattr(adapter._clob_http, "get", mock_get)

        ok, data = await adapter.get_batch_prices_history(
            token_ids=["tok_A", "tok_B"],
            start_ts=1700000000,
            end_ts=1700010000,
            fidelity=60,
        )
        assert ok is True
        assert isinstance(data, dict)
        assert set(data.keys()) == {"tok_A", "tok_B"}
        assert len(data["tok_A"]) == 2
        assert data["tok_A"][0]["p"] == 0.5
        assert data["tok_B"][0]["p"] == 0.3
        assert len(call_log) == 2

    @pytest.mark.asyncio
    async def test_get_batch_prices_history_empty_token(self, adapter, monkeypatch):
        async def mock_get(*_args, **kwargs):
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = {"history": []}
            return mock_resp

        monkeypatch.setattr(adapter._clob_http, "get", mock_get)

        ok, data = await adapter.get_batch_prices_history(
            token_ids=["tok_dead"],
            start_ts=1700000000,
            end_ts=1700010000,
        )
        assert ok is True
        assert data["tok_dead"] == []

    @pytest.mark.asyncio
    async def test_get_batch_prices_history_partial_failure(self, adapter, monkeypatch):
        """When a single token fetch fails, it returns an empty list."""

        async def mock_get(*_args, **kwargs):
            params = kwargs.get("params", {})
            token = params.get("market", "")
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            if token == "tok_bad":
                mock_resp.json.return_value = "unexpected string"
            else:
                mock_resp.json.return_value = {"history": [{"t": 1700005000, "p": 0.5}]}
            return mock_resp

        monkeypatch.setattr(adapter._clob_http, "get", mock_get)

        ok, data = await adapter.get_batch_prices_history(
            token_ids=["tok_ok", "tok_bad"],
            start_ts=1700000000,
            end_ts=1700010000,
        )
        assert ok is True
        assert len(data["tok_ok"]) == 1
        assert data["tok_bad"] == []

    # ------------------------------------------------------------------
    # Batch market metadata
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_markets_by_condition_ids(self, adapter, monkeypatch):
        call_count = 0

        async def mock_get_market(*, condition_id):
            nonlocal call_count
            call_count += 1
            return True, {
                "conditionId": condition_id,
                "slug": "market-one",
                "question": "Will X happen?",
                "outcomes": ["Yes", "No"],
                "outcomePrices": [0.6, 0.4],
                "clobTokenIds": ["tok1", "tok2"],
                "endDate": "2025-12-31T00:00:00Z",
                "closed": False,
                "volumeNum": 50000,
            }

        monkeypatch.setattr(adapter, "get_market_by_condition_id", mock_get_market)

        ok, data = await adapter.get_markets_by_condition_ids(
            condition_ids=["0xcond1"],
        )
        assert ok is True
        assert "0xcond1" in data
        assert data["0xcond1"]["slug"] == "market-one"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_get_markets_by_condition_ids_concurrent(self, adapter, monkeypatch):
        """Multiple condition IDs are fetched concurrently."""
        call_count = 0

        async def mock_get_market(*, condition_id):
            nonlocal call_count
            call_count += 1
            return True, {
                "conditionId": condition_id,
                "slug": f"market-{condition_id}",
                "outcomes": ["Yes", "No"],
                "outcomePrices": [0.5, 0.5],
                "clobTokenIds": ["t1", "t2"],
            }

        monkeypatch.setattr(adapter, "get_market_by_condition_id", mock_get_market)

        cids = [f"0xcond{i}" for i in range(5)]
        ok, data = await adapter.get_markets_by_condition_ids(condition_ids=cids)
        assert ok is True
        assert len(data) == 5
        assert call_count == 5

    @pytest.mark.asyncio
    async def test_get_markets_by_condition_ids_skips_not_found(
        self, adapter, monkeypatch
    ):
        async def mock_get_market(*, condition_id):
            if condition_id == "0xbad":
                return False, "Market not found"
            return True, {"conditionId": condition_id, "slug": "ok"}

        monkeypatch.setattr(adapter, "get_market_by_condition_id", mock_get_market)

        ok, data = await adapter.get_markets_by_condition_ids(
            condition_ids=["0xgood", "0xbad"],
        )
        assert ok is True
        assert len(data) == 1
        assert "0xgood" in data
