from unittest.mock import AsyncMock

import pytest
from eth_account import Account
from hyperliquid.utils.signing import sign_l1_action

from wayfinder_paths.adapters.hyperliquid_adapter.adapter import (
    HyperliquidAdapter,
    trailing_stop_action,
    trailing_stop_pct_wire,
)

# Cross-implementation vector: @bloxwap/hyperliquid pins this signature for the
# same action (public test key, fixed nonce, testnet), and its live testnet
# test places trailing stops with it. Matching it proves our key order and
# hashing are what HL accepts.
VECTOR_KEY = "0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
VECTOR_NONCE = 1_700_000_000_000
VECTOR_SIGNATURE = {
    "r": "0x59eed0cdff8d6b5261e1f52feb37e4da7c3f256e74c152c108d5bae370e54679",
    "s": "0x20e2655aed29e363e0d7b8e43ea1d5e020377af5c1fd79bd23102b7329987ba7",
    "v": 27,
}


def test_action_signs_like_the_reference_implementation():
    action = trailing_stop_action(
        asset_id=0,
        is_buy=True,
        size_wire="0.1",
        reduce_only=False,
        retracement={"pct": "1.234%"},
        activation_px="30000",
    )
    assert list(action) == [
        "type",
        "asset",
        "isBuy",
        "sz",
        "reduceOnly",
        "retracement",
        "activationPx",
    ]
    signature = sign_l1_action(
        Account.from_key(VECTOR_KEY), action, None, VECTOR_NONCE, None, False
    )
    assert signature == VECTOR_SIGNATURE


def test_unset_activation_stays_in_the_action_as_null():
    action = trailing_stop_action(
        asset_id=0,
        is_buy=False,
        size_wire="1",
        reduce_only=True,
        retracement={"px": "250"},
        activation_px=None,
    )
    assert "activationPx" in action and action["activationPx"] is None


@pytest.mark.parametrize(
    ("pct", "wire"),
    [(1.5, "1.5%"), (2, "2%"), (1.2345, "1.234%"), (0.1, "0.1%"), (50, "50%")],
)
def test_pct_wire_format(pct, wire):
    assert trailing_stop_pct_wire(pct) == wire


@pytest.mark.parametrize("pct", [0, -1, 100, 0.0001])
def test_pct_wire_rejects_out_of_range(pct):
    with pytest.raises(ValueError):
        trailing_stop_pct_wire(pct)


def _adapter(result):
    adapter = HyperliquidAdapter(config={})
    adapter.get_valid_order_size = lambda asset_id, size: size
    adapter.get_valid_order_price = lambda asset_id, price: price
    adapter.ensure_unified_account = AsyncMock()
    adapter._sign_and_broadcast_hypecore = AsyncMock(return_value=result)
    return adapter


@pytest.mark.asyncio
async def test_place_trailing_stop_sends_the_action_and_reads_the_oid():
    adapter = _adapter(
        {"status": "ok", "response": {"type": "trailingStop", "data": {"oid": 77}}}
    )
    ok, res = await adapter.place_trailing_stop(
        3, False, 0.5, "0xabc", retracement_pct=2.5, activation_price=72000.0
    )
    assert ok is True
    action, address = adapter._sign_and_broadcast_hypecore.await_args.args
    assert address == "0xabc"
    assert action == {
        "type": "trailingStop",
        "asset": 3,
        "isBuy": False,
        "sz": "0.5",
        "reduceOnly": True,
        "retracement": {"pct": "2.5%"},
        "activationPx": "72000",
    }


@pytest.mark.asyncio
async def test_place_trailing_stop_absolute_distance_without_activation():
    adapter = _adapter({"status": "ok", "response": {"data": {"oid": 1}}})
    await adapter.place_trailing_stop(0, True, 1.0, "0xabc", retracement_px=150.0)
    action = adapter._sign_and_broadcast_hypecore.await_args.args[0]
    assert action["retracement"] == {"px": "150"}
    assert action["activationPx"] is None


@pytest.mark.asyncio
async def test_place_trailing_stop_reports_hl_errors_as_failure():
    adapter = _adapter(
        {"status": "ok", "response": {"data": {"error": "Retracement too small"}}}
    )
    ok, _ = await adapter.place_trailing_stop(
        0, True, 1.0, "0xabc", retracement_pct=0.01
    )
    assert ok is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"retracement_pct": 1.0, "retracement_px": 10.0},
        {"retracement_px": 0.0},
        {"retracement_pct": 1.0, "activation_price": 0.0},
    ],
)
async def test_place_trailing_stop_rejects_bad_input_before_signing(kwargs):
    adapter = _adapter({"status": "ok"})
    with pytest.raises(ValueError):
        await adapter.place_trailing_stop(0, True, 1.0, "0xabc", **kwargs)
    adapter._sign_and_broadcast_hypecore.assert_not_awaited()
