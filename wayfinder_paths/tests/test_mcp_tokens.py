from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from wayfinder_paths.mcp.tools.tokens import (
    onchain_fuzzy_search_tokens,
    onchain_get_gas_token,
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
async def test_fuzzy_search_tokens_happy_path():
    fake_client = AsyncMock()
    fake_client.fuzzy_search = AsyncMock(return_value={"results": [{"id": "foo"}]})

    with patch("wayfinder_paths.mcp.tools.tokens.TOKEN_CLIENT", fake_client):
        out = await onchain_fuzzy_search_tokens(chain_code="arbitrum", query="usd")

    assert out["ok"] is True
    assert out["result"]["results"][0]["id"] == "foo"


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
