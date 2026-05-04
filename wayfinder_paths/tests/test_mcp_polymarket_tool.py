from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.mcp.tools.polymarket import polymarket, polymarket_execute

_FIND_WALLET = "wayfinder_paths.mcp.utils.find_wallet_by_label"
_GET_SIGN_CB = "wayfinder_paths.mcp.tools.polymarket.get_wallet_signing_callback"
_GET_HASH_CB = "wayfinder_paths.mcp.tools.polymarket.get_wallet_sign_hash_callback"

_ADDR = "0x000000000000000000000000000000000000dEaD"
_WALLET = {"address": _ADDR, "private_key_hex": "0x" + "11" * 32}
_SIGN_CB = AsyncMock(return_value=b"\x00" * 65)
_HASH_CB = AsyncMock(return_value="0x" + "00" * 65)


@pytest.mark.asyncio
async def test_polymarket_status_uses_adapter_full_state():
    with (
        patch(_FIND_WALLET, AsyncMock(return_value=_WALLET)),
        patch(_GET_SIGN_CB, AsyncMock(return_value=(_SIGN_CB, _ADDR))),
        patch(_GET_HASH_CB, AsyncMock(return_value=(_HASH_CB, _ADDR))),
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_full_user_state",
            new=AsyncMock(return_value=(True, {"protocol": "polymarket"})),
        ),
    ):
        out = await polymarket("status", wallet_label="main")
        assert out["ok"] is True
        assert out["result"]["action"] == "status"
        assert out["result"]["ok"] is True
        assert out["result"]["state"]["protocol"] == "polymarket"


@pytest.mark.asyncio
async def test_polymarket_search_uses_adapter_search():
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.search_markets_fuzzy",
            new=AsyncMock(return_value=(True, [{"slug": "m1"}])),
        ),
    ):
        out = await polymarket("search", query="bitcoin", limit=1)
        assert out["ok"] is True
        assert out["result"]["action"] == "search"
        assert out["result"]["markets"][0]["slug"] == "m1"


@pytest.mark.asyncio
async def test_polymarket_quote_uses_adapter_quote_by_token_id():
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.quote_market_order",
            new=AsyncMock(
                return_value=(
                    True,
                    {
                        "token_id": "tok_yes",
                        "side": "BUY",
                        "average_price": 0.42,
                        "shares": 10.0,
                    },
                )
            ),
        ),
    ):
        out = await polymarket(
            "quote",
            token_id="tok_yes",
            side="BUY",
            amount_collateral=4.2,
        )
        assert out["ok"] is True
        assert out["result"]["action"] == "quote"
        assert out["result"]["token_id"] == "tok_yes"
        assert out["result"]["quote"]["average_price"] == 0.42


@pytest.mark.asyncio
async def test_polymarket_quote_uses_adapter_quote_by_market_slug():
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.quote_prediction",
            new=AsyncMock(
                return_value=(
                    True,
                    {
                        "token_id": "tok_yes",
                        "side": "SELL",
                        "average_price": 0.61,
                        "shares": 3.0,
                    },
                )
            ),
        ),
    ):
        out = await polymarket(
            "quote",
            market_slug="market-slug",
            outcome="YES",
            side="SELL",
            shares=3.0,
        )
        assert out["ok"] is True
        assert out["result"]["action"] == "quote"
        assert out["result"]["side"] == "SELL"
        assert out["result"]["quote"]["average_price"] == 0.61


@pytest.mark.asyncio
async def test_polymarket_quote_buy_requires_amount_collateral():
    with patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}):
        out = await polymarket("quote", token_id="tok_yes", side="BUY")
        assert out["ok"] is False
        assert out["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_polymarket_quote_sell_requires_shares():
    with patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}):
        out = await polymarket("quote", token_id="tok_yes", side="SELL")
        assert out["ok"] is False
        assert out["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_polymarket_quote_surfaces_adapter_failure():
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.quote_market_order",
            new=AsyncMock(return_value=(False, "order book unavailable")),
        ),
    ):
        out = await polymarket(
            "quote",
            token_id="tok_yes",
            side="BUY",
            amount_collateral=4.2,
        )
        assert out["ok"] is False
        assert out["error"]["code"] == "error"
        assert "order book unavailable" in out["error"]["message"]


@pytest.mark.asyncio
async def test_polymarket_execute_bridge_deposit(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path / "runs"))

    with (
        patch(_FIND_WALLET, AsyncMock(return_value=_WALLET)),
        patch(_GET_SIGN_CB, AsyncMock(return_value=(_SIGN_CB, _ADDR))),
        patch(_GET_HASH_CB, AsyncMock(return_value=(_HASH_CB, _ADDR))),
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.bridge_deposit",
            new=AsyncMock(return_value=(True, {"tx_hash": "0xabc"})),
        ),
    ):
        out = await polymarket_execute(
            "bridge_deposit",
            wallet_label="main",
            amount=1.0,
        )
        assert out["ok"] is True
        assert out["result"]["status"] == "confirmed"
        assert out["result"]["action"] == "bridge_deposit"
        effects = out["result"]["effects"]
        assert effects and effects[0]["label"] == "bridge_deposit"


@pytest.mark.asyncio
async def test_polymarket_execute_buy_market_order(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path / "runs"))

    with (
        patch(_FIND_WALLET, AsyncMock(return_value=_WALLET)),
        patch(_GET_SIGN_CB, AsyncMock(return_value=(_SIGN_CB, _ADDR))),
        patch(_GET_HASH_CB, AsyncMock(return_value=(_HASH_CB, _ADDR))),
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.place_prediction",
            new=AsyncMock(return_value=(True, {"status": "matched"})),
        ),
    ):
        out = await polymarket_execute(
            "buy",
            wallet_label="main",
            market_slug="bitcoin-above-70k-on-february-9",
            outcome="YES",
            amount_collateral=2.0,
        )
        assert out["ok"] is True
        assert out["result"]["status"] == "confirmed"
        assert out["result"]["action"] == "buy"
        effects = out["result"]["effects"]
        assert effects and effects[0]["label"] == "buy"
