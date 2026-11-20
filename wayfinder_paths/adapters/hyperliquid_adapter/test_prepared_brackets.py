"""Native brackets must keep exact terms and never duplicate an uncertain entry."""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from hyperliquid.utils.signing import get_l1_action_payload

from wayfinder_paths.adapters.hyperliquid_adapter import prepared_orders as module
from wayfinder_paths.adapters.hyperliquid_adapter.prepared_orders import (
    BracketNotSubmitted,
    PerpIocOrder,
    submit_prepared_bracket,
)
from wayfinder_paths.core.constants.hyperliquid import DEFAULT_HYPERLIQUID_BUILDER_FEE


@pytest.fixture
def bracket_args(monkeypatch):
    monkeypatch.setattr(module, "get_timestamp_ms", lambda: 1_000_000)
    return {
        "entry": PerpIocOrder(1, Decimal("0.02"), Decimal("2000")),
        "take_profit_price": Decimal("2020"),
        "stop_loss_price": Decimal("1980"),
        "cloids": tuple("0x" + digit * 32 for digit in "123"),
        "expires_after": 1_030_000,
        "sign": AsyncMock(return_value={"r": "0x01", "s": "0x02", "v": 27}),
        "post_exchange": AsyncMock(return_value={"status": "ok"}),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("asset_id", [0, 1, 110012, 120001])
@pytest.mark.parametrize("is_long", [True, False])
async def test_bracket_submits_three_persisted_ids_and_native_reduce_only_exits(
    bracket_args, asset_id, is_long
):
    size = Decimal("0.02") if is_long else Decimal("-0.02")
    take, stop = (Decimal("2020"), Decimal("1980"))
    if not is_long:
        take, stop = stop, take
    args = {
        **bracket_args,
        "entry": PerpIocOrder(asset_id, size, Decimal("2000")),
        "take_profit_price": take,
        "stop_loss_price": stop,
    }
    await submit_prepared_bracket(**args)
    args["post_exchange"].assert_awaited_once()
    payload = args["post_exchange"].call_args.args[0]
    assert payload["nonce"] == 1_000_000
    assert payload["expiresAfter"] == 1_030_000
    action = payload["action"]
    assert action["grouping"] == "normalTpsl"
    assert action["builder"] == {
        **DEFAULT_HYPERLIQUID_BUILDER_FEE,
        "b": DEFAULT_HYPERLIQUID_BUILDER_FEE["b"].lower(),
    }
    assert action["orders"] == [
        {
            "a": asset_id,
            "b": is_long,
            "p": "2000",
            "s": "0.02",
            "r": False,
            "t": {"limit": {"tif": "Ioc"}},
            "c": args["cloids"][0],
        },
        *[
            {
                "a": asset_id,
                "b": not is_long,
                "p": str(price),
                "s": "0.02",
                "r": True,
                "t": {
                    "trigger": {"isMarket": True, "triggerPx": str(price), "tpsl": tpsl}
                },
                "c": cloid,
            }
            for price, tpsl, cloid in (
                (take, "tp", args["cloids"][1]),
                (stop, "sl", args["cloids"][2]),
            )
        ],
    ]
    args["sign"].assert_awaited_once_with(
        get_l1_action_payload(action, None, payload["nonce"], 1_030_000, True)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"cloids": ()},
        {"cloids": tuple("0x" + digit * 32 for digit in "12")},
        {"cloids": tuple("0x" + digit * 32 for digit in "1234")},
        {"cloids": tuple("0x" + digit * 32 for digit in "112")},
        {"cloids": tuple("0x" + digit * 32 for digit in "aA1")},
        {"cloids": ("invalid", "0x" + "2" * 32, "0x" + "3" * 32)},
        {"stop_loss_price": Decimal("NaN")},
        {"take_profit_price": Decimal("Infinity")},
        {"stop_loss_price": Decimal(0)},
        {"take_profit_price": Decimal(-1)},
        {"stop_loss_price": Decimal("2010")},
        {"take_profit_price": Decimal("1990")},
        {"stop_loss_price": Decimal("2000")},
        {"take_profit_price": Decimal("2000")},
        {"entry": PerpIocOrder(1, Decimal("-0.02"), Decimal("2000"))},
        {"entry": PerpIocOrder(1, Decimal("1E-13"), Decimal("2000"))},
        {"take_profit_price": Decimal("2020.00000000000001")},
        {"expires_after": 1_000_000},
        {"expires_after": 1_030_001},
        {"expires_after": True},
    ],
)
async def test_invalid_bracket_never_signs_or_submits(bracket_args, overrides):
    with pytest.raises(BracketNotSubmitted):
        await submit_prepared_bracket(**{**bracket_args, **overrides})
    bracket_args["sign"].assert_not_awaited()
    bracket_args["post_exchange"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ValueError("session expired"), None])
async def test_failed_or_empty_signature_is_known_unsent(bracket_args, failure):
    bracket_args["sign"].side_effect = failure
    bracket_args["sign"].return_value = None
    with pytest.raises(BracketNotSubmitted):
        await submit_prepared_bracket(**bracket_args)
    bracket_args["post_exchange"].assert_not_awaited()


@pytest.mark.asyncio
async def test_expiry_during_signing_does_not_send_any_order(bracket_args, monkeypatch):
    times = iter((1_000_000, 1_030_000))
    monkeypatch.setattr(module, "get_timestamp_ms", lambda: next(times))
    with pytest.raises(BracketNotSubmitted):
        await submit_prepared_bracket(**bracket_args)
    bracket_args["sign"].assert_awaited_once()
    bracket_args["post_exchange"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_during", ["sign", "post_exchange"])
async def test_cancellation_is_not_misreported_as_known_unsent(
    bracket_args, cancel_during
):
    entered = asyncio.Event()

    async def hang(_):
        entered.set()
        await asyncio.Event().wait()

    bracket_args[cancel_during].side_effect = hang
    task = asyncio.create_task(submit_prepared_bracket(**bracket_args))
    async with asyncio.timeout(5):
        await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bracket_args["post_exchange"].await_count == (
        cancel_during == "post_exchange"
    )


@pytest.mark.asyncio
async def test_lost_response_is_uncertain_and_not_retried(bracket_args):
    bracket_args["post_exchange"].side_effect = TimeoutError("response lost")
    with pytest.raises(TimeoutError):
        await submit_prepared_bracket(**bracket_args)
    bracket_args["post_exchange"].assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statuses",
    [
        [
            {"error": "Insufficient margin"},
            {"error": "Parent rejected"},
            {"error": "Parent rejected"},
        ],
        [
            {"filled": {"totalSz": "0.01", "avgPx": "2000", "oid": 100}},
            "waitingForFill",
            "waitingForFill",
        ],
        [
            {"filled": {"totalSz": "0.02", "avgPx": "2000", "oid": 100}},
            "waitingForTrigger",
            {"error": "Invalid trigger"},
        ],
        [{"filled": {"totalSz": "0.02", "avgPx": "2000", "oid": 100}}],
    ],
)
async def test_per_order_failures_pass_through_without_repair_or_entry_retry(
    bracket_args, statuses
):
    response = {
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": statuses}},
    }
    bracket_args["post_exchange"].return_value = response
    assert await submit_prepared_bracket(**bracket_args) is response
    bracket_args["post_exchange"].assert_awaited_once()
