from __future__ import annotations

import asyncio
import os

import pytest

from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.constants.chains import CHAIN_CODE_TO_ID
from wayfinder_paths.core.utils.token_resolver import TokenResolver


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()

@pytest.mark.skipif(
    os.environ.get("CI") == "true",
    reason="Live quote_swap test — local only",
)
class TestNativeTokenQueries:
    @pytest.mark.asyncio
    async def test_literal_native_with_chain_id(self):
        meta = await TokenResolver.resolve_token_meta("native", chain_id=42161)
        assert meta["token_id"] == "arbitrum_0x0000000000000000000000000000000000000000"
        assert meta["symbol"].lower() == "eth"
        assert meta["decimals"] == 18
        assert meta["chain_id"] == 42161
        assert meta["address"] == ZERO_ADDRESS
        assert meta["metadata"].get("source") == "api"

    @pytest.mark.asyncio
    async def test_literal_native_with_chain_id_polygon(self):
        meta = await TokenResolver.resolve_token_meta("native", chain_id=137)
        assert meta["token_id"] == "polygon_0x0000000000000000000000000000000000001010"
        assert meta["symbol"].lower() == "pol"
        assert meta["decimals"] == 18
        assert meta["chain_id"] == 137
        assert meta["address"] == ZERO_ADDRESS
        assert meta["metadata"].get("source") == "api"

    @pytest.mark.asyncio
    async def test_arbitrum_native(self):
        meta = await TokenResolver.resolve_token_meta("arbitrum_native")
        assert meta["token_id"] == "arbitrum_0x0000000000000000000000000000000000000000"
        assert meta["symbol"].lower() == "eth"
        assert meta["decimals"] == 18
        assert meta["chain_id"] == 42161
        assert meta["address"] == ZERO_ADDRESS
        assert meta["metadata"].get("source") == "api"

    @pytest.mark.asyncio
    async def test_native_arbitrum(self):
        meta = await TokenResolver.resolve_token_meta("arbitrum_native")
        assert meta["token_id"] == "arbitrum_0x0000000000000000000000000000000000000000"
        assert meta["symbol"].lower() == "eth"
        assert meta["decimals"] == 18
        assert meta["chain_id"] == 42161
        assert meta["address"] == ZERO_ADDRESS
        assert meta["metadata"].get("source") == "api"

    @pytest.mark.asyncio
    async def test_literal_native_without_any_chain_indication(self):
        with pytest.raises(
            ValueError, match="Chain id is not provided for native query"
        ):
            await TokenResolver.resolve_token_meta("native")

    @pytest.mark.asyncio
    async def test_literal_native_with_chain_indication(self):
        meta = await TokenResolver.resolve_token_meta("native", chain_id=42161)
        assert meta["token_id"] == "arbitrum_0x0000000000000000000000000000000000000000"
        assert meta["symbol"].lower() == "eth"
        assert meta["decimals"] == 18
        assert meta["chain_id"] == 42161
        assert meta["address"] == ZERO_ADDRESS
        assert meta["metadata"].get("source") == "api"

    @pytest.mark.asyncio
    async def test_eth_arbitrum_resolves_native(self):
        meta = await TokenResolver.resolve_token_meta("ethereum-arbitrum")
        assert meta["token_id"] == "arbitrum_0x0000000000000000000000000000000000000000"
        assert meta["symbol"].lower() == "eth"
        assert meta["decimals"] == 18
        assert meta["chain_id"] == 42161
        assert meta["address"] == ZERO_ADDRESS
        assert meta["metadata"].get("source") == "api"

    @pytest.mark.asyncio
    async def test_ethereum_base_resolves_native(self):
        meta = await TokenResolver.resolve_token_meta("ethereum-base")
        assert meta["token_id"] == "base_0x0000000000000000000000000000000000000000"
        assert meta["symbol"].lower() == "eth"
        assert meta["decimals"] == 18
        assert meta["chain_id"] == 8453
        assert meta["address"] == ZERO_ADDRESS
        assert meta["metadata"].get("source") == "api"

    @pytest.mark.asyncio
    async def test_bad_string(self):
        with pytest.raises(ValueError, match="Cannot resolve token: "):
            await TokenResolver.resolve_token_meta("dfcghvbjknml")

