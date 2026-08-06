from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.mcp.tools.quotes import onchain_quote_swap

EVM_ADDRESS = "0x000000000000000000000000000000000000dEaD"
SVM_ADDRESS = "BTXGZD6APaEPLUnELUT3Q1HWUYaWatu42WXT3YCU1vxY"


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
    calldata = {"data": "0x" + ("ab" * 4096)}
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
                    "safety_warnings": [{"code": "output_market_data_unavailable"}],
                    "output_validation": {"identity": {"suspicious": False}},
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
            "wayfinder_paths.mcp.tools.quotes.load_wallet_ring",
            return_value=[fake_wallet],
        ),
        patch(
            "wayfinder_paths.mcp.tools.quotes.TokenResolver.resolve_token_meta",
            new_callable=AsyncMock,
            side_effect=fake_resolve,
        ),
        patch("wayfinder_paths.mcp.tools.quotes.BRAP_CLIENT", fake_brap),
    ):
        out = await onchain_quote_swap(
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
    assert best["safety_warnings"][0]["code"] == "output_market_data_unavailable"
    assert best["output_validation"]["identity"]["suspicious"] is False
    assert fake_brap.get_quote.await_args.kwargs["allow_unverified_output"] is False


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

    calldata = {"data": "0x" + ("cd" * 1024)}
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
            "wayfinder_paths.mcp.tools.quotes.load_wallet_ring",
            return_value=[fake_wallet],
        ),
        patch(
            "wayfinder_paths.mcp.tools.quotes.TokenResolver.resolve_token_meta",
            new_callable=AsyncMock,
            side_effect=fake_resolve,
        ),
        patch("wayfinder_paths.mcp.tools.quotes.BRAP_CLIENT", fake_brap),
    ):
        out = await onchain_quote_swap(
            wallet_label="main",
            from_token="ethereum-arbitrum",
            to_token="usd-coin-arbitrum",
            amount="0.0017",
            slippage_bps=50,
            include_calldata=True,
            allow_unverified_output=True,
        )

    assert out["ok"] is True
    best = out["result"]["quote"]["best_quote"]
    assert best["calldata"] == calldata["data"]
    assert out["result"]["suggested_swap_request"]["allow_unverified_output"] is True
    assert fake_brap.get_quote.await_args.kwargs["allow_unverified_output"] is True


@pytest.mark.asyncio
async def test_quote_swap_surfaces_backend_safety_rejection():
    token_meta = {
        "token_id": "token-base",
        "symbol": "TOKEN",
        "decimals": 18,
        "chain_id": 8453,
        "address": "0x1111111111111111111111111111111111111111",
    }
    fake_brap = AsyncMock()
    fake_brap.get_quote = AsyncMock(
        return_value={
            "quotes": [],
            "best_quote": {},
            "errors": [{"error": "unverified_protected_output"}],
        }
    )

    with (
        patch(
            "wayfinder_paths.mcp.tools.quotes.load_wallet_ring",
            return_value=[{"address": EVM_ADDRESS}],
        ),
        patch(
            "wayfinder_paths.mcp.tools.quotes.TokenResolver.resolve_token_meta",
            new=AsyncMock(return_value=token_meta),
        ),
        patch("wayfinder_paths.mcp.tools.quotes.BRAP_CLIENT", fake_brap),
    ):
        out = await onchain_quote_swap(
            wallet_label="main",
            from_token="from",
            to_token="to",
            amount="1.0",
        )

    assert out["ok"] is False
    assert out["error"]["code"] == "quote_rejected"
    assert out["error"]["details"]["errors"] == [
        {"error": "unverified_protected_output"}
    ]


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
                "calldata": {"data": "0xabc"},
            },
        }
    )

    with (
        patch(
            "wayfinder_paths.mcp.tools.quotes.load_wallet_ring",
            return_value=[fake_wallet],
        ),
        patch(
            "wayfinder_paths.mcp.tools.quotes.TokenResolver.resolve_token_meta",
            new_callable=AsyncMock,
            side_effect=fake_resolve,
        ),
        patch("wayfinder_paths.mcp.tools.quotes.BRAP_CLIENT", fake_brap),
    ):
        out = await onchain_quote_swap(
            wallet_label="main",
            from_token="usd-coin-arbitrum",
            to_token="tether-arbitrum",
            amount="1.0",
        )

    assert out["ok"] is True
    assert out["result"]["quote"]["quote_count"] == 2
    assert out["result"]["quote"]["providers"] == ["brap_best", "brap_alt"]


@pytest.mark.asyncio
async def test_quote_swap_passes_destination_ring_leg_to_brap():
    ring = [
        {
            "address": EVM_ADDRESS,
            "label": "main",
            "type": "remote",
            "chain_type": "ethereum",
        },
        {
            "address": SVM_ADDRESS,
            "label": "main",
            "type": "remote",
            "chain_type": "solana",
        },
    ]
    from_meta = {
        "token_id": "usd-coin-base",
        "symbol": "USDC",
        "decimals": 6,
        "chain_id": 8453,
        "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    }
    to_meta = {
        "token_id": "solana_EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "symbol": "USDC",
        "decimals": 6,
        "chain_id": 900,
        "address": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    }

    async def fake_resolve(query: str, *, chain_id: int | None = None):
        _ = chain_id
        return from_meta if query == "from" else to_meta

    fake_brap = AsyncMock()
    fake_brap.get_quote = AsyncMock(
        return_value={
            "quotes": [{"provider": "lifi"}],
            "best_quote": {
                "provider": "lifi",
                "output_amount": "990000",
                "calldata": {"data": "0xabc"},
            },
        }
    )

    with (
        patch(
            "wayfinder_paths.mcp.tools.quotes.load_wallet_ring",
            new=AsyncMock(return_value=ring),
        ),
        patch(
            "wayfinder_paths.mcp.tools.quotes.TokenResolver.resolve_token_meta",
            new_callable=AsyncMock,
            side_effect=fake_resolve,
        ),
        patch("wayfinder_paths.mcp.tools.quotes.BRAP_CLIENT", fake_brap),
    ):
        out = await onchain_quote_swap(
            wallet_label="main",
            from_token="from",
            to_token="to",
            amount="1.0",
        )

    assert out["ok"] is True
    assert out["result"]["suggested_swap_request"]["recipient"] == SVM_ADDRESS
    assert fake_brap.get_quote.await_args.kwargs["from_wallet"] == EVM_ADDRESS
    assert fake_brap.get_quote.await_args.kwargs["to_wallet"] == SVM_ADDRESS
