from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from web3 import Web3

from wayfinder_paths.adapters.pendle_adapter.adapter import PendleAdapter


class TestPendleAdapter:
    def test_adapter_type(self):
        adapter = PendleAdapter(config={})
        assert adapter.adapter_type == "PENDLE"

    @pytest.mark.asyncio
    async def test_list_active_pt_yt_markets_filters_and_sort(self, monkeypatch):
        fixed_now = datetime(2026, 1, 1, tzinfo=UTC)
        monkeypatch.setattr(
            "wayfinder_paths.adapters.pendle_adapter.adapter._now_utc",
            lambda: fixed_now,
        )

        def markets_payload(chain_id: int) -> dict[str, Any]:
            if chain_id == 42161:
                return {
                    "markets": [
                        {
                            "chainId": 42161,
                            "name": "PT-USDC-Example",
                            "address": "42161-0xMarketA",
                            "pt": "42161-0xPTA",
                            "yt": "42161-0xYTA",
                            "sy": "42161-0xSYA",
                            "underlyingAsset": "42161-0xUSDC",
                            "expiry": "2026-02-01T00:00:00.000Z",
                            "details": {
                                "liquidity": 300_000,
                                "tradingVolume": 40_000,
                                "totalTvl": 500_000,
                                "impliedApy": 0.12,
                                "underlyingApy": 0.15,
                            },
                        },
                        {
                            "chainId": 42161,
                            "name": "TooSmallLiquidity",
                            "address": "42161-0xMarketB",
                            "pt": "42161-0xPTB",
                            "yt": "42161-0xYTB",
                            "sy": "42161-0xSYB",
                            "underlyingAsset": "42161-0xUSDT",
                            "expiry": "2026-02-01T00:00:00.000Z",
                            "details": {
                                "liquidity": 10_000,
                                "tradingVolume": 999_999,
                                "totalTvl": 500_000,
                                "impliedApy": 0.50,
                                "underlyingApy": 0.55,
                            },
                        },
                    ]
                }
            if chain_id == 8453:
                return {
                    "markets": [
                        {
                            "chainId": 8453,
                            "name": "PT-DAI-Example",
                            "address": "8453-0xMarketC",
                            "pt": "8453-0xPTC",
                            "yt": "8453-0xYTC",
                            "sy": "8453-0xSYC",
                            "underlyingAsset": "8453-0xDAI",
                            "expiry": "2026-03-15T00:00:00.000Z",
                            "details": {
                                "liquidity": 800_000,
                                "tradingVolume": 30_000,
                                "totalTvl": 1_100_000,
                                "impliedApy": 0.10,
                                "underlyingApy": 0.13,
                            },
                        }
                    ]
                }
            return {"markets": []}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/core/v1/markets/all"
            chain_id = int(request.url.params.get("chainId", "0"))
            assert request.url.params.get("isActive") == "true"
            return httpx.Response(200, json=markets_payload(chain_id))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = PendleAdapter(config={}, client=client)

        rows = await adapter.list_active_pt_yt_markets(
            chains=["arbitrum", "base"],
            min_liquidity_usd=250_000,
            min_volume_usd_24h=25_000,
            min_days_to_expiry=7,
            sort_by="fixed_apy",
            descending=True,
        )

        await client.aclose()

        # TooSmallLiquidity filtered out; remaining rows sorted by fixedApy desc
        assert [r["marketAddress"] for r in rows] == ["0xMarketA", "0xMarketC"]
        assert rows[0]["fixedApy"] == 0.12
        assert rows[0]["floatingApy"] == pytest.approx(0.03)
        assert rows[0]["underlyingAddress"] == "0xUSDC"

    @pytest.mark.asyncio
    async def test_sdk_swap_v2_builds_query_params(self):
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["params"] = dict(request.url.params)
            return httpx.Response(
                200,
                json={
                    "tx": {"to": "0xRouter", "data": "0xdeadbeef", "value": "0"},
                    "tokenApprovals": [],
                    "data": {"amountOut": "123"},
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = PendleAdapter(
            config={}, client=client, base_url="https://api-v2.pendle.finance/core"
        )

        market = "0xMarketA"
        await adapter.sdk_swap_v2(
            chain="arbitrum",
            market_address=market,
            receiver="0xReceiver",
            slippage=0.01,
            token_in="0xTokenIn",
            token_out="0xTokenOut",
            amount_in="1000",
            enable_aggregator=True,
            aggregators=["one", "two"],
            additional_data=["impliedApy", "effectiveApy"],
            need_scale=True,
        )

        await client.aclose()

        assert captured["path"] == f"/core/v2/sdk/42161/markets/{market}/swap"
        params = captured["params"]
        assert params["receiver"] == "0xReceiver"
        assert params["slippage"] == "0.01"
        assert params["enableAggregator"] == "true"
        assert params["aggregators"] == "one,two"
        assert params["tokenIn"] == "0xTokenIn"
        assert params["tokenOut"] == "0xTokenOut"
        assert params["amountIn"] == "1000"
        assert params["additionalData"] == "impliedApy,effectiveApy"
        assert params["needScale"] == "true"

    @pytest.mark.asyncio
    async def test_sdk_convert_v2_falls_back_to_get_and_builds_query_params(self):
        captured: dict[str, Any] = {"methods": []}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["methods"].append(request.method)

            if request.method == "POST":
                # Pendle currently responds 404 for POST in some environments.
                return httpx.Response(
                    404,
                    json={"message": "Not Found"},
                    headers={"x-ratelimit-remaining": "98"},
                )

            captured["path"] = request.url.path
            captured["params"] = dict(request.url.params)
            return httpx.Response(
                200,
                json={
                    "action": "swap",
                    "inputs": [{"token": "0xTokenIn", "amount": "1000"}],
                    "requiredApprovals": [{"token": "0xTokenIn", "amount": "1000"}],
                    "routes": [
                        {
                            "tx": {
                                "to": "0xRouter",
                                "from": "0xFrom",
                                "data": "0xdead",
                                "value": "0",
                            },
                            "outputs": [{"token": "0xTokenOut", "amount": "123"}],
                            "data": {"effectiveApy": 0.1, "priceImpact": 0.01},
                        }
                    ],
                },
                headers={"x-computing-unit": "6", "x-ratelimit-remaining": "99"},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = PendleAdapter(
            config={"strategy_wallet": {"address": "0x" + "a" * 40}},
            client=client,
            base_url="https://api-v2.pendle.finance/core",
        )

        resp = await adapter.sdk_convert_v2(
            chain="arbitrum",
            slippage=0.01,
            receiver="0xReceiver",
            inputs=[{"token": "0xTokenIn", "amount": "1000"}],
            outputs=["0xTokenOut"],
            enable_aggregator=True,
            aggregators=["kyberswap"],
            additional_data=["impliedApy", "effectiveApy", "priceImpact"],
            need_scale=True,
            use_limit_order=True,
        )

        await client.aclose()

        assert captured["methods"] == ["POST", "GET"]
        assert captured["path"] == "/core/v2/sdk/42161/convert"
        params = captured["params"]
        assert params["receiver"] == "0xReceiver"
        assert params["slippage"] == "0.01"
        assert params["tokensIn"] == "0xTokenIn"
        assert params["amountsIn"] == "1000"
        assert params["tokensOut"] == "0xTokenOut"
        assert params["enableAggregator"] == "true"
        assert params["aggregators"] == "kyberswap"
        assert params["additionalData"] == "impliedApy,effectiveApy,priceImpact"
        assert params["needScale"] == "true"
        assert params["useLimitOrder"] == "true"
        assert resp["rateLimit"]["computingUnit"] == 6

    def test_build_convert_plan_selects_best_route(self):
        adapter = PendleAdapter(
            config={"strategy_wallet": {"address": "0x" + "a" * 40}}
        )
        convert_response = {
            "action": "swap",
            "requiredApprovals": [{"token": "0xTokenIn", "amount": "1000"}],
            "routes": [
                {
                    "tx": {
                        "to": "0x" + "b" * 40,
                        "from": "0x" + "a" * 40,
                        "data": "0x01",
                        "value": "0",
                    },
                    "outputs": [{"token": "0xTokenOut", "amount": "100"}],
                },
                {
                    "tx": {
                        "to": "0x" + "c" * 40,
                        "from": "0x" + "a" * 40,
                        "data": "0x02",
                        "value": "0",
                    },
                    "outputs": [{"token": "0xTokenOut", "amount": "200"}],
                },
            ],
        }

        plan = adapter.build_convert_plan(
            chain=42161, convert_response=convert_response
        )
        assert plan["tx"]["to"].lower() == ("0x" + "c" * 40).lower()
        assert plan["outputs"][0]["amount"] == "200"
        assert plan["approvals"][0]["token"] == "0xTokenIn"

    @pytest.mark.asyncio
    async def test_execute_convert_success(self):
        signing_callback = AsyncMock(return_value=b"\x00" * 65)

        adapter = PendleAdapter(
            config={"strategy_wallet": {"address": "0x" + "a" * 40}},
            signing_callback=signing_callback,
        )

        adapter.sdk_convert_v2 = AsyncMock(
            return_value={
                "action": "swap",
                "requiredApprovals": [{"token": "0x" + "c" * 40, "amount": "1000"}],
                "routes": [
                    {
                        "tx": {
                            "to": "0x" + "b" * 40,
                            "from": "0x" + "a" * 40,
                            "data": "0xdead",
                            "value": "0",
                        },
                        "outputs": [{"token": "0x" + "d" * 40, "amount": "123"}],
                    }
                ],
            }
        )

        with (
            patch(
                "wayfinder_paths.adapters.pendle_adapter.adapter.get_token_balance",
                new_callable=AsyncMock,
                return_value=10_000,
            ),
            patch(
                "wayfinder_paths.adapters.pendle_adapter.adapter.ensure_allowance",
                new_callable=AsyncMock,
                return_value=(True, "0xapprovehash"),
            ),
            patch.object(
                adapter,
                "_send_tx",
                new_callable=AsyncMock,
                return_value=(True, "0xtxhash123"),
            ),
        ):
            ok, res = await adapter.execute_convert(
                chain="arbitrum",
                slippage=0.01,
                inputs=[{"token": "0x" + "c" * 40, "amount": "1000"}],
                outputs=["0x" + "d" * 40],
                receiver="0x" + "a" * 40,
            )

        assert ok is True
        assert res["tx_hash"] == "0xtxhash123"
        assert res["chainId"] == 42161

    @pytest.mark.asyncio
    async def test_build_best_pt_swap_tx_selects_best_effective_apy(self):
        adapter = PendleAdapter(config={})
        adapter.list_active_pt_yt_markets = AsyncMock(
            return_value=[
                {
                    "marketAddress": "0xM1",
                    "ptAddress": "0xPT1",
                    "fixedApy": 0.10,
                    "liquidityUsd": 500_000,
                    "volumeUsd24h": 100_000,
                    "daysToExpiry": 30.0,
                },
                {
                    "marketAddress": "0xM2",
                    "ptAddress": "0xPT2",
                    "fixedApy": 0.12,
                    "liquidityUsd": 500_000,
                    "volumeUsd24h": 100_000,
                    "daysToExpiry": 30.0,
                },
            ]
        )

        async def fake_swap(*, market_address: str, **_: Any) -> dict[str, Any]:
            if market_address == "0xM1":
                return {
                    "tx": {"to": "0xRouter", "data": "0x1"},
                    "data": {"effectiveApy": 0.08, "priceImpact": 0.001},
                    "tokenApprovals": [],
                }
            return {
                "tx": {"to": "0xRouter", "data": "0x2"},
                "data": {"effectiveApy": 0.09, "priceImpact": 0.005},
                "tokenApprovals": [],
            }

        adapter.sdk_swap_v2 = AsyncMock(side_effect=fake_swap)

        best = await adapter.build_best_pt_swap_tx(
            chain=42161,
            token_in="0xTokenIn",
            amount_in="1000",
            receiver="0xReceiver",
            max_markets_to_quote=2,
            prefer="effective_apy",
        )

        assert best["ok"] is True
        assert best["selectedMarket"]["marketAddress"] == "0xM2"
        assert best["quote"]["effectiveApy"] == 0.09

    @pytest.mark.asyncio
    async def test_get_full_user_state_onchain_multicall_filters_zeros(
        self, monkeypatch
    ):
        w3 = Web3()
        user = "0x" + ("a" * 40)
        market1 = "0x" + ("1" * 40)
        pt1 = "0x" + ("2" * 40)
        yt1 = "0x" + ("3" * 40)
        sy1 = "0x" + ("4" * 40)
        u1 = "0x" + ("5" * 40)

        market2 = "0x" + ("6" * 40)
        pt2 = "0x" + ("7" * 40)
        yt2 = "0x" + ("8" * 40)
        sy2 = "0x" + ("9" * 40)
        u2 = "0x" + ("b" * 40)

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield w3

        adapter = PendleAdapter(config={})
        adapter.fetch_markets = AsyncMock(
            return_value={
                "markets": [
                    {
                        "chainId": 42161,
                        "name": "M1",
                        "address": f"42161-{market1}",
                        "pt": f"42161-{pt1}",
                        "yt": f"42161-{yt1}",
                        "sy": f"42161-{sy1}",
                        "underlyingAsset": f"42161-{u1}",
                        "expiry": "2026-02-01T00:00:00.000Z",
                        "isActive": True,
                    },
                    {
                        "chainId": 42161,
                        "name": "M2",
                        "address": f"42161-{market2}",
                        "pt": f"42161-{pt2}",
                        "yt": f"42161-{yt2}",
                        "sy": f"42161-{sy2}",
                        "underlyingAsset": f"42161-{u2}",
                        "expiry": "2026-02-01T00:00:00.000Z",
                        "isActive": True,
                    },
                ]
            }
        )

        # Order is: (pt bal, pt dec, yt bal, yt dec, lp bal, lp dec, sy bal, sy dec) per market.
        adapter._multicall_uint256_chunked = AsyncMock(
            return_value=[
                1000,
                18,
                0,
                18,
                0,
                18,
                0,
                18,
                0,
                18,
                0,
                18,
                0,
                18,
                0,
                18,
            ]
        )

        monkeypatch.setattr(
            "wayfinder_paths.adapters.pendle_adapter.adapter.web3_from_chain_id",
            mock_web3_ctx,
        )

        ok, state = await adapter.get_full_user_state_per_chain(
            chain=42161, account=user
        )
        assert ok is True
        assert state["protocol"] == "pendle"
        assert state["chainId"] == 42161
        assert state["account"] == user
        assert len(state["positions"]) == 1
        assert state["positions"][0]["marketAddress"] == market1

    @pytest.mark.asyncio
    async def test_execute_swap_success(self):
        """Test successful swap execution."""
        signing_callback = AsyncMock(return_value=b"\x00" * 65)

        router_addr = "0x" + "b" * 40
        token_in_addr = "0x" + "c" * 40

        adapter = PendleAdapter(
            config={"strategy_wallet": {"address": "0x" + "a" * 40}},
            signing_callback=signing_callback,
        )

        # Mock sdk_swap_v2 to return a valid quote
        adapter.sdk_swap_v2 = AsyncMock(
            return_value={
                "tx": {"to": router_addr, "data": "0xdeadbeef", "value": "0"},
                "tokenApprovals": [{"token": token_in_addr, "amount": "1000000"}],
                "data": {"amountOut": "990000", "priceImpact": 0.001},
            }
        )

        # Mock allowance and approval
        with (
            patch(
                "wayfinder_paths.adapters.pendle_adapter.adapter.ensure_allowance",
                new_callable=AsyncMock,
                return_value=(True, "0xapprovehash"),
            ),
            patch(
                "wayfinder_paths.adapters.pendle_adapter.adapter.send_transaction",
                new_callable=AsyncMock,
                return_value="0xtxhash123",
            ) as mock_send,
        ):
            success, result = await adapter.execute_swap(
                chain="arbitrum",
                market_address="0x" + "d" * 40,
                token_in=token_in_addr,
                token_out="0x" + "e" * 40,
                amount_in="1000000",
                slippage=0.01,
            )

        assert success is True
        assert result["tx_hash"] == "0xtxhash123"
        assert result["chainId"] == 42161
        assert "quote" in result
        mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_swap_quote_fails(self):
        """Test swap fails when quote returns invalid tx."""
        adapter = PendleAdapter(
            config={"strategy_wallet": {"address": "0x" + "a" * 40}},
            signing_callback=AsyncMock(),
        )

        adapter.sdk_swap_v2 = AsyncMock(
            return_value={"tx": None, "tokenApprovals": [], "data": {}}
        )

        success, result = await adapter.execute_swap(
            chain="arbitrum",
            market_address="0xMarket",
            token_in="0xTokenIn",
            token_out="0xPT",
            amount_in="1000000",
        )

        assert success is False
        assert result["stage"] == "quote"
        assert "error" in result

    @pytest.mark.asyncio
    async def test_execute_swap_approval_fails(self):
        """Test swap fails when approval fails."""
        router_addr = "0x" + "b" * 40
        token_in_addr = "0x" + "c" * 40

        adapter = PendleAdapter(
            config={"strategy_wallet": {"address": "0x" + "a" * 40}},
            signing_callback=AsyncMock(return_value=b"\x00" * 65),
        )

        adapter.sdk_swap_v2 = AsyncMock(
            return_value={
                "tx": {"to": router_addr, "data": "0xdeadbeef"},
                "tokenApprovals": [{"token": token_in_addr, "amount": "1000000"}],
                "data": {},
            }
        )

        with (
            patch(
                "wayfinder_paths.adapters.pendle_adapter.adapter.ensure_allowance",
                new_callable=AsyncMock,
                return_value=(False, {"error": "Approval tx failed"}),
            ),
        ):
            success, result = await adapter.execute_swap(
                chain="arbitrum",
                market_address="0x" + "d" * 40,
                token_in=token_in_addr,
                token_out="0x" + "e" * 40,
                amount_in="1000000",
            )

        assert success is False
        assert result["stage"] == "approval"

    @pytest.mark.asyncio
    async def test_execute_swap_requires_signing_callback(self):
        """Test swap fails without signing callback."""
        router_addr = "0x" + "b" * 40
        token_in_addr = "0x" + "c" * 40

        adapter = PendleAdapter(
            config={"strategy_wallet": {"address": "0x" + "a" * 40}},
            # No signing callback
        )

        # Include tokenApprovals so it fails during the approval stage
        adapter.sdk_swap_v2 = AsyncMock(
            return_value={
                "tx": {"to": router_addr, "data": "0xdeadbeef"},
                "tokenApprovals": [{"token": token_in_addr, "amount": "1000000"}],
                "data": {},
            }
        )

        success, result = await adapter.execute_swap(
            chain="arbitrum",
            market_address="0x" + "d" * 40,
            token_in=token_in_addr,
            token_out="0x" + "e" * 40,
            amount_in="1000000",
        )

        assert success is False
        assert result["stage"] == "approval"
        assert "signing_callback" in result["details"]["error"]

    @pytest.mark.asyncio
    async def test_execute_swap_requires_strategy_wallet(self):
        """Test swap fails without strategy_wallet address."""
        adapter = PendleAdapter(
            config={},  # No strategy_wallet
            signing_callback=AsyncMock(),
        )

        with pytest.raises(ValueError, match="strategy_wallet address is required"):
            await adapter.execute_swap(
                chain="arbitrum",
                market_address="0xMarket",
                token_in="0xTokenIn",
                token_out="0xPT",
                amount_in="1000000",
            )
