from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wayfinder_paths.core.constants.hyperliquid import (
    MARKET_TYPE_PERP,
    MARKET_TYPE_SPOT,
)
from wayfinder_paths.mcp.preview import build_hyperliquid_place_trailing_stop_preview
from wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_place_trailing_stop

TOOLS = "wayfinder_paths.mcp.tools.hyperliquid"


def _adapter(oid: int | None = 9):
    adapter = MagicMock()
    adapter.get_valid_order_size = lambda asset_id, size: round(size, 2)
    adapter.get_sz_decimals = lambda asset_id: 2
    data = {"oid": oid} if oid is not None else {"error": "Retracement out of range"}
    adapter.place_trailing_stop = AsyncMock(
        return_value=(
            oid is not None,
            {"status": "ok", "response": {"type": "trailingStop", "data": data}},
        )
    )
    return adapter


async def _call(adapter, market_type=MARKET_TYPE_PERP, **kwargs):
    with (
        patch(f"{TOOLS}._make_hl_adapter", AsyncMock(return_value=(adapter, "0xme"))),
        patch(f"{TOOLS}._resolve_asset", AsyncMock(return_value=(5, market_type))),
        patch(f"{TOOLS}._validate_price"),
        patch(f"{TOOLS}._annotate_hl_profile"),
    ):
        return await hyperliquid_place_trailing_stop(
            wallet_label="main", asset_name="ETH-USDC", **kwargs
        )


def _payload(result):
    return result.get("result", result)


@pytest.mark.asyncio
async def test_places_a_pct_trailing_stop_and_returns_the_oid():
    adapter = _adapter(oid=42)
    result = _payload(
        await _call(
            adapter,
            is_buy=False,
            size=1.234,
            retracement_pct=2.0,
            activation_price=4000.0,
        )
    )
    assert result["status"] == "confirmed"
    assert result["trailing_stop"]["oid"] == 42
    args, kwargs = adapter.place_trailing_stop.await_args
    assert args == (5, False, 1.23, "0xme")
    assert kwargs == {
        "retracement_pct": 2.0,
        "retracement_px": None,
        "activation_price": 4000.0,
        "reduce_only": True,
    }


@pytest.mark.asyncio
async def test_hl_rejection_is_reported_as_failed():
    result = _payload(
        await _call(_adapter(oid=None), is_buy=True, size=1, retracement_px=50)
    )
    assert result["status"] == "failed"
    assert result["trailing_stop"]["oid"] is None


@pytest.mark.asyncio
async def test_spot_markets_are_rejected_before_placing():
    adapter = _adapter()
    result = await _call(
        adapter, market_type=MARKET_TYPE_SPOT, is_buy=False, size=1, retracement_pct=1
    )
    assert "perp-only" in str(result)
    adapter.place_trailing_stop.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"is_buy": False, "size": 1},
        {"is_buy": False, "size": 1, "retracement_pct": 1, "retracement_px": 5},
        {"is_buy": False, "size": 1, "retracement_pct": 100},
        {"is_buy": False, "size": 1, "retracement_px": -5},
        {"is_buy": False, "size": 0, "retracement_pct": 1},
        {"is_buy": False, "size": 1, "retracement_pct": 1, "activation_price": 0},
    ],
)
async def test_bad_inputs_never_reach_the_adapter(kwargs):
    adapter = _adapter()
    result = await _call(adapter, **kwargs)
    assert "error" in str(result).lower()
    adapter.place_trailing_stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_spells_out_the_trailing_stop():
    with patch(
        "wayfinder_paths.mcp.preview._hl_preview_base",
        AsyncMock(return_value=("HEADER\n", "base")),
    ):
        preview = await build_hyperliquid_place_trailing_stop_preview(
            {"is_buy": False, "size": 1, "retracement_pct": 2.5}
        )
    summary = preview["summary"]
    assert "TRAILING STOP (2.5% pullback)" in summary
    assert "none (trails now)" in summary


@pytest.mark.asyncio
async def test_action_level_rejection_is_reported_not_raised():
    adapter = _adapter()
    adapter.place_trailing_stop = AsyncMock(
        return_value=(
            False,
            {"status": "err", "response": "Trailing stop retracement is invalid."},
        )
    )
    result = _payload(await _call(adapter, is_buy=False, size=1, retracement_pct=1))
    assert result["status"] == "failed"
    assert result["trailing_stop"]["oid"] is None
