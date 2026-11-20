import asyncio
from unittest.mock import AsyncMock

import pytest
from hyperliquid.utils.signing import get_l1_action_payload

from wayfinder_paths.adapters.hyperliquid_adapter import prepared_orders as module
from wayfinder_paths.adapters.hyperliquid_adapter.prepared_orders import (
    CancelNotSubmitted,
    submit_prepared_cancels,
)


@pytest.fixture
def cancel_args(monkeypatch):
    monkeypatch.setattr(module, "get_timestamp_ms", lambda: 1_000_000)
    return {
        "asset_id": 1,
        "cloids": tuple("0x" + digit * 32 for digit in "12"),
        "expires_after": 1_030_000,
        "sign": AsyncMock(return_value={"r": "0x01", "s": "0x02", "v": 27}),
        "post_exchange": AsyncMock(return_value={"status": "ok"}),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("asset_id", [0, 1, 110012])
async def test_cancels_only_requested_ids_once_with_signed_expiry(
    cancel_args, asset_id
):
    cancel_args["asset_id"] = asset_id
    assert await submit_prepared_cancels(**cancel_args) == {"status": "ok"}
    cancel_args["post_exchange"].assert_awaited_once()
    payload = cancel_args["post_exchange"].call_args.args[0]
    action = {
        "type": "cancelByCloid",
        "cancels": [
            {"asset": asset_id, "cloid": cloid} for cloid in cancel_args["cloids"]
        ],
    }
    assert (
        payload["action"] == action
    )  # No account-wide cancel or unsupported fast flag.
    assert payload["expiresAfter"] == 1_030_000
    cancel_args["sign"].assert_awaited_once_with(
        get_l1_action_payload(action, None, 1_000_000, 1_030_000, True)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"asset_id": True},
        {"asset_id": -1},
        {"asset_id": 10000},
        {"asset_id": 100000000},
        {"cloids": ()},
        {"cloids": ("bad",)},
        {"cloids": tuple("0x" + digit * 32 for digit in "aA")},
        {"cloids": tuple("0x" + digit * 32 for digit in "1234")},
        {"expires_after": 1_000_000},
        {"expires_after": 1_030_001},
    ],
)
async def test_invalid_cancel_is_known_unsent(cancel_args, overrides):
    with pytest.raises(CancelNotSubmitted):
        await submit_prepared_cancels(**{**cancel_args, **overrides})
    cancel_args["sign"].assert_not_awaited()
    cancel_args["post_exchange"].assert_not_awaited()


@pytest.mark.asyncio
async def test_expiry_during_signing_is_known_unsent(cancel_args, monkeypatch):
    times = iter((1_000_000, 1_030_000))
    monkeypatch.setattr(module, "get_timestamp_ms", lambda: next(times))
    with pytest.raises(CancelNotSubmitted):
        await submit_prepared_cancels(**cancel_args)
    cancel_args["post_exchange"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ValueError("session expired"), None])
async def test_failed_or_empty_cancel_signature_is_known_unsent(cancel_args, failure):
    cancel_args["sign"].side_effect = failure
    cancel_args["sign"].return_value = None
    with pytest.raises(CancelNotSubmitted):
        await submit_prepared_cancels(**cancel_args)
    cancel_args["post_exchange"].assert_not_awaited()


@pytest.mark.asyncio
async def test_lost_cancel_response_is_not_retried(cancel_args):
    cancel_args["post_exchange"].side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await submit_prepared_cancels(**cancel_args)
    cancel_args["post_exchange"].assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["sign", "post_exchange"])
async def test_cancelled_task_remains_uncertain(cancel_args, stage):
    cancel_args[stage].side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await submit_prepared_cancels(**cancel_args)
    assert cancel_args["post_exchange"].await_count == (stage == "post_exchange")
