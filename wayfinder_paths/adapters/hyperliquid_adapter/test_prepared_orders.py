import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from hyperliquid.utils.signing import get_l1_action_payload

from wayfinder_paths.adapters.hyperliquid_adapter import prepared_orders as module
from wayfinder_paths.adapters.hyperliquid_adapter.prepared_orders import (
    IocNotSubmitted,
    PerpIocOrder,
    submit_prepared_ioc,
)
from wayfinder_paths.adapters.hyperliquid_adapter.utils import round_order_price
from wayfinder_paths.core.constants.hyperliquid import DEFAULT_HYPERLIQUID_BUILDER_FEE


@pytest.fixture
def submit_args(monkeypatch):
    monkeypatch.setattr(module, "get_timestamp_ms", lambda: 1_000_000)
    return {
        "orders": (
            PerpIocOrder(1, Decimal("0.02"), Decimal("2000")),
            PerpIocOrder(0, Decimal("-0.001"), Decimal("60000")),
        ),
        "cloids": ("0x" + "01" * 16, "0x" + "02" * 16),
        "reduce_only": False,
        "expires_after": 1_030_000,
        "sign": AsyncMock(return_value={"r": "0x01", "s": "0x02", "v": 27}),
        "post_exchange": AsyncMock(return_value={"status": "ok"}),
    }


@pytest.mark.asyncio
async def test_submits_exact_terms_once_with_signed_expiry_and_builder(submit_args):
    result = await submit_prepared_ioc(**submit_args)
    assert result == {"status": "ok"}
    submit_args["post_exchange"].assert_awaited_once()
    payload = submit_args["post_exchange"].call_args.args[0]
    assert payload["expiresAfter"] == submit_args["expires_after"]
    assert payload["nonce"] == 1_000_000
    assert payload["action"]["builder"] == {
        **DEFAULT_HYPERLIQUID_BUILDER_FEE,
        "b": DEFAULT_HYPERLIQUID_BUILDER_FEE["b"].lower(),
    }
    assert payload["action"]["orders"] == [
        {
            "a": 1,
            "b": True,
            "p": "2000",
            "s": "0.02",
            "r": False,
            "t": {"limit": {"tif": "Ioc"}},
            "c": submit_args["cloids"][0],
        },
        {
            "a": 0,
            "b": False,
            "p": "60000",
            "s": "0.001",
            "r": False,
            "t": {"limit": {"tif": "Ioc"}},
            "c": submit_args["cloids"][1],
        },
    ]
    submit_args["sign"].assert_awaited_once_with(
        get_l1_action_payload(
            payload["action"], None, payload["nonce"], payload["expiresAfter"], True
        )
    )
    assert payload["action"]["grouping"] == "na"  # Not a falsely atomic/TP-SL group.


@pytest.mark.asyncio
async def test_reduce_only_is_applied_to_both_legs(submit_args):
    await submit_prepared_ioc(**{**submit_args, "reduce_only": True})
    orders = submit_args["post_exchange"].call_args.args[0]["action"]["orders"]
    assert all(order["r"] for order in orders)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"expires_after": 1_000_000},
        {"expires_after": 1_030_001},
        {"expires_after": True},
        {"reduce_only": 1},
        {"orders": ()},
        {"cloids": ("0x" + "01" * 16,)},
        {"cloids": ("0x" + "01" * 16, "0x" + "01" * 16)},
        {"cloids": ("0xinvalid", "0x" + "01" * 16)},
    ],
)
async def test_invalid_batch_never_signs_or_submits(submit_args, overrides):
    with pytest.raises(IocNotSubmitted):
        await submit_prepared_ioc(**{**submit_args, **overrides})
    submit_args["sign"].assert_not_awaited()
    submit_args["post_exchange"].assert_not_awaited()


@pytest.mark.asyncio
async def test_signing_failure_is_distinct_from_uncertain_transport(submit_args):
    submit_args["sign"].side_effect = RuntimeError("session expired")
    with pytest.raises(IocNotSubmitted):
        await submit_prepared_ioc(**submit_args)
    submit_args["post_exchange"].assert_not_awaited()


@pytest.mark.asyncio
async def test_expiry_during_signing_does_not_broadcast(submit_args, monkeypatch):
    times = iter((1_000_000, 1_030_000))
    monkeypatch.setattr(module, "get_timestamp_ms", lambda: next(times))
    with pytest.raises(IocNotSubmitted):
        await submit_prepared_ioc(**submit_args)
    submit_args["sign"].assert_awaited_once()
    submit_args["post_exchange"].assert_not_awaited()


@pytest.mark.asyncio
async def test_transport_timeout_is_not_misreported_as_unsent_or_retried(submit_args):
    submit_args["post_exchange"].side_effect = TimeoutError("response lost")
    with pytest.raises(TimeoutError):
        await submit_prepared_ioc(**submit_args)
    submit_args["post_exchange"].assert_awaited_once()


@pytest.mark.asyncio
async def test_cancellation_after_dispatch_remains_uncertain(submit_args):
    submit_args["post_exchange"].side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await submit_prepared_ioc(**submit_args)
    submit_args["post_exchange"].assert_awaited_once()


@pytest.mark.parametrize(
    "asset,size,price",
    [
        (True, "1", "1"),
        (10000, "1", "1"),
        (1, "0", "1"),
        (1, "NaN", "1"),
        (1, "1", "Infinity"),
        (1, "1", "0"),
    ],
)
def test_invalid_orders_rejected(asset, size, price):
    with pytest.raises(ValueError):
        PerpIocOrder(asset, Decimal(size), Decimal(price))


@pytest.mark.parametrize(
    "price,decimals,down,up",
    [
        (12345.678, 3, 12345, 12346),
        (0.123456, 6, 0.12345, 0.12346),
        (0.123456, 3, 0.123, 0.124),
        (1234567.0, 0, 1234567.0, 1234567.0),
        (0, 6, 0, 0),
    ],
)
def test_tick_rounding_preserves_each_side_of_slippage_bound(price, decimals, down, up):
    assert round_order_price(price, decimals) == down
    assert round_order_price(price, decimals, round_up=True) == up
