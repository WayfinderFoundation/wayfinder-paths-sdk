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
async def test_list_tokens_resolves_chain_code_to_id():
    fake_client = AsyncMock()
    fake_client.list_markets = AsyncMock(return_value=[{"symbol": "USDC"}])

    with patch("wayfinder_paths.mcp.tools.tokens.TOKEN_CLIENT", fake_client):
        out = await onchain_list_tokens(chain_code="base")

    assert out["ok"] is True
    assert out["result"]["tokens"][0]["symbol"] == "USDC"
    assert out["result"]["page"] == 1
    assert out["result"]["has_next"] is False
    fake_client.list_markets.assert_awaited_once_with(chain_id=8453)


@pytest.mark.asyncio
async def test_list_tokens_requires_a_specific_chain():
    fake_client = AsyncMock()
    fake_client.list_markets = AsyncMock()

    with patch("wayfinder_paths.mcp.tools.tokens.TOKEN_CLIENT", fake_client):
        out = await onchain_list_tokens(chain_code="all")

    assert out["ok"] is False
    assert out["error"]["code"] == "unknown_chain_code"
    fake_client.list_markets.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_tokens_paginates_by_volume():
    rows = [{"symbol": f"T{i}"} for i in range(60)]
    fake_client = AsyncMock()
    fake_client.list_markets = AsyncMock(return_value=rows)

    with patch("wayfinder_paths.mcp.tools.tokens.TOKEN_CLIENT", fake_client):
        out = await onchain_list_tokens(chain_code="base", page=2)

    result = out["result"]
    assert [t["symbol"] for t in result["tokens"]] == [f"T{i}" for i in range(25, 50)]
    assert result["page"] == 2
    assert result["total"] == 60
    assert result["has_next"] is True


@pytest.mark.asyncio
async def test_list_tokens_unknown_chain_code_errors():
    fake_client = AsyncMock()
    fake_client.list_markets = AsyncMock()

    with patch("wayfinder_paths.mcp.tools.tokens.TOKEN_CLIENT", fake_client):
        out = await onchain_list_tokens(chain_code="dogechain")

    assert out["ok"] is False
    assert out["error"]["code"] == "unknown_chain_code"
    fake_client.list_markets.assert_not_awaited()
