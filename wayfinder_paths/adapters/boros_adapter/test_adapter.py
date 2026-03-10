"""Tests for BorosAdapter."""

import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eth_abi import encode as abi_encode

from wayfinder_paths.adapters.boros_adapter.adapter import (
    BorosAdapter,
    BorosLimitOrder,
    BorosMarketQuote,
    BorosVault,
)


class TestBorosAdapter:
    """Test cases for BorosAdapter."""

    @pytest.fixture
    def mock_boros_client(self):
        """Mock BorosClient for testing."""
        mock_client = AsyncMock()
        return mock_client

    @pytest.fixture
    def adapter(self, mock_boros_client):
        """Create a BorosAdapter instance with mocked client for testing."""
        mock_config = {
            "boros_adapter": {},
        }
        with patch(
            "wayfinder_paths.adapters.boros_adapter.adapter.BorosClient",
            return_value=mock_boros_client,
        ):
            adapter = BorosAdapter(
                config=mock_config,
                wallet_address="0x1234567890123456789012345678901234567890",
            )
            adapter.boros_client = mock_boros_client
            return adapter

    def test_adapter_type(self, adapter):
        """Test adapter has correct type."""
        assert adapter.adapter_type == "BOROS"

    @pytest.mark.asyncio
    async def test_list_markets_success(self, adapter, mock_boros_client):
        """Test successful market listing."""
        mock_response = [
            {"marketId": 1, "symbol": "HYPE-USD", "underlying": "HYPE"},
            {"marketId": 2, "symbol": "BTC-USD", "underlying": "BTC"},
        ]
        mock_boros_client.list_markets = AsyncMock(return_value=mock_response)

        success, markets = await adapter.list_markets()

        assert success is True
        assert len(markets) == 2
        assert markets[0]["marketId"] == 1

    @pytest.mark.asyncio
    async def test_list_markets_failure(self, adapter, mock_boros_client):
        """Test market listing failure."""
        mock_boros_client.list_markets = AsyncMock(side_effect=Exception("API Error"))

        success, data = await adapter.list_markets()

        assert success is False
        assert "API Error" in str(data)

    @pytest.mark.asyncio
    async def test_get_market_success(self, adapter, mock_boros_client):
        """Test successful single market fetch."""
        mock_response = {
            "marketId": 18,
            "symbol": "HYPERLIQUID-HYPE-USD",
            "underlying": "HYPE",
        }
        mock_boros_client.get_market = AsyncMock(return_value=mock_response)

        success, market = await adapter.get_market(18)

        assert success is True
        assert market["marketId"] == 18

    @pytest.mark.asyncio
    async def test_get_orderbook_success(self, adapter, mock_boros_client):
        """Test successful orderbook fetch."""
        mock_response = {
            "long": {"ia": [100, 105, 110], "sz": [1000, 2000, 3000]},
            "short": {"ia": [115, 120, 125], "sz": [1500, 2500, 3500]},
        }
        mock_boros_client.get_order_book = AsyncMock(return_value=mock_response)

        success, book = await adapter.get_orderbook(18)

        assert success is True
        assert "long" in book
        assert "short" in book

    @pytest.mark.asyncio
    async def test_quote_market_success(self, adapter, mock_boros_client):
        """Test successful market quote."""
        mock_market = {
            "marketId": 18,
            "address": "0xabcd",
            "symbol": "HYPERLIQUID-HYPE-USD",
            "underlying": "HYPE",
            "imData": {"tickStep": 1, "maturity": 1735689600},
            "tokenId": 3,
        }
        mock_orderbook = {
            "long": {"ia": [100, 105]},
            "short": {"ia": [115, 120]},
        }
        mock_boros_client.get_order_book = AsyncMock(return_value=mock_orderbook)

        success, quote = await adapter.quote_market(mock_market)

        assert success is True
        assert isinstance(quote, BorosMarketQuote)
        assert quote.market_id == 18
        assert quote.best_bid_apr == 0.105  # max(long) * tick_size
        assert quote.best_ask_apr == 0.115  # min(short) * tick_size

    @pytest.mark.asyncio
    async def test_quote_market_uses_market_snapshot_when_available(
        self, adapter, mock_boros_client
    ):
        """Prefer /markets embedded `data` instead of fetching an orderbook."""
        mock_market = {
            "marketId": 18,
            "address": "0xabcd",
            "imData": {
                "symbol": "HYPERLIQUID-HYPE-USD",
                "underlying": "HYPE",
                "collateral": "0xUSDT",
                "tickStep": 1,
                "maturity": 1735689600,
            },
            "tokenId": 3,
            "data": {
                "bestBid": 0.10,
                "bestAsk": 0.12,
                "midApr": 0.11,
                "floatingApr": 0.13,
                "b7dmafr": 0.09,
                "b30dmafr": 0.08,
            },
        }
        mock_boros_client.get_order_book = AsyncMock(
            side_effect=AssertionError("get_order_book should not be called")
        )

        success, quote = await adapter.quote_market(
            mock_market, prefer_market_data=True
        )

        assert success is True
        assert isinstance(quote, BorosMarketQuote)
        assert quote.best_bid_apr == 0.10
        assert quote.best_ask_apr == 0.12
        assert quote.mid_apr == 0.11
        assert quote.floating_apr == 0.13
        assert quote.funding_7d_ma_apr == 0.09
        assert quote.funding_30d_ma_apr == 0.08

    @pytest.mark.asyncio
    async def test_quote_markets_for_underlying_success(
        self, adapter, mock_boros_client
    ):
        """Test quoting markets for underlying."""
        mock_markets = [
            {
                "marketId": 18,
                "symbol": "HYPERLIQUID-HYPE-USD-30D",
                "underlying": "HYPE",
                "imData": {"maturity": 1735689600},
            },
            {
                "marketId": 19,
                "symbol": "HYPERLIQUID-HYPE-USD-60D",
                "underlying": "HYPE",
                "imData": {"maturity": 1738368000},
            },
            {
                "marketId": 20,
                "symbol": "BTC-USD-30D",
                "underlying": "BTC",
                "imData": {"maturity": 1735689600},
            },
        ]
        mock_boros_client.list_markets = AsyncMock(return_value=mock_markets)
        mock_boros_client.get_order_book = AsyncMock(
            return_value={"long": {"ia": [100]}, "short": {"ia": [110]}}
        )

        success, quotes = await adapter.quote_markets_for_underlying("HYPE")

        assert success is True
        assert len(quotes) == 2  # Only HYPE markets
        assert all(q.underlying == "HYPE" for q in quotes)

    @pytest.mark.asyncio
    async def test_list_tenor_quotes_filters_underlying(
        self, adapter, mock_boros_client
    ):
        mock_markets = [
            {
                "marketId": 1,
                "address": "0x1111",
                "imData": {"symbol": "HYPE-USD", "maturity": 1000},
                "metadata": {"assetSymbol": "HYPE"},
                "data": {"midApr": 0.10, "floatingApr": 0.11},
            },
            {
                "marketId": 2,
                "address": "0x2222",
                "imData": {"symbol": "BTC-USD", "maturity": 2000},
                "metadata": {"assetSymbol": "BTC"},
                "data": {"midApr": 0.05, "floatingApr": 0.06},
            },
        ]
        mock_boros_client.list_markets = AsyncMock(return_value=mock_markets)

        ok, quotes = await adapter.list_tenor_quotes(underlying_symbol="HYPE")

        assert ok is True
        assert len(quotes) == 1
        assert quotes[0].underlying_symbol == "HYPE"
        assert quotes[0].mid_apr == 0.10
        assert quotes[0].floating_apr == 0.11

    @pytest.mark.asyncio
    async def test_get_collaterals_success(self, adapter, mock_boros_client):
        """Test collateral fetch."""
        mock_response = {
            "collaterals": [
                {
                    "tokenId": 3,
                    "crossPosition": {"netBalance": "100000000000000000000"},
                    "isolatedPositions": [],
                }
            ]
        }
        mock_boros_client.get_collaterals = AsyncMock(return_value=mock_response)

        success, data = await adapter.get_collaterals()

        assert success is True
        assert "collaterals" in data

    @pytest.mark.asyncio
    async def test_get_account_balances_success(self, adapter, mock_boros_client):
        """Test account balance fetch."""
        mock_response = {
            "collaterals": [
                {
                    "tokenId": 3,
                    "crossPosition": {"availableBalance": "100000000000000000000"},
                    "isolatedPositions": [{"availableBalance": "50000000000000000000"}],
                }
            ]
        }
        mock_boros_client.get_collaterals = AsyncMock(return_value=mock_response)

        success, balances = await adapter.get_account_balances(token_id=3)

        assert success is True
        assert balances["cross"] == 100.0
        assert balances["isolated"] == 50.0
        assert balances["total"] == 150.0

    @pytest.mark.asyncio
    async def test_get_active_positions_success(self, adapter, mock_boros_client):
        """Test active positions fetch."""
        mock_response = {
            "collaterals": [
                {
                    "tokenId": 3,
                    "crossPosition": {
                        "marketPositions": [
                            {
                                "marketId": 18,
                                "side": 1,
                                "sizeWei": "1000000000000000000",
                            }
                        ]
                    },
                    "isolatedPositions": [],
                }
            ]
        }
        mock_boros_client.get_collaterals = AsyncMock(return_value=mock_response)

        success, positions = await adapter.get_active_positions(market_id=18)

        assert success is True
        assert len(positions) == 1
        assert positions[0]["marketId"] == 18
        assert positions[0]["size"] == 1.0

    @pytest.mark.asyncio
    async def test_get_open_limit_orders_success(self, adapter, mock_boros_client):
        """Test open orders fetch."""
        mock_response = [
            {
                "orderId": "order-123",
                "marketId": 18,
                "side": 1,
                "size": "1000000000000000000",
                "filledSize": "500000000000000000",
                "limitTick": 100,
                "tickStep": 1,
                "status": "open",
            }
        ]
        mock_boros_client.get_open_orders = AsyncMock(return_value=mock_response)

        success, orders = await adapter.get_open_limit_orders()

        assert success is True
        assert len(orders) == 1
        assert isinstance(orders[0], BorosLimitOrder)
        assert orders[0].order_id == "order-123"
        assert orders[0].size == 1.0
        assert orders[0].filled_size == 0.5
        assert orders[0].remaining_size == 0.5

    @pytest.mark.asyncio
    async def test_get_full_user_state_success(self, adapter, mock_boros_client):
        market_acc = "0x" + ("0" * 58) + "000012"  # 0x12 == 18
        mock_boros_client.get_collaterals = AsyncMock(
            return_value={
                "collaterals": [
                    {
                        "tokenId": 3,
                        "crossPosition": {
                            "availableBalance": "1000000000000000000",
                            "marketPositions": [
                                {
                                    "marketId": 18,
                                    "side": 1,
                                    "notionalSize": "1000000000000000000",
                                    "pnl": {
                                        "unrealisedPnl": "0",
                                        "rateSettlementPnl": "0",
                                    },
                                }
                            ],
                        },
                        "isolatedPositions": [
                            {
                                "availableBalance": "2000000000000000000",
                                "marketAcc": market_acc,
                                "marketPositions": [
                                    {
                                        "marketId": 18,
                                        "side": 0,
                                        "notionalSize": "2000000000000000000",
                                        "pnl": {
                                            "unrealisedPnl": "0",
                                            "rateSettlementPnl": "0",
                                        },
                                    }
                                ],
                            }
                        ],
                        "withdrawal": {
                            "lastWithdrawalRequestTime": 0,
                            "lastWithdrawalAmount": 0,
                        },
                    }
                ]
            }
        )
        mock_boros_client.get_open_orders = AsyncMock(
            return_value=[
                {
                    "orderId": "order-1",
                    "marketId": 18,
                    "side": 1,
                    "size": "1000000000000000000",
                    "filledSize": "0",
                    "limitTick": 100,
                    "tickStep": 1,
                    "status": "open",
                }
            ]
        )

        ok, state = await adapter.get_full_user_state(
            account="0x1234567890123456789012345678901234567890",
            include_withdrawal_status=False,
        )
        assert ok is True
        assert state["protocol"] == "boros"
        assert state["chainId"] == adapter.chain_id
        assert "collaterals" in state
        assert state["balances"]["total"] == 3.0
        assert len(state["positions"]) == 2
        assert len(state["openOrders"]) == 1

    @pytest.mark.asyncio
    async def test_get_cash_fee_data_decodes_values(self, adapter):
        """Test MarketHub.getCashFeeData decoding (on-chain read is mocked)."""
        scaling_factor_wei = 123
        fee_rate_wei = 5_000_000_000_000_000  # 0.005e18
        min_cash_cross_wei = 400_000_000_000_000_000  # 0.4e18
        min_cash_isolated_wei = 1_000_000_000_000_000_000  # 1.0e18
        raw = abi_encode(
            ["uint256", "uint256", "uint256", "uint256"],
            [
                scaling_factor_wei,
                fee_rate_wei,
                min_cash_cross_wei,
                min_cash_isolated_wei,
            ],
        )

        mock_web3 = AsyncMock()
        mock_web3.eth.call = AsyncMock(return_value=raw)
        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value = mock_web3
        mock_cm.__aexit__.return_value = False

        with patch(
            "wayfinder_paths.adapters.boros_adapter.adapter.web3_from_chain_id",
            return_value=mock_cm,
        ):
            ok, data = await adapter.get_cash_fee_data(token_id=5)

        assert ok is True
        assert data["token_id"] == 5
        assert data["scaling_factor_wei"] == scaling_factor_wei
        assert data["fee_rate_wei"] == fee_rate_wei
        assert data["min_cash_cross_wei"] == min_cash_cross_wei
        assert data["min_cash_isolated_wei"] == min_cash_isolated_wei
        assert data["fee_rate"] == pytest.approx(fee_rate_wei / 1e18)
        assert data["min_cash_cross"] == pytest.approx(0.4)
        assert data["min_cash_isolated"] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_sweep_isolated_to_cross_filters_by_market(self, adapter):
        """Test isolated -> cross sweep only affects the specified market."""
        adapter.get_account_balances = AsyncMock(
            return_value=(
                True,
                {
                    "isolated_positions": [
                        {"market_id": 18, "balance_wei": 111},
                        {"market_id": 19, "balance_wei": 222},
                    ]
                },
            )
        )
        adapter.cash_transfer = AsyncMock(return_value=(True, {"status": "ok"}))

        ok, res = await adapter.sweep_isolated_to_cross(token_id=3, market_id=19)
        assert ok is True
        assert res["status"] == "ok"
        assert len(res["moved"]) == 1
        assert res["moved"][0]["market_id"] == 19
        assert res["moved"][0]["balance_wei"] == 222

        adapter.cash_transfer.assert_awaited_once_with(
            market_id=19, amount_wei=222, is_deposit=False
        )

    @pytest.mark.asyncio
    async def test_sweep_isolated_to_cross_errors_on_failed_transfer(self, adapter):
        """Test sweep fails fast when an isolated->cross transfer fails."""
        adapter.get_account_balances = AsyncMock(
            return_value=(
                True,
                {
                    "isolated_positions": [
                        {"market_id": 18, "balance_wei": 111},
                    ]
                },
            )
        )
        adapter.cash_transfer = AsyncMock(return_value=(False, {"error": "nope"}))

        ok, res = await adapter.sweep_isolated_to_cross(token_id=3, market_id=18)
        assert ok is False
        assert "Failed sweep isolated->cross" in res["error"]
        assert res["moved"][0]["market_id"] == 18
        assert res["moved"][0]["ok"] is False

    @pytest.mark.asyncio
    async def test_deposit_to_cross_margin_sweeps_after_deposit(
        self, adapter, mock_boros_client
    ):
        """Test deposit falls back to direct isolated->cross transfer when sweep is empty."""
        adapter.sign_callback = object()

        mock_boros_client.build_deposit_calldata = AsyncMock(
            return_value={
                "to": "0x0000000000000000000000000000000000000002",
                "data": "0xdeadbeef",
                "value": 0,
            }
        )

        @asynccontextmanager
        async def _mock_web3_from_chain_id(_chain_id: int):  # noqa: ANN001
            mock_web3 = MagicMock()
            mock_contract = MagicMock()
            mock_fn = MagicMock()
            mock_fn.call = AsyncMock(return_value=10**30)  # plenty of balance
            mock_contract.functions.balanceOf.return_value = mock_fn
            mock_web3.eth.contract.return_value = mock_contract
            yield mock_web3

        with (
            patch(
                "wayfinder_paths.adapters.boros_adapter.adapter.web3_from_chain_id",
                new=_mock_web3_from_chain_id,
            ),
            patch(
                "wayfinder_paths.adapters.boros_adapter.adapter.build_approve_transaction",
                new=AsyncMock(
                    return_value={"to": "0x0", "data": "0x0", "chainId": 42161}
                ),
            ),
            patch(
                "wayfinder_paths.adapters.boros_adapter.adapter.send_transaction",
                new=AsyncMock(return_value="0xapprove"),
            ),
            patch.object(
                adapter,
                "_broadcast_calldata",
                new=AsyncMock(return_value=(True, {"tx_hash": "0xdeposit"})),
            ),
            patch.object(
                adapter,
                "sweep_isolated_to_cross",
                new=AsyncMock(return_value=(True, {"status": "ok", "moved": []})),
            ) as mock_sweep,
            patch.object(
                adapter,
                "unscaled_to_scaled_cash_wei",
                new=AsyncMock(return_value=10**18),
            ) as mock_scaled,
            patch.object(
                adapter,
                "cash_transfer",
                new=AsyncMock(return_value=(True, {"status": "ok"})),
            ) as mock_transfer,
        ):
            ok, res = await adapter.deposit_to_cross_margin(
                collateral_address="0x0000000000000000000000000000000000000001",
                amount_wei=1_000_000,  # 1 USDT
                token_id=3,
                market_id=18,
            )

        assert ok is True
        assert res["status"] == "ok"
        assert res["approve"]["tx_hash"] == "0xapprove"
        assert res["tx"]["tx_hash"] == "0xdeposit"
        assert res["sweep"]["status"] == "fallback_direct_transfer"

        mock_sweep.assert_awaited_once_with(token_id=3, market_id=18)
        mock_scaled.assert_awaited_once_with(3, 1_000_000)
        mock_transfer.assert_awaited_once_with(
            market_id=18,
            amount_wei=10**18,
            is_deposit=False,
        )

    @pytest.mark.asyncio
    async def test_deposit_to_vault_ignores_zero_available_balance_cap(self, adapter):
        adapter.get_vaults_summary = AsyncMock(
            return_value=(
                True,
                [
                    BorosVault(
                        amm_id=680,
                        market_id=68,
                        symbol="BYBIT-HYPEUSDT-27MAR2026",
                        raw={"user": {"availableBalanceToDeposit": "0"}},
                    )
                ],
            )
        )
        adapter._get_amm_id_for_market = AsyncMock(return_value=680)
        adapter.deposit_to_vault_direct = AsyncMock(
            return_value=(True, {"status": "ok", "amm_id": 680})
        )

        ok, res = await adapter.deposit_to_vault(
            market_id=68,
            net_cash_in_wei=2 * 10**18,
        )

        assert ok is True
        assert res["amm_id"] == 680
        adapter.deposit_to_vault_direct.assert_awaited_once_with(
            amm_id=680,
            net_cash_in_wei=2 * 10**18,
            min_lp_out_wei=None,
            slippage_bps=20,
        )

    @pytest.mark.asyncio
    async def test_close_positions_except_skips_keep_market(self, adapter):
        adapter.close_positions_market = AsyncMock(
            return_value=(True, {"status": "ok"})
        )

        ok, res = await adapter.close_positions_except(
            keep_market_id=19, token_id=3, market_ids=[18, 19, 20]
        )
        assert ok is True
        assert res["status"] == "ok"

        calls = adapter.close_positions_market.await_args_list
        assert len(calls) == 2
        assert calls[0].args[0] == 18
        assert calls[1].args[0] == 20

    @pytest.mark.asyncio
    async def test_ensure_position_size_yu_increases_short(self, adapter):
        adapter.get_active_positions = AsyncMock(return_value=(True, []))
        adapter.place_rate_order = AsyncMock(return_value=(True, {"tx_hash": "0xopen"}))

        ok, res = await adapter.ensure_position_size_yu(
            market_id=18, token_id=3, target_size_yu=1.5
        )
        assert ok is True
        assert res["action"] == "increase_short"
        adapter.place_rate_order.assert_awaited_once()

        _, kwargs = adapter.place_rate_order.await_args
        assert kwargs["market_id"] == 18
        assert kwargs["token_id"] == 3
        assert kwargs["side"] == "short"
        assert kwargs["tif"] == "IOC"
        assert kwargs["size_yu_wei"] == int(1.5 * 1e18)

    @pytest.mark.asyncio
    async def test_ensure_position_size_yu_decreases(self, adapter):
        adapter.get_active_positions = AsyncMock(
            return_value=(True, [{"size": 2.0, "sizeWei": int(2e18)}])
        )
        adapter.close_positions_market = AsyncMock(
            return_value=(True, {"tx_hash": "0xclose"})
        )

        ok, res = await adapter.ensure_position_size_yu(
            market_id=18, token_id=3, target_size_yu=1.0
        )
        assert ok is True
        assert res["action"] == "decrease"
        adapter.close_positions_market.assert_awaited_once_with(
            market_id=18, token_id=3, size_yu_wei=int(1.0 * 1e18)
        )

    @pytest.mark.asyncio
    async def test_bridge_hype_oft_rounds_amount_and_builds_tx(self, adapter):
        adapter.sign_callback = object()

        mock_contract = SimpleNamespace()
        mock_dec_fn = SimpleNamespace(call=AsyncMock(return_value=10))
        mock_quote_fn = SimpleNamespace(call=AsyncMock(return_value=(5, 0)))
        mock_functions = SimpleNamespace(
            decimalConversionRate=MagicMock(return_value=mock_dec_fn),
            quoteSend=MagicMock(return_value=mock_quote_fn),
        )
        mock_contract.functions = mock_functions

        mock_web3 = SimpleNamespace(
            eth=SimpleNamespace(contract=MagicMock(return_value=mock_contract)),
            to_checksum_address=lambda x: x,
        )
        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value = mock_web3
        mock_cm.__aexit__.return_value = False

        with (
            patch(
                "wayfinder_paths.adapters.boros_adapter.adapter.web3_from_chain_id",
                return_value=mock_cm,
            ),
            patch(
                "wayfinder_paths.adapters.boros_adapter.adapter.encode_call",
                new=AsyncMock(return_value={"chainId": 999}),
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.boros_adapter.adapter.send_transaction",
                new=AsyncMock(return_value="0xtx"),
            ),
        ):
            ok, res = await adapter.bridge_hype_oft_hyperevm_to_arbitrum(
                amount_wei=123,
                max_value_wei=1000,
                to_address=adapter.wallet_address,
                from_address=adapter.wallet_address,
            )

        assert ok is True
        assert res["status"] == "ok"
        assert res["tx_hash"] == "0xtx"
        assert res["amount_wei"] == 120  # rounded down by conversion rate 10
        assert res["native_fee_wei"] == 5
        assert res["total_value_wei"] == 125

        _, kwargs = mock_encode.await_args
        assert kwargs["fn_name"] == "send"
        assert kwargs["value"] == 125
        send_params = kwargs["args"][0]
        assert send_params[0] == 30110
        assert send_params[2] == 120

    @pytest.mark.asyncio
    async def test_bridge_hype_oft_arbitrum_to_hyperevm_rounds_amount_and_builds_tx(
        self, adapter
    ):
        adapter.sign_callback = object()

        mock_contract = SimpleNamespace()
        mock_dec_fn = SimpleNamespace(call=AsyncMock(return_value=10))
        mock_quote_fn = SimpleNamespace(call=AsyncMock(return_value=(7, 0)))
        mock_functions = SimpleNamespace(
            decimalConversionRate=MagicMock(return_value=mock_dec_fn),
            quoteSend=MagicMock(return_value=mock_quote_fn),
        )
        mock_contract.functions = mock_functions

        mock_web3 = SimpleNamespace(
            eth=SimpleNamespace(contract=MagicMock(return_value=mock_contract)),
            to_checksum_address=lambda x: x,
        )
        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value = mock_web3
        mock_cm.__aexit__.return_value = False

        with (
            patch(
                "wayfinder_paths.adapters.boros_adapter.adapter.web3_from_chain_id",
                return_value=mock_cm,
            ),
            patch(
                "wayfinder_paths.adapters.boros_adapter.adapter.encode_call",
                new=AsyncMock(return_value={"chainId": 42161}),
            ) as mock_encode,
            patch(
                "wayfinder_paths.adapters.boros_adapter.adapter.send_transaction",
                new=AsyncMock(return_value="0xtx"),
            ),
        ):
            ok, res = await adapter.bridge_hype_oft_arbitrum_to_hyperevm(
                amount_wei=123,
                max_fee_wei=1000,
                to_address=adapter.wallet_address,
                from_address=adapter.wallet_address,
            )

        assert ok is True
        assert res["status"] == "ok"
        assert res["tx_hash"] == "0xtx"
        assert res["amount_wei"] == 120  # rounded down by conversion rate 10
        assert res["native_fee_wei"] == 7
        assert res["total_value_wei"] == 7

        _, kwargs = mock_encode.await_args
        assert kwargs["fn_name"] == "send"
        assert kwargs["value"] == 7
        send_params = kwargs["args"][0]
        assert len(send_params) == 7
        assert send_params[0] == adapter.LZ_EID_HYPEREVM
        assert send_params[2] == 120

    def test_tick_from_rate(self):
        """Test APR to tick conversion."""
        # 10% APR with tick_step=1
        tick = BorosAdapter.tick_from_rate(0.10, tick_step=1, round_down=False)
        assert tick > 0

        # Verify roundtrip
        rate_back = BorosAdapter.rate_from_tick(tick, tick_step=1)
        assert abs(rate_back - 0.10) < 0.001

    def test_rate_from_tick(self):
        """Test tick to APR conversion."""
        rate = BorosAdapter.rate_from_tick(954, tick_step=1)
        assert rate > 0
        assert rate < 1  # Should be a decimal

        # Negative tick
        rate_neg = BorosAdapter.rate_from_tick(-954, tick_step=1)
        assert rate_neg < 0

    def test_normalize_apr(self):
        """Test APR normalization."""
        # Already decimal
        assert BorosAdapter.normalize_apr(0.10) == 0.10

        # Percent (values between 1 and 1000)
        assert BorosAdapter.normalize_apr(10.0) == 0.10

        # BPS (values > 1000)
        assert BorosAdapter.normalize_apr(1100) == 0.11  # 1100 bps = 11%

        # 1e18 scaled
        result = BorosAdapter.normalize_apr(100000000000000000)
        assert abs(result - 0.10) < 0.001

        # None
        assert BorosAdapter.normalize_apr(None) is None

    @pytest.mark.asyncio
    async def test_get_vaults_summary_uses_direct_lp_balances(
        self, adapter, mock_boros_client
    ):
        mock_boros_client.get_amm_summary = AsyncMock(
            return_value={
                "vaults": [
                    {
                        "ammId": 7,
                        "marketId": 18,
                        "symbol": "HYPE-USDT",
                        "lpApy": 0.12,
                        "lpPrice": 1.0,
                        "totalSupplyCap": 1000,
                        "totalLp": 500,
                    }
                ]
            }
        )
        adapter.list_markets_all = AsyncMock(
            return_value=(
                True,
                [
                    {
                        "marketId": 18,
                        "tokenId": 3,
                        "metadata": {"name": "HYPE-USDT"},
                        "imData": {"symbol": "HYPE-USDT", "maturity": int(time.time()) + 86400},
                    }
                ],
            )
        )
        adapter.get_vault_lp_balance = AsyncMock(return_value=(True, int(25 * 1e18)))

        ok, vaults = await adapter.get_vaults_summary(account=adapter.wallet_address)

        assert ok is True
        assert isinstance(vaults, list) and len(vaults) == 1
        vault = vaults[0]
        assert isinstance(vault, BorosVault)
        assert vault.amm_id == 7
        assert vault.user_total_lp_wei == int(25 * 1e18)
        assert vault.user_deposit_tokens == pytest.approx(25.0)

    @pytest.mark.asyncio
    async def test_vault_helpers_reuse_summary_fields(self, adapter):
        vault = BorosVault(
            amm_id=7,
            market_id=18,
            symbol="HYPE-USDT",
            remaining_supply_lp=int(50 * 1e18),
            tenor_days=10.0,
            raw={
                "lpPrice": 1.25,
                "user": {"totalLp": str(int(8 * 1e18))},
            },
        )

        assert adapter.estimate_user_lp_balance_wei(vault) == int(8 * 1e18)
        assert adapter.estimate_user_vault_value_tokens(vault) == pytest.approx(10.0)
        assert adapter.estimate_vault_capacity_tokens(vault) == pytest.approx(62.5)
        assert adapter.is_vault_open_for_deposit(vault, min_tenor_days=3.0) is True

    @pytest.mark.asyncio
    async def test_best_yield_vault_filters_expired_and_capacity(self, adapter):
        adapter.search_vaults = AsyncMock(
            return_value=(
                True,
                [
                    BorosVault(
                        amm_id=1,
                        market_id=11,
                        symbol="A",
                        apy=0.08,
                        is_expired=True,
                    ),
                    BorosVault(
                        amm_id=2,
                        market_id=12,
                        symbol="B",
                        apy=0.10,
                        remaining_supply_lp=int(50 * 1e18),
                        raw={"lpPrice": 1.0},
                        tenor_days=10.0,
                    ),
                    BorosVault(
                        amm_id=3,
                        market_id=14,
                        symbol="D",
                        apy=0.20,
                        remaining_supply_lp=int(50 * 1e18),
                        raw={"lpPrice": 1.0},
                        tenor_days=10.0,
                        is_isolated_only=True,
                    ),
                    BorosVault(
                        amm_id=4,
                        market_id=15,
                        symbol="E",
                        apy=0.18,
                        remaining_supply_lp=int(50 * 1e18),
                        raw={"lpPrice": 1.0},
                        tenor_days=10.0,
                        market_state="Paused",
                    ),
                    BorosVault(
                        amm_id=5,
                        market_id=13,
                        symbol="C",
                        apy=0.15,
                        remaining_supply_lp=int(1 * 1e18),
                        raw={"lpPrice": 1.0},
                        tenor_days=10.0,
                    ),
                ],
            )
        )

        ok, best = await adapter.best_yield_vault(token_id=3, amount_tokens=10.0)

        assert ok is True
        assert isinstance(best, BorosVault)
        assert best.amm_id == 2

    @pytest.mark.asyncio
    async def test_get_account_idle_balance_reads_total(self, adapter):
        adapter.get_account_balances = AsyncMock(return_value=(True, {"total": 42.5}))

        ok, total = await adapter.get_account_idle_balance(token_id=3, account_id=0)

        assert ok is True
        assert total == 42.5
