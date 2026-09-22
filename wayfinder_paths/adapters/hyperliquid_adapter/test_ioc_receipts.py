from decimal import Decimal as D

import pytest

from wayfinder_paths.adapters.hyperliquid_adapter.prepared_orders import (
    IocFill,
    ioc_response_fills,
    reconcile_ioc_fill,
)


def test_mixed_ioc_acknowledgement_is_resolved_per_leg():
    response = {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {
                "statuses": [
                    {"filled": {"totalSz": "0.02", "avgPx": "2000", "oid": 1}},
                    {"error": "Insufficient margin"},
                ]
            },
        },
    }
    assert ioc_response_fills(response, 2) == (
        IocFill(D("0.02"), D("2000"), 1),
        IocFill(D(0)),
    )


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"status": "ok", "response": {"type": "default"}},
        {"status": "ok", "response": {"type": "order", "data": {"statuses": []}}},
        {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {"statuses": [{"resting": {"oid": 1}}]},
            },
        },
    ],
)
def test_unknown_response_never_becomes_known_zero(response):
    assert ioc_response_fills(response, 1) == (None,)


def test_explicit_batch_rejection_is_known_zero():
    assert (
        ioc_response_fills({"status": "err", "response": "Expired"}, 2)
        == (IocFill(D(0)),) * 2
    )


@pytest.fixture
def receipt():
    cloid = "0x" + "01" * 16
    return {
        "status": {
            "status": "order",
            "order": {
                "order": {
                    "cloid": cloid,
                    "coin": "ETH",
                    "side": "B",
                    "origSz": "0.02",
                    "sz": "0.01",
                    "oid": 1,
                },
                "status": "canceled",
                "statusTimestamp": 2_000,
            },
        },
        "fills": [
            {
                "coin": "ETH",
                "side": "B",
                "oid": 1,
                "tid": 1,
                "sz": "0.01",
                "px": "2000",
                "fee": "0.1",
                "feeToken": "USDC",
                "builderFee": "0.05",
            }
        ],
        "cloid": cloid,
        "coin": "ETH",
        "signed_size": D("0.02"),
    }


def test_partial_canceled_ioc_reconciles_exact_fill_without_double_builder_fee(receipt):
    receipt["fills"] *= 2
    assert reconcile_ioc_fill(**receipt) == IocFill(D("0.01"), D("2000"), 1, D("0.1"))


def test_client_order_id_is_a_case_insensitive_hex_identifier(receipt):
    receipt["cloid"] = "0x" + "AB" * 16
    receipt["status"]["order"]["order"]["cloid"] = "0x" + "ab" * 16
    assert reconcile_ioc_fill(**receipt) == IocFill(D("0.01"), D("2000"), 1, D("0.1"))


def test_filled_ioc_requires_complete_history(receipt):
    receipt["status"]["order"].update(status="filled")
    receipt["status"]["order"]["order"]["sz"] = "0"
    assert reconcile_ioc_fill(**receipt) is None
    receipt["fills"].append({**receipt["fills"][0], "tid": 2, "px": "2010"})
    assert reconcile_ioc_fill(**receipt) == IocFill(D("0.02"), D("2005"), 1, D("0.2"))


def test_unknown_order_and_open_order_remain_unresolved(receipt):
    assert reconcile_ioc_fill(**{**receipt, "status": {"status": "unknownOid"}}) is None
    receipt["status"]["order"]["status"] = "open"
    assert reconcile_ioc_fill(**receipt) is None


@pytest.mark.parametrize(
    "state", ["rejected", "iocCancelRejected", "perpMarginRejected"]
)
def test_terminal_rejection_is_zero(receipt, state):
    receipt["status"]["order"]["status"] = state
    assert reconcile_ioc_fill(**receipt) == IocFill(D(0), order_id=1, fee_usd=D(0))


@pytest.mark.parametrize(
    "field,value", [("cloid", "wrong"), ("coin", "BTC"), ("side", "A"), ("origSz", "1")]
)
def test_wrong_order_identity_cannot_resolve_persisted_order(receipt, field, value):
    receipt["status"]["order"]["order"][field] = value
    with pytest.raises(ValueError, match="persisted IOC"):
        reconcile_ioc_fill(**receipt)


@pytest.mark.parametrize(
    "field,value",
    [("side", "A"), ("coin", "BTC"), ("feeToken", "HYPE"), ("sz", "NaN"), ("px", "0")],
)
def test_invalid_fill_cannot_resolve_order(receipt, field, value):
    receipt["fills"][0][field] = value
    with pytest.raises(ValueError):
        reconcile_ioc_fill(**receipt)


def test_receipt_json_keeps_venue_identity_and_optional_fee():
    assert IocFill(D("0.01"), D("2000"), 1, D("0.1")).as_dict() == {
        "size": "0.01",
        "average_price": "2000",
        "order_id": 1,
        "fee_usd": "0.1",
    }