@pytest.mark.skipif(
    os.environ.get("CI") == "true",
    reason="Live quote_swap test — local only",
)
class TestCoingeckoIdQueries:
    @pytest.mark.asyncio
    async def test_arbitrum_bridged_weth(self):
        meta = await TokenResolver.resolve_token_meta(
            "arbitrum-arbitrum-bridged-weth-arbitrum-one"
        )

        assert meta["token_id"] == "arbitrum_0x82af49447d8a07e3bd95bd0d56f35241523fbab1"
        assert meta["symbol"].lower() == "weth"
        assert meta["decimals"] == 18
        assert meta["chain_id"] == CHAIN_CODE_TO_ID["arbitrum"]
        assert meta["address"] == "0x82af49447d8a07e3bd95bd0d56f35241523fbab1"
        assert meta["metadata"].get("source") == "api"

    @pytest.mark.asyncio
    async def test_usd_coin_base(self):
        meta = await TokenResolver.resolve_token_meta("usd-coin-base")
        assert (
            meta["token_id"]
            == "base_0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913".lower()
        )
        assert meta["symbol"].lower() == "usdc"
        assert meta["decimals"] == 6
        assert meta["chain_id"] == CHAIN_CODE_TO_ID["base"]
        assert (
            meta["address"].lower()
            == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913".lower()
        )
        assert meta["metadata"].get("source") == "api"

    @pytest.mark.asyncio
    async def test_usd_coin_arbitrum(self):
        meta = await TokenResolver.resolve_token_meta("usd-coin-arbitrum")
        assert (
            meta["token_id"]
            == "arbitrum_0xaf88d065e77c8cc2239327c5edb3a432268e5831".lower()
        )
        assert meta["symbol"].lower() == "usdc"
        assert meta["decimals"] == 6
        assert meta["chain_id"] == CHAIN_CODE_TO_ID["arbitrum"]
        assert (
            meta["address"].lower()
            == "0xaf88d065e77c8cc2239327c5edb3a432268e5831".lower()
        )
        assert meta["metadata"].get("source") == "api"

    @pytest.mark.asyncio
    async def test_bridged_usdc_polygon(self):
        meta = await TokenResolver.resolve_token_meta(
            "polygon-bridged-usdc-polygon-pos-bridge"
        )
        assert (
            meta["token_id"]
            == "polygon_0x2791bca1f2de4661ed88a30c99a7a9449aa84174".lower()
        )
        assert meta["symbol"].lower() == "usdc.e"
        assert meta["decimals"] == 6
        assert meta["chain_id"] == CHAIN_CODE_TO_ID["polygon"]
        assert (
            meta["address"].lower()
            == "0x2791bca1f2de4661ed88a30c99a7a9449aa84174".lower()
        )
        assert meta["metadata"].get("source") == "api"

    @pytest.mark.asyncio
    async def test_tether_ethereum(self):
        meta = await TokenResolver.resolve_token_meta("tether-ethereum")
        assert meta["token_id"] == "ethereum_0xdac17f958d2ee523a2206206994597c13d831ec7"
        assert meta["symbol"].lower() == "usdt"
        assert meta["decimals"] == 6
        assert meta["chain_id"] == 1
        assert meta["address"] == "0xdac17f958d2ee523a2206206994597c13d831ec7"
        assert meta["metadata"].get("source") == "api"

    @pytest.mark.asyncio
    async def test_weth_ethereum(self):
        meta = await TokenResolver.resolve_token_meta("WETH-ethereum")
        assert meta["chain_id"] == CHAIN_CODE_TO_ID["ethereum"]
        assert meta["address"].lower() != ZERO_ADDRESS.lower()


@pytest.mark.skipif(
    os.environ.get("CI") == "true",
    reason="Live quote_swap test — local only",
)
class TestQuoteSwapLive:
    @pytest.mark.asyncio
    async def test_quote_usdc_to_eth_on_arbitrum(self):
        from wayfinder_paths.mcp.tools.quotes import quote_swap
        from wayfinder_paths.mcp.utils import find_wallet_by_label

        wallet = find_wallet_by_label("main")
        if not wallet:
            pytest.skip("No 'main' wallet configured")

        out = await quote_swap(
            wallet_label="main",
            from_token="usd-coin-arbitrum",
            to_token="ethereum-arbitrum",
            amount="1",
            slippage_bps=100,
        )

        assert out.get("ok") is True, f"quote_swap failed: {out}"
        result = out["result"]
        assert result["from_token"] == "USDC"
        assert result["to_token"] == "ETH"
        assert result["quote"]["best_quote"] is not None
