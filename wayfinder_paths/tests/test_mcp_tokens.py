from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.mcp.resources.tokens import (
    fuzzy_search_tokens,
    fuzzy_search_tokens_full,
    get_gas_token,
    resolve_token,
)


@pytest.mark.asyncio
async def test_resolve_token_happy_path():
    fake_client = AsyncMock()
    fake_client.get_token_details = AsyncMock(
        return_value={
            "symbol": "USDC",
            "id": "usd-coin-arbitrum",
            "name": "USD Coin",
            "chain": "arbitrum",
            "address": "0x1",
            "decimals": 6,
        }
    )

    with patch("wayfinder_paths.mcp.resources.tokens.TOKEN_CLIENT", fake_client):
        out = await resolve_token("usd-coin-arbitrum")

    result = json.loads(out)
    assert result["token"]["symbol"] == "USDC"
    assert "decimals" not in result["token"]


@pytest.mark.asyncio
async def test_get_gas_token_happy_path():
    fake_client = AsyncMock()
    fake_client.get_gas_token = AsyncMock(
        return_value={"symbol": "ETH", "id": "eth", "chain": "arbitrum"}
    )

    with patch("wayfinder_paths.mcp.resources.tokens.TOKEN_CLIENT", fake_client):
        out = await get_gas_token("arbitrum")

    result = json.loads(out)
    assert result["token"]["symbol"] == "ETH"


@pytest.mark.asyncio
async def test_fuzzy_search_tokens_returns_compact_top_results():
    fake_client = AsyncMock()
    fake_client.fuzzy_search = AsyncMock(
        return_value={
            "results": [
                {
                    "id": f"foo-{idx}",
                    "symbol": f"T{idx}",
                    "name": f"Token {idx}",
                    "chain": "arbitrum",
                    "address": f"0x{idx}",
                    "decimals": 18,
                }
                for idx in range(7)
            ]
        }
    )

    with patch("wayfinder_paths.mcp.resources.tokens.TOKEN_CLIENT", fake_client):
        out = await fuzzy_search_tokens(chain_code="arbitrum", query="usd")

    result = json.loads(out)
    assert result["result_count"] == 7
    assert len(result["results"]) == 5
    assert result["results"][0]["id"] == "foo-0"
    assert "decimals" not in result["results"][0]


@pytest.mark.asyncio
async def test_fuzzy_search_tokens_full_returns_original_payload():
    fake_client = AsyncMock()
    fake_client.fuzzy_search = AsyncMock(return_value={"results": [{"id": "foo"}]})

    with patch("wayfinder_paths.mcp.resources.tokens.TOKEN_CLIENT", fake_client):
        out = await fuzzy_search_tokens_full(chain_code="arbitrum", query="usd")

    result = json.loads(out)
    assert result["results"][0]["id"] == "foo"
