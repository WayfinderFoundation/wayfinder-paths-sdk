from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from wayfinder_paths.core.clients.TokenClient import TokenClient
from wayfinder_paths.core.constants.chains import CHAIN_ID_SOLANA
from wayfinder_paths.core.utils.svm_tokens import SOL_DECIMALS
from wayfinder_paths.mcp.tools.tokens import (
    onchain_fuzzy_search_tokens,
    onchain_get_gas_token,
    onchain_get_settlement_assets,
    onchain_list_tokens,
    onchain_resolve_token,
)


@pytest.mark.asyncio
async def test_resolve_token_happy_path():
    fake_client = AsyncMock()
    fake_client.get_token_details = AsyncMock(return_value={"symbol": "USDC"})

    with patch("wayfinder_paths.mcp.tools.tokens.TOKEN_CLIENT", fake_client):
        out = await onchain_resolve_token("usd-coin-arbitrum")

    assert out["ok"] is True
    assert out["result"]["symbol"] == "USDC"


@pytest.mark.asyncio
async def test_resolve_token_hides_backend_url_on_status_error():
    fake_client = AsyncMock()
    request = httpx.Request(
        "GET",
        "https://strategies-dev.wayfinder.ai/api/v1/blockchain/tokens/detail/",
    )
    response = httpx.Response(400, request=request)
    fake_client.get_token_details = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "bad request", request=request, response=response
        )
    )

    with patch("wayfinder_paths.mcp.tools.tokens.TOKEN_CLIENT", fake_client):
        out = await onchain_resolve_token("polygon_usdc")

    assert out["ok"] is False
    assert out["error"]["code"] == "token_not_resolved"
    assert "strategies-dev.wayfinder.ai" not in out["error"]["message"]
    assert out["error"]["details"] == {"status_code": 400}


@pytest.mark.asyncio
async def test_get_gas_token_happy_path():
    fake_client = AsyncMock()
    fake_client.get_gas_token = AsyncMock(return_value={"symbol": "ETH"})

    with patch("wayfinder_paths.mcp.tools.tokens.TOKEN_CLIENT", fake_client):
        out = await onchain_get_gas_token("arbitrum")

    assert out["ok"] is True
    assert out["result"]["symbol"] == "ETH"


@pytest.mark.asyncio
async def test_solana_gas_token_uses_canonical_sdk_metadata():
    client = TokenClient()
    response = httpx.Response(
        200,
        json={
            "data": {
                "asset_id": "solana",
                "symbol": "SOL",
                "decimals": 18,
                "address": None,
                "chain": {
                    "id": 1,
                    "code": "ethereum",
                    "name": "Ethereum",
                },
            }
        },
        request=httpx.Request("GET", "https://example.test/tokens/gas/"),
    )
    try:
        with patch.object(
            client,
            "_authed_request",
            new=AsyncMock(return_value=response),
        ):
            token = await client.get_gas_token("solana")
    finally:
        await client.client.aclose()

    assert token["symbol"] == "SOL"
    assert token["decimals"] == SOL_DECIMALS
    assert token["chain"] == {
        "id": CHAIN_ID_SOLANA,
        "code": "solana",
        "name": "Solana",
    }


@pytest.mark.asyncio
async def test_token_candles_returns_rows():
    client = TokenClient()
    response = httpx.Response(
        200,
        json={"rows": [{"t": 1, "c": "1.0"}]},
        request=httpx.Request("GET", "https://example.test/tokens/candles/"),
    )
    request = AsyncMock(return_value=response)
    try:
        with patch.object(client, "_authed_request", new=request):
            rows = await client.get_candles("0xabc", "5m", chain_id=8453)
    finally:
        await client.client.aclose()

    assert rows == [{"t": 1, "c": "1.0"}]
    assert request.await_args.kwargs["params"] == {
        "coin": "0xabc",
        "interval": "5m",
        "chain_id": 8453,
    }


@pytest.mark.asyncio
async def test_fuzzy_search_tokens_happy_path():
    fake_client = AsyncMock()
    fake_client.fuzzy_search = AsyncMock(return_value={"results": [{"id": "foo"}]})

    with patch("wayfinder_paths.mcp.tools.tokens.TOKEN_CLIENT", fake_client):
        out = await onchain_fuzzy_search_tokens(chain_code="arbitrum", query="usd")

    assert out["ok"] is True
    assert out["result"]["results"][0]["id"] == "foo"


