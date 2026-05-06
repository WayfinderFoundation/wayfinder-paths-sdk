from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.mcp.tools.tokens import (
    onchain_fuzzy_search_tokens,
    onchain_get_gas_token,
    onchain_resolve_token,
)


@pytest.mark.asyncio
async def test_resolve_token_happy_path():
    fake_client = AsyncMock()
    fake_client.get_token_details = AsyncMock(return_value={"symbol": "USDC"})

    with patch("wayfinder_paths.mcp.tools.tokens.TOKEN_CLIENT", fake_client):
        out = await onchain_resolve_token("usd-coin-arbitrum")

    assert out["ok"] is True
    assert out["result"]["token"]["symbol"] == "USDC"


@pytest.mark.asyncio
async def test_get_gas_token_happy_path():
    fake_client = AsyncMock()
    fake_client.get_gas_token = AsyncMock(return_value={"symbol": "ETH"})

    with patch("wayfinder_paths.mcp.tools.tokens.TOKEN_CLIENT", fake_client):
        out = await onchain_get_gas_token("arbitrum")

    assert out["ok"] is True
    assert out["result"]["token"]["symbol"] == "ETH"


@pytest.mark.asyncio
async def test_fuzzy_search_tokens_happy_path():
    fake_client = AsyncMock()
    fake_client.fuzzy_search = AsyncMock(return_value={"results": [{"id": "foo"}]})

    with patch("wayfinder_paths.mcp.tools.tokens.TOKEN_CLIENT", fake_client):
        out = await onchain_fuzzy_search_tokens(chain_code="arbitrum", query="usd")

    assert out["ok"] is True
    assert out["result"]["results"][0]["id"] == "foo"
