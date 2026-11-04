from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wayfinder_paths.mcp.preview import build_onchain_place_order_preview
from wayfinder_paths.mcp.tools.onchain_orders import (
    onchain_cancel_order,
    onchain_list_orders,
    onchain_place_order,
)

TOOLS = "wayfinder_paths.mcp.tools.onchain_orders"
USDC = {
    "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "chain_id": 8453,
    "decimals": 6,
}
WETH = {
    "address": "0x4200000000000000000000000000000000000006",
    "chain_id": 8453,
    "decimals": 18,
}
ME = "0x3C2d70AaE1f84bD41991BFa95decF76c4370698B"


def _adapter(**methods):
    adapter = MagicMock()
    adapter.place_order = AsyncMock(
        return_value=methods.get(
            "place", (True, {"order_id": "oid", "approval_tx_hash": None})
        )
    )
    adapter.cancel_order = AsyncMock(
        return_value=methods.get(
            "cancel", (True, {"order_id": "oid", "status": "cancelled"})
        )
    )
    adapter.list_orders = AsyncMock(return_value=methods.get("list", (True, [])))
    return adapter


async def _place(adapter, *, tokens=(USDC, WETH), **kwargs):
    with (
        patch(f"{TOOLS}._adapter", AsyncMock(return_value=(adapter, ME))),
        patch(
            f"{TOOLS}.TokenResolver.resolve_token_meta",
            AsyncMock(side_effect=list(tokens)),
        ),
        patch(f"{TOOLS}._annotate_profile"),
    ):
        return await onchain_place_order(
            wallet_label="main",
            from_token="usdc-base",
            to_token="weth-base",
            **kwargs,
        )


def _payload(result):
    return result.get("result", result)


@pytest.mark.asyncio
async def test_limit_buy_reaches_the_adapter_with_floored_qty():
    adapter = _adapter()
    result = _payload(
        await _place(
            adapter, order_type="limit_buy", amount="25.1234567", limit_price=2500
        )
    )
    assert result["status"] == "placed"
    assert result["order_id"] == "oid"
    chain_id, request, spent_raw = adapter.place_order.await_args.args
    assert chain_id == 8453
    assert spent_raw == 25_123_456
    assert request["qty"] == "25.123456"  # floored to USDC's 6 decimals
    assert request["side"] == "buy"
    assert request["targetAsset"] == WETH["address"]
    assert request["limitNotionalPrice"] == "2500"


@pytest.mark.asyncio
async def test_trailing_stop_sells_the_from_token():
    adapter = _adapter()
    await _place(
        adapter,
        tokens=(WETH, USDC),
        order_type="trailing_stop",
        amount="0.5",
        trail_percent=3,
    )
    request = adapter.place_order.await_args.args[1]
    assert request["side"] == "sell"
    assert request["targetAsset"] == WETH["address"]
    assert request["trailingStop"] == {"trailPercent": "0.03"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "tokens"),
    [
        ({"order_type": "market", "amount": "1.0"}, (USDC, WETH)),
        (
            {"order_type": "limit_buy", "amount": "1.0", "limit_price": 1},
            (USDC, {**WETH, "chain_id": 1}),
        ),
        (
            {"order_type": "limit_buy", "amount": "1.0", "limit_price": 1},
            ({**USDC, "chain_id": 5000}, {**WETH, "chain_id": 5000}),
        ),
        ({"order_type": "limit_buy", "amount": "1", "limit_price": 1}, (USDC, WETH)),
        ({"order_type": "limit_buy", "amount": "1.0"}, (USDC, WETH)),
        (
            {"order_type": "trailing_stop", "amount": "1.0", "trail_percent": 80},
            (WETH, USDC),
        ),
    ],
)
async def test_bad_requests_never_reach_the_adapter(kwargs, tokens):
    adapter = _adapter()
    result = await _place(adapter, tokens=tokens, **kwargs)
    assert "error" in str(result).lower()
    adapter.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_placement_is_reported():
    adapter = _adapter(
        place=(
            False,
            {"error": "Refusing to sign this order: x", "approval_tx_hash": None},
        )
    )
    result = _payload(
        await _place(adapter, order_type="limit_buy", amount="1.0", limit_price=1)
    )
    assert result["status"] == "failed"
    assert "Refusing" in result["error"]


@pytest.mark.asyncio
async def test_cancel_and_list():
    adapter = _adapter(list=(True, [{"orderId": "oid"}]))
    with patch(f"{TOOLS}._adapter", AsyncMock(return_value=(adapter, ME))):
        cancelled = _payload(
            await onchain_cancel_order(wallet_label="main", order_id="oid")
        )
        listed = _payload(await onchain_list_orders(wallet_label="main"))
    assert cancelled["status"] == "cancelled"
    assert listed["orders"] == [{"orderId": "oid"}]
    adapter.list_orders.assert_awaited_once_with(include_closed=False)


@pytest.mark.asyncio
async def test_preview_spells_out_the_order():
    with patch(
        "wayfinder_paths.mcp.preview._onchain_preview_base",
        AsyncMock(return_value=("HEADER\n", "base", ME)),
    ):
        preview = await build_onchain_place_order_preview(
            {
                "order_type": "bracket",
                "from_token": "weth-base",
                "to_token": "usdc-base",
                "amount": "1.0",
                "take_profit_price": 3500,
                "stop_loss_price": 2800,
            }
        )
    summary = preview["summary"]
    assert "BRACKET (Definitive, rests until it fills)" in summary
    assert "take_profit_price: 3500" in summary
    assert "stop_loss_price: 2800" in summary
    assert "output goes to: sender" in summary
