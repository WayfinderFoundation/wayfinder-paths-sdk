from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.mcp.resources.tokens import (
    fuzzy_search_tokens,
    get_gas_token,
    resolve_token,
)


@pytest.mark.asyncio
async def test_resolve_token_happy_path():
    fake_client = AsyncMock()
    fake_client.get_token_details = AsyncMock(return_value={"symbol": "USDC"})

    with patch("wayfinder_paths.mcp.resources.tokens.TOKEN_CLIENT", fake_client):
        out = await resolve_token("usd-coin-arbitrum")

    result = json.loads(out)
    assert result["token"]["symbol"] == "USDC"


@pytest.mark.asyncio
async def test_get_gas_token_happy_path():
    fake_client = AsyncMock()
    fake_client.get_gas_token = AsyncMock(return_value={"symbol": "ETH"})

    with patch("wayfinder_paths.mcp.resources.tokens.TOKEN_CLIENT", fake_client):
        out = await get_gas_token("arbitrum")

    result = json.loads(out)
    assert result["token"]["symbol"] == "ETH"


@pytest.mark.asyncio
async def test_fuzzy_search_tokens_happy_path():
    fake_client = AsyncMock()
    fake_client.fuzzy_search = AsyncMock(return_value={"results": [{"id": "foo"}]})

    with patch("wayfinder_paths.mcp.resources.tokens.TOKEN_CLIENT", fake_client):
        out = await fuzzy_search_tokens(chain_code="arbitrum", query="usd")

    result = json.loads(out)
    assert result["results"][0]["id"] == "foo"
