from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from wayfinder_paths.mcp.tools.tokens import onchain_token_holder_intel

TOKEN = "0x" + "ab" * 20


def _blob() -> dict:
    return {
        "chain": "base",
        "holder_pnl": {"mean_pnl_pct": 42.1, "pct_profitable": 61.0},
        "hold_time": {"weighted_avg_hold_hours": 311.2},
        "whale_entry": {"vwap_entry_price_usd": 0.0045, "cohort_pnl_pct": 173.3},
        "coverage": {"swap_coverage": "full", "pnl_coverage_pct": 74.4},
        "holder_stats": {"totalHolders": 812},
    }


@pytest.mark.asyncio
async def test_holder_intel_happy_path():
    fake_client = AsyncMock()
    fake_client.get_holder_intel = AsyncMock(return_value=_blob())

    with patch("wayfinder_paths.mcp.tools.tokens.TOKEN_CLIENT", fake_client):
        out = await onchain_token_holder_intel("base", TOKEN)

    assert out["ok"] is True
    assert out["result"]["holder_pnl"]["mean_pnl_pct"] == 42.1
    assert out["result"]["coverage"]["swap_coverage"] == "full"
    fake_client.get_holder_intel.assert_awaited_once_with(8453, TOKEN, False)


@pytest.mark.asyncio
async def test_holder_intel_refresh_is_passed_through():
    fake_client = AsyncMock()
    fake_client.get_holder_intel = AsyncMock(return_value=_blob())

    with patch("wayfinder_paths.mcp.tools.tokens.TOKEN_CLIENT", fake_client):
        out = await onchain_token_holder_intel("ethereum", TOKEN, refresh=True)

    assert out["ok"] is True
    fake_client.get_holder_intel.assert_awaited_once_with(1, TOKEN, True)


@pytest.mark.asyncio
async def test_holder_intel_rejects_unsupported_chain():
    fake_client = AsyncMock()

    # robinhood is a valid platform chain but not Moralis-indexed.
    with patch("wayfinder_paths.mcp.tools.tokens.TOKEN_CLIENT", fake_client):
        out = await onchain_token_holder_intel("robinhood", TOKEN)

    assert out["ok"] is False
    assert out["error"]["code"] == "unsupported_chain"
    assert "base" in out["error"]["details"]["valid"]
    fake_client.get_holder_intel.assert_not_awaited()


def _status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request(
        "GET",
        "https://strategies-dev.wayfinder.ai/api/v1/blockchain/token-intel/holders/",
    )
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


@pytest.mark.asyncio
async def test_holder_intel_maps_backend_statuses_without_leaking_urls():
    for status_code, code in ((404, "token_not_found"), (503, "not_configured")):
        fake_client = AsyncMock()
        fake_client.get_holder_intel = AsyncMock(side_effect=_status_error(status_code))
        with patch("wayfinder_paths.mcp.tools.tokens.TOKEN_CLIENT", fake_client):
            out = await onchain_token_holder_intel("base", TOKEN)
        assert out["ok"] is False
        assert out["error"]["code"] == code
        assert "strategies-dev.wayfinder.ai" not in str(out["error"])
