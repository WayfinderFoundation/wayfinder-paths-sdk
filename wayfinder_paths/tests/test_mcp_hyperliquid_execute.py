from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.core.constants.hyperliquid import HYPE_FEE_WALLET
from wayfinder_paths.mcp.tools.hyperliquid import (
    _resolve_builder_fee,
    _resolve_perp_asset_id,
    hyperliquid_execute,
)


def test_resolve_builder_fee_rejects_wrong_builder_wallet():
    with pytest.raises(ValueError, match="config builder_fee\\.b must be"):
        _resolve_builder_fee(
            config={"builder_fee": {"b": "0x" + "00" * 20, "f": 10}},
            builder_fee_tenths_bp=None,
        )


def test_resolve_builder_fee_prefers_explicit_fee():
    fee = _resolve_builder_fee(config={}, builder_fee_tenths_bp=7)
    assert fee == {"b": HYPE_FEE_WALLET.lower(), "f": 7}


def test_resolve_perp_asset_id_accepts_coin_and_strips_perp_suffix():
    class StubAdapter:
        coin_to_asset = {"HYPE": 7}

    ok, res = _resolve_perp_asset_id(StubAdapter(), coin="HYPE-perp", asset_id=None)
    assert ok is True
    assert res == 7


@pytest.mark.asyncio
async def test_hyperliquid_execute_withdraw(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WAYFINDER_MCP_STATE_PATH", str(tmp_path / "mcp.sqlite3"))
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path / "runs"))

    wallet = {
        "address": "0x000000000000000000000000000000000000dEaD",
        "private_key_hex": "0x" + "11" * 32,
    }

    with (
        patch(
            "wayfinder_paths.mcp.utils.find_wallet_by_label",
            return_value=wallet,
        ),
        patch("wayfinder_paths.mcp.tools.hyperliquid.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.hyperliquid.HyperliquidAdapter.withdraw",
            new=AsyncMock(return_value=(True, {"status": "ok"})),
        ),
    ):
        out1 = await hyperliquid_execute(
            "withdraw", wallet_label="main", amount_usdc=10
        )
        assert out1["ok"] is True
