from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.mcp.tools.quotes import quote_swap


@pytest.mark.asyncio
async def test_quote_swap_returns_compact_best_quote_by_default():
    fake_wallet = {"address": "0x000000000000000000000000000000000000dEaD"}

    from_meta = {
        "token_id": "ethereum-arbitrum",
        "asset_id": "ethereum",
        "symbol": "ETH",
        "decimals": 18,
        "chain_id": 42161,
        "address": "0x0000000000000000000000000000000000000000",
    }
    to_meta = {
        "token_id": "usd-coin-arbitrum",
        "asset_id": "usd-coin",
        "symbol": "USDC",
        "decimals": 6,
        "chain_id": 42161,
        "address": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
    }

    async def fake_resolve(query: str, *, chain_id: int | None = None):
        _ = chain_id
        if "ethereum" in query.lower() or "eth" in query.lower():
            return from_meta
        return to_meta

    fake_brap = AsyncMock()
    calldata = "0x" + ("ab" * 4096)
    fake_brap.get_quote = AsyncMock(
        return_value={
            "quotes": {
                "quote_count": 3,
                "best_quote": {
                    "provider": "brap_best",
                    "input_amount": "1700000000000000",
                    "output_amount": "1234567",
                    "input_amount_usd": 5.0,
                    "output_amount_usd": 4.99,
                    "gas_estimate": 210000,
                    "fee_estimate": {"total_usd": 0.01},
                    "native_input": True,
                    "native_output": False,
                    "calldata": calldata,
                    "wrap_transaction": None,
                    "unwrap_transaction": None,
                },
                "all_quotes": [
                    {"provider": "brap_best"},
                    {"provider": "brap_alt"},
                    {"provider": "brap_alt"},
                ],
            }
        }
    )

    with (
        patch(
            "wayfinder_paths.mcp.tools.quotes.find_wallet_by_label",
            return_value=fake_wallet,
        ),
        patch(
            "wayfinder_paths.mcp.tools.quotes.TokenResolver.resolve_token_meta",
            new_callable=AsyncMock,
            side_effect=fake_resolve,
        ),
        patch("wayfinder_paths.mcp.tools.quotes.BRAP_CLIENT", fake_brap),
    ):
        out = await quote_swap(
            wallet_label="main",
            from_token="ethereum-arbitrum",
            to_token="usd-coin-arbitrum",
            amount="0.0017",
            slippage_bps=50,
        )

    assert out["ok"] is True
    res = out["result"]
    assert "raw" not in res["quote"]

    best = res["quote"]["best_quote"]
    assert best["provider"] == "brap_best"
    assert best["output_amount"] == "1234567"
    assert best["calldata_len"] > 0
    assert "calldata" not in best
    assert res["quote"]["quote_count"] == 3
    assert res["quote"]["providers"] == ["brap_best", "brap_alt"]


@pytest.mark.asyncio
async def test_quote_swap_can_include_calldata_when_requested():
    fake_wallet = {"address": "0x000000000000000000000000000000000000dEaD"}

    from_meta = {
        "token_id": "ethereum-arbitrum",
        "asset_id": "ethereum",
        "symbol": "ETH",
        "decimals": 18,
        "chain_id": 42161,
        "address": "0x0000000000000000000000000000000000000000",
    }
    to_meta = {
        "token_id": "usd-coin-arbitrum",
        "asset_id": "usd-coin",
        "symbol": "USDC",
        "decimals": 6,
        "chain_id": 42161,
        "address": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
    }

    async def fake_resolve(query: str, *, chain_id: int | None = None):
        _ = chain_id
        if "ethereum" in query.lower() or "eth" in query.lower():
            return from_meta
        return to_meta

    calldata = "0x" + ("cd" * 1024)
    fake_brap = AsyncMock()
    fake_brap.get_quote = AsyncMock(
        return_value={
            "quotes": {
                "quote_count": 1,
                "best_quote": {
                    "provider": "brap_best",
                    "output_amount": "1",
                    "calldata": calldata,
                },
                "all_quotes": [{"provider": "brap_best"}],
            }
        }
    )

    with (
        patch(
            "wayfinder_paths.mcp.tools.quotes.find_wallet_by_label",
            return_value=fake_wallet,
        ),
        patch(
            "wayfinder_paths.mcp.tools.quotes.TokenResolver.resolve_token_meta",
            new_callable=AsyncMock,
            side_effect=fake_resolve,
        ),
        patch("wayfinder_paths.mcp.tools.quotes.BRAP_CLIENT", fake_brap),
    ):
        out = await quote_swap(
            wallet_label="main",
            from_token="ethereum-arbitrum",
            to_token="usd-coin-arbitrum",
            amount="0.0017",
            slippage_bps=50,
            include_calldata=True,
        )

    assert out["ok"] is True
    best = out["result"]["quote"]["best_quote"]
    assert best["calldata"] == calldata


@pytest.mark.asyncio
async def test_quote_swap_accepts_top_level_brap_shape():
    fake_wallet = {"address": "0x000000000000000000000000000000000000dEaD"}

    from_meta = {
        "token_id": "usd-coin-arbitrum",
        "asset_id": "usd-coin",
        "symbol": "USDC",
        "decimals": 6,
        "chain_id": 42161,
        "address": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
    }
    to_meta = {
        "token_id": "tether-arbitrum",
        "asset_id": "tether",
        "symbol": "USDT",
        "decimals": 6,
        "chain_id": 42161,
        "address": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
    }

    async def fake_resolve(query: str, *, chain_id: int | None = None):
        _ = chain_id
        if "tether" in query.lower() or "usdt" in query.lower():
            return to_meta
        return from_meta

    fake_brap = AsyncMock()
    fake_brap.get_quote = AsyncMock(
        return_value={
            "quotes": [{"provider": "brap_best"}, {"provider": "brap_alt"}],
            "best_quote": {
                "provider": "brap_best",
                "output_amount": "1",
                "calldata": "0xabc",
            },
        }
    )

    with (
        patch(
            "wayfinder_paths.mcp.tools.quotes.find_wallet_by_label",
            return_value=fake_wallet,
        ),
        patch(
            "wayfinder_paths.mcp.tools.quotes.TokenResolver.resolve_token_meta",
            new_callable=AsyncMock,
            side_effect=fake_resolve,
        ),
        patch("wayfinder_paths.mcp.tools.quotes.BRAP_CLIENT", fake_brap),
    ):
        out = await quote_swap(
            wallet_label="main",
            from_token="usd-coin-arbitrum",
            to_token="tether-arbitrum",
            amount="1",
        )

    assert out["ok"] is True
    assert out["result"]["quote"]["quote_count"] == 2
    assert out["result"]["quote"]["providers"] == ["brap_best", "brap_alt"]
