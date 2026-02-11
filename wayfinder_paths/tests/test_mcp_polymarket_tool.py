from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.mcp.tools.polymarket import polymarket, polymarket_execute


@pytest.mark.asyncio
async def test_polymarket_status_uses_adapter_full_state():
    wallet = {"address": "0x000000000000000000000000000000000000dEaD"}

    with (
        patch(
            "wayfinder_paths.mcp.tools.polymarket.find_wallet_by_label",
            return_value=wallet,
        ),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.load_config_json",
            return_value={},
        ),
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
        patch(
            "wayfinder_paths.mcp.tools.polymarket.load_config_json",
            return_value={},
        ),
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
async def test_polymarket_execute_bridge_deposit(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path / "runs"))

    wallet = {
        "address": "0x000000000000000000000000000000000000dEaD",
        "private_key_hex": "0x" + "11" * 32,
    }

    with (
        patch(
            "wayfinder_paths.mcp.tools.polymarket.find_wallet_by_label",
            return_value=wallet,
        ),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.load_config_json",
            return_value={},
        ),
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

    wallet = {
        "address": "0x000000000000000000000000000000000000dEaD",
        "private_key_hex": "0x" + "11" * 32,
    }

    with (
        patch(
            "wayfinder_paths.mcp.tools.polymarket.find_wallet_by_label",
            return_value=wallet,
        ),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.load_config_json",
            return_value={},
        ),
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
            amount_usdc=2.0,
        )
        assert out["ok"] is True
        assert out["result"]["status"] == "confirmed"
        assert out["result"]["action"] == "buy"
        effects = out["result"]["effects"]
        assert effects and effects[0]["label"] == "buy"
