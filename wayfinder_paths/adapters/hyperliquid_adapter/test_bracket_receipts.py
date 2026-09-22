from decimal import Decimal as D

import pytest

from wayfinder_paths.adapters.hyperliquid_adapter.bracket_receipts import (
    NativeExitReceipt,
    reconcile_bracket_exit,
)
from wayfinder_paths.adapters.hyperliquid_adapter.prepared_orders import IocFill


@pytest.fixture
def exit_receipt():
    cloid = "0x" + "ab" * 16
    return {
        "status": {
            "status": "order",
            "order": {
                "order": {
                    "cloid": cloid,
                    "coin": "ETH",
                    "side": "A",
                    "origSz": "0.02",
                    "sz": "0.02",
                    "oid": 10,
                    "isTrigger": True,
                    "isPositionTpsl": False,
                    "reduceOnly": True,
                    "orderType": "Stop Market",
                    "triggerPx": "1980",
                },
                "status": "open",
                "statusTimestamp": 2_000,
            },
        },
        "fills": [],
        "cloid": cloid,
        "coin": "ETH",
        "signed_size": D("-0.02"),
        "trigger_price": D("1980"),
        "tpsl": "sl",
    }


@pytest.mark.parametrize("coin", ["ETH", "xyz:SP500"])
@pytest.mark.parametrize("is_long", [True, False])
@pytest.mark.parametrize("tpsl", ["tp", "sl"])
def test_live_native_exit_matches_frozen_terms(exit_receipt, coin, is_long, tpsl):
    exit_receipt.update(coin=coin, tpsl=tpsl)
    exit_receipt["signed_size"] *= 1 if is_long else -1
    order = exit_receipt["status"]["order"]["order"]
    order.update(
        coin=coin,
        side="A" if is_long else "B",
        orderType="Take Profit Market" if tpsl == "tp" else "Stop Market",
        cloid=exit_receipt["cloid"].upper().replace("0X", "0x"),
    )
    assert reconcile_bracket_exit(**exit_receipt) == NativeExitReceipt("open", 10)


@pytest.mark.parametrize(
    "field,value",
    [
        ("cloid", "wrong"),
        ("coin", "BTC"),
        ("side", "B"),
        ("origSz", "0.01"),
        ("sz", "0.01"),
        ("triggerPx", "1970"),
        ("isTrigger", False),
        ("reduceOnly", False),
        ("isPositionTpsl", True),
        ("orderType", "Stop Limit"),
        ("orderType", "Take Profit Market"),
    ],
)
def test_wrong_exit_never_counts_as_protection(exit_receipt, field, value):
    exit_receipt["status"]["order"]["order"][field] = value
    with pytest.raises(ValueError):
        reconcile_bracket_exit(**exit_receipt)


def test_unknown_is_not_a_canceled_child(exit_receipt):
    exit_receipt["status"] = {"status": "unknownOid"}
    assert reconcile_bracket_exit(**exit_receipt) is None


def test_triggered_is_not_a_final_fill_or_live_protection(exit_receipt):
    exit_receipt["status"]["order"]["status"] = "triggered"
    result = reconcile_bracket_exit(**exit_receipt)
    assert result == NativeExitReceipt("triggered", 10)
    assert result.fill is None


@pytest.mark.parametrize(
    "state",
    [
        "canceled",
        "siblingFilledCanceled",
        "marginCanceled",
        "scheduledCancel",
        "tickRejected",
    ],
)
def test_confirmed_empty_terminal_child(exit_receipt, state):
    exit_receipt["status"]["order"]["status"] = state
    result = reconcile_bracket_exit(**exit_receipt)
    assert result == NativeExitReceipt(
        "terminal", 10, IocFill(D(0), order_id=10, fee_usd=D(0))
    )


@pytest.mark.parametrize("state,remaining", [("filled", "0"), ("canceled", "0.01")])
def test_final_exit_needs_all_attributable_fills(exit_receipt, state, remaining):
    envelope = exit_receipt["status"]["order"]
    envelope.update(status=state)
    # Once executed the venue may describe the order as Market, not Stop Market.
    envelope["order"].update(
        sz=remaining, isTrigger=False, orderType="Market", triggerPx="0"
    )
    assert reconcile_bracket_exit(**exit_receipt) is None
    size = D("0.02") - D(remaining)
    fill = {
        "oid": 10,
        "tid": 1,
        "coin": "ETH",
        "side": "A",
        "sz": str(size),
        "px": "1979",
        "fee": "0.01",
        "feeToken": "USDC",
        "builderFee": "0.005",
    }
    exit_receipt["fills"] = [fill, fill]  # Page boundaries must not double count.
    assert reconcile_bracket_exit(**exit_receipt) == NativeExitReceipt(
        "terminal", 10, IocFill(size, D("1979"), 10, D("0.01"))
    )


def test_wrong_fill_identity_is_rejected(exit_receipt):
    exit_receipt["status"]["order"].update(status="filled")
    exit_receipt["status"]["order"]["order"]["sz"] = "0"
    exit_receipt["fills"] = [
        {"oid": 10, "tid": 1, "coin": "BTC", "side": "A", "feeToken": "USDC"}
    ]
    with pytest.raises(ValueError, match="attribution"):
        reconcile_bracket_exit(**exit_receipt)