def test_fuzzy_xml_parses_safety_metadata_and_compatibility_score():
    response = TokenClient()._parse_fuzzy_xml(
        """<?xml version="1.0"?>
        <tokens>
          <notice>Verified USDT0 on hyperevm.</notice>
          <token>
            <address>0xB8CE59FC3717ada4C02eaDF9682A9e934F625ebb</address>
            <chain>hyperevm</chain>
            <symbol>USDT0</symbol>
            <match_score>100</match_score>
            <confidence>100</confidence>
            <is_canonical>true</is_canonical>
            <verification>usdt0</verification>
            <suspicious>false</suspicious>
            <canonical_asset>
              <chain_code>hyperevm</chain_code>
              <chain_id>999</chain_id>
              <symbol>USDT0</symbol>
              <name>USDT0</name>
              <address>0xB8CE59FC3717ada4C02eaDF9682A9e934F625ebb</address>
              <decimals>6</decimals>
            </canonical_asset>
          </token>
        </tokens>"""
    )

    assert response["notice"] == "Verified USDT0 on hyperevm."
    token = response["tokens"][0]
    assert token["match_score"] == 100
    assert token["confidence"] == 100
    assert token["is_canonical"] is True
    assert token["suspicious"] is False
    assert token["canonical_asset"]["chain_id"] == 999


def test_fuzzy_xml_backfills_match_score_from_legacy_confidence():
    response = TokenClient()._parse_fuzzy_xml(
        "<tokens><token><symbol>RANDOM</symbol><confidence>72</confidence></token></tokens>"
    )

    assert response["tokens"][0]["match_score"] == 72


@pytest.mark.asyncio
async def test_get_settlement_assets_happy_path():
    fake_client = AsyncMock()
    fake_client.get_canonical_assets = AsyncMock(
        return_value={
            "success": True,
            "chain_code": "hyperevm",
            "assets": [],
            "settlement_assets": [{"symbol": "USDT0"}, {"symbol": "USDC"}],
        }
    )

    with patch("wayfinder_paths.mcp.tools.tokens.TOKEN_CLIENT", fake_client):
        out = await onchain_get_settlement_assets("hyperevm")

    assert out["ok"] is True
    assert out["result"]["settlement_assets"][0]["symbol"] == "USDT0"
    fake_client.get_canonical_assets.assert_awaited_once_with("hyperevm")


@pytest.mark.asyncio
async def test_list_tokens_happy_path():
    fake_client = AsyncMock()
    fake_client.discover_tokens = AsyncMock(
        return_value={
            "success": True,
            "chain_code": "robinhood",
            "dimension": "trending",
            "tokens": [{"symbol": "CASHCAT", "liquidity_usd": 353592.6}],
        }
    )

    with patch("wayfinder_paths.mcp.tools.tokens.TOKEN_CLIENT", fake_client):
        out = await onchain_list_tokens("robinhood", "trending")

    assert out["ok"] is True
    assert out["result"]["tokens"][0]["symbol"] == "CASHCAT"
    fake_client.discover_tokens.assert_awaited_once_with("robinhood", "trending", 25)


@pytest.mark.asyncio
async def test_list_tokens_rejects_bad_dimension():
    fake_client = AsyncMock()
    with patch("wayfinder_paths.mcp.tools.tokens.TOKEN_CLIENT", fake_client):
        out = await onchain_list_tokens("robinhood", "bogus")

    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_dimension"
    fake_client.discover_tokens.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_tokens_passes_volume_dimension_and_limit():
    fake_client = AsyncMock()
    fake_client.discover_tokens = AsyncMock(return_value={"tokens": []})
    with patch("wayfinder_paths.mcp.tools.tokens.TOKEN_CLIENT", fake_client):
        out = await onchain_list_tokens("base", "volume", 10)

    assert out["ok"] is True
    fake_client.discover_tokens.assert_awaited_once_with("base", "volume", 10)


@pytest.mark.asyncio
async def test_list_tokens_requires_a_specific_chain():
    fake_client = AsyncMock()

    with patch("wayfinder_paths.mcp.tools.tokens.TOKEN_CLIENT", fake_client):
        out = await onchain_list_tokens(chain_code="all")

    assert out["ok"] is False
    assert out["error"]["code"] == "unknown_chain_code"
    fake_client.discover_tokens.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_tokens_unknown_chain_code_errors():
    fake_client = AsyncMock()

    with patch("wayfinder_paths.mcp.tools.tokens.TOKEN_CLIENT", fake_client):
        out = await onchain_list_tokens(chain_code="dogechain")

    assert out["ok"] is False
    assert out["error"]["code"] == "unknown_chain_code"
    fake_client.discover_tokens.assert_not_awaited()


@pytest.mark.live_data
@pytest.mark.requires_config
@pytest.mark.asyncio
async def test_list_tokens_live_backend_propagation():
    """Live: real discovery data flows backend -> TokenClient -> tool.

    Skips (not fails) wherever the pipe can't be exercised — no API key in the
    environment, or the backend discover endpoint (vault-backend #935) not
    deployed yet. Once deployed, this proves the agent-facing tool returns
    real tokens with identity + market data end to end.
    """
    out = await onchain_list_tokens("robinhood", "trending", 5)

    if not out["ok"]:
        pytest.skip(f"live discover unavailable here: {out['error']}")
    tokens = out["result"]["tokens"]
    assert tokens, "live discover returned no tokens for robinhood"
    first = tokens[0]
    assert first["token_id"].startswith("robinhood_0x")
    assert {"symbol", "price_usd", "liquidity_usd", "volume_24h_usd"} <= set(first)
