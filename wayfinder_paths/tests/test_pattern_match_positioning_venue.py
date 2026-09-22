from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal as D
from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.adapters.hyperliquid_adapter.prepared_orders import (
    IocFill,
    PerpIocOrder,
)
from wayfinder_paths.quant.pattern_match_positioning_venue import (
    positioning_account_snapshot,
    prepare_positioning_entry,
    prepare_positioning_exit,
)


@pytest.fixture
def provider():
    address = "0x" + "a1" * 20
    responses = {
        "metaAndAssetCtxs": [
            {
                "universe": [
                    {"name": "BTC", "szDecimals": 5},
                    {"name": "ETH", "szDecimals": 4},
                ]
            },
            [{"midPx": "60000"}, {"midPx": "2000"}],
        ],
        "clearinghouseState": {
            "time": int(datetime.now(UTC).timestamp() * 1000),
            "assetPositions": [],
        },
        "frontendOpenOrders": [],
        "userAbstraction": "unifiedAccount",
        "maxBuilderFee": 50,
        "userFees": {"userCrossRate": "0.00045"},
        "ETH": {
            "coin": "ETH",
            "user": address,
            "leverage": {"value": 10},
            "maxTradeSzs": ["1", "1"],
            "availableToTrade": ["20", "20"],
        },
        "BTC": {
            "coin": "BTC",
            "user": address,
            "leverage": {"value": 10},
            "maxTradeSzs": ["1", "1"],
            "availableToTrade": ["20", "20"],
        },
    }
    info = AsyncMock(
        side_effect=lambda payload: responses[payload.get("coin", payload["type"])]
    )
    args = {
        "info": info,
        "address": address,
        "coin": "ETH",
        "direction": 1,
        "hedge_beta": 1.5,
        "gross_notional": D("100"),
        "slippage_bps": 50,
    }
    return args, responses


@pytest.mark.asyncio
async def test_entry_sizes_both_legs_and_preserves_side_limits(provider):
    args, _ = provider
    orders = await prepare_positioning_entry(**args)
    assert orders == (
        PerpIocOrder(1, D("0.02"), D("2010")),
        PerpIocOrder(0, D("-0.001"), D("59700")),
    )
    assert {call.args[0]["type"] for call in args["info"].await_args_list} == {
        "metaAndAssetCtxs",
        "clearinghouseState",
        "frontendOpenOrders",
        "userAbstraction",
        "maxBuilderFee",
        "userFees",
        "activeAssetData",
    }  # No account setup, leverage or signing writes.


@pytest.mark.asyncio
async def test_both_legs_cannot_each_spend_the_same_margin(provider):
    args, data = provider
    for coin in ("BTC", "ETH"):
        data[coin]["availableToTrade"] = ["7", "7"]
    with pytest.raises(ValueError, match="both legs"):
        await prepare_positioning_entry(**args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        "account",
        "fee",
        "position",
        "order",
        "stale",
        "wallet",
        "capacity",
        "price",
        "minimum",
    ],
)
async def test_entry_rejects_invalid_live_preflight(provider, kind):
    args, data = provider
    if kind == "account":
        data["userAbstraction"] = "default"
    elif kind == "fee":
        data["maxBuilderFee"] = 49
    elif kind == "position":
        data["clearinghouseState"]["assetPositions"] = [
            {"position": {"coin": "BTC", "szi": "-0.1"}}
        ]
    elif kind == "order":
        data["frontendOpenOrders"] = [{"coin": "ETH"}]
    elif kind == "stale":
        data["clearinghouseState"]["time"] -= 31_000
    elif kind == "wallet":
        data["ETH"]["user"] = "0x" + "b2" * 20
    elif kind == "capacity":
        data["BTC"]["maxTradeSzs"][1] = "0.0001"
    elif kind == "price":
        data["metaAndAssetCtxs"][1][0]["midPx"] = "NaN"
    else:
        args.update(gross_notional=D("20"), hedge_beta=1)
    with pytest.raises(ValueError):
        await prepare_positioning_entry(**args)


@pytest.mark.asyncio
@pytest.mark.parametrize("slippage", [-1, 101, True, 0.5])
async def test_invalid_slippage_cannot_loosen_limit(provider, slippage):
    args, _ = provider
    with pytest.raises(ValueError, match="slippage"):
        await prepare_positioning_entry(**{**args, "slippage_bps": slippage})


@pytest.mark.asyncio
async def test_zero_beta_only_reads_asset_capacity(provider):
    args, _ = provider
    orders = await prepare_positioning_entry(**{**args, "hedge_beta": 0})
    assert len(orders) == 1 and orders[0].asset_id == 1
    assert not any(
        call.args[0].get("coin") == "BTC" for call in args["info"].await_args_list
    )


@pytest.mark.asyncio
async def test_exit_rounds_down_without_dropping_subminimum_reductions(provider):
    args, _ = provider
    orders = await prepare_positioning_exit(
        args["info"],
        remaining={1: D("0.00209"), 0: D("-0.00002")},
        coins={1: "ETH", 0: "BTC"},
        slippage_bps=50,
    )
    assert orders == (
        PerpIocOrder(1, D("-0.002"), D("1990")),
        PerpIocOrder(0, D("0.00002"), D("60300")),
    )


@pytest.mark.asyncio
async def test_exit_refuses_remapped_asset(provider):
    args, data = provider
    data["metaAndAssetCtxs"][0]["universe"].reverse()
    with pytest.raises(ValueError, match="identity"):
        await prepare_positioning_exit(
            args["info"], remaining={1: D("0.02")}, coins={1: "ETH"}, slippage_bps=50
        )


@pytest.fixture
def account():
    return {
        "state": {
            "time": 5_000,
            "assetPositions": [
                {"position": {"coin": "ETH", "szi": "0.02", "unrealizedPnl": "2"}},
                {"position": {"coin": "BTC", "szi": "-0.001", "unrealizedPnl": "-1"}},
            ],
        },
        "coins": {1: "ETH", 0: "BTC"},
        "entries": [
            (PerpIocOrder(1, D("0.02"), D("2010")), IocFill(D("0.02"), D("2000"), 1)),
            (
                PerpIocOrder(0, D("-0.001"), D("59700")),
                IocFill(D("0.001"), D("60000"), 2),
            ),
        ],
        "exits": [],
        "fills": [
            {
                "coin": "ETH",
                "side": "B",
                "oid": 1,
                "tid": 1,
                "sz": "0.02",
                "px": "2000",
                "fee": "0.1",
                "builderFee": "0.02",
                "feeToken": "USDC",
                "startPosition": "0",
                "time": 1_000,
            },
            {
                "coin": "BTC",
                "side": "A",
                "oid": 2,
                "tid": 2,
                "sz": "0.001",
                "px": "60000",
                "fee": "0.2",
                "builderFee": "0.03",
                "feeToken": "USDC",
                "startPosition": "0",
                "time": 1_000,
            },
        ],
        "funding": [
            {
                "time": 2_000,
                "delta": {
                    "coin": "ETH",
                    "type": "funding",
                    "szi": "0.02",
                    "usdc": "-0.05",
                },
            }
        ],
    }


def test_snapshot_net_pnl_includes_funding_and_fees_not_builder_twice(account):
    account["fills"].append(deepcopy(account["fills"][0]))
    result = positioning_account_snapshot(**account)
    assert result.positions == {1: D("0.02"), 0: D("-0.001")}
    assert result.net_return == pytest.approx(0.0065)
    assert not result.unmanaged_trade


def test_fill_cost_not_rounded_acknowledgement_price_is_denominator(account):
    order, fill = account["entries"][0]
    account["entries"][0] = order, IocFill(fill.size, D("2001"), fill.order_id)
    assert positioning_account_snapshot(**account).net_return == pytest.approx(0.0065)


@pytest.mark.parametrize("funding", [None, [{"time": 0, "delta": {}}] * 500])
def test_missing_or_truncated_funding_preserves_inventory_without_inventing_pnl(
    account, funding
):
    account["funding"] = funding
    result = positioning_account_snapshot(**account)
    assert result.net_return is None and result.positions[1] == D("0.02")


def test_funding_from_different_inventory_cannot_be_attributed(account):
    account["funding"][0]["delta"]["szi"] = "0.03"
    assert positioning_account_snapshot(**account).net_return is None


@pytest.mark.parametrize(
    "kind", ["missing", "wrong_side", "wrong_coin", "missing_oid", "fee_currency"]
)
def test_snapshot_requires_complete_attributable_history(account, kind):
    if kind == "missing":
        account["fills"].pop()
    elif kind == "missing_oid":
        order, fill = account["entries"][0]
        account["entries"][0] = order, IocFill(fill.size, fill.average_price)
    else:
        field, value = {
            "wrong_side": ("side", "A"),
            "wrong_coin": ("coin", "BTC"),
            "fee_currency": ("feeToken", "HYPE"),
        }[kind]
        account["fills"][0][field] = value
    with pytest.raises(ValueError):
        positioning_account_snapshot(**account)


def test_manual_close_then_reopen_equal_size_does_not_resurrect_owned_position(account):
    original = account["fills"][0]
    account["fills"] += [
        {
            **original,
            "oid": 10,
            "tid": 10,
            "side": "A",
            "startPosition": "0.02",
            "time": 2_000,
        },
        {**original, "oid": 11, "tid": 11, "startPosition": "0", "time": 3_000},
    ]
    result = positioning_account_snapshot(**account)
    assert result.unmanaged_trade and result.net_return is None
    assert result.positions == {1: D(0), 0: D("-0.001")}


def test_partial_manual_reduction_caps_cleanup_to_tracked_remainder(account):
    account["fills"].append(
        {
            **account["fills"][0],
            "oid": 10,
            "tid": 10,
            "side": "A",
            "sz": "0.005",
            "time": 2_000,
        }
    )
    result = positioning_account_snapshot(**account)
    assert result.unmanaged_trade and result.positions[1] == D("0.015")


def test_entry_race_is_detected_from_start_position(account):
    account["fills"][0]["startPosition"] = "0.01"
    result = positioning_account_snapshot(**account)
    assert result.unmanaged_trade and result.net_return is None
    assert result.positions[1] == D("0.02")


def test_partial_entry_fills_in_same_millisecond_do_not_depend_on_trade_id_order(
    account,
):
    original = account["fills"].pop(0)
    account["fills"] += [
        {**original, "tid": 9, "sz": "0.01", "fee": "0.05"},
        {**original, "tid": 3, "sz": "0.01", "startPosition": "0.01", "fee": "0.05"},
    ]
    result = positioning_account_snapshot(**account)
    assert not result.unmanaged_trade
    assert result.net_return == pytest.approx(0.0065)


def test_same_millisecond_manual_reduction_cannot_be_skipped_before_entry(account):
    account["fills"][0]["tid"] = 9
    account["fills"].append(
        {
            **account["fills"][0],
            "oid": 10,
            "tid": 0,
            "side": "A",
            "startPosition": "0.02",
        }
    )
    result = positioning_account_snapshot(**account)
    assert result.unmanaged_trade and result.positions[1] == 0


def test_prior_wallet_history_and_other_markets_do_not_trigger_cleanup(account):
    account["fills"] += [
        {**account["fills"][0], "oid": 10, "tid": 10, "time": 1},
        {**account["fills"][0], "coin": "SOL", "oid": 11, "tid": 11, "time": 2_000},
    ]
    assert not positioning_account_snapshot(**account).unmanaged_trade


def test_owned_exit_reduces_inventory_without_counting_pnl_twice(account):
    account["exits"] = [
        (PerpIocOrder(1, D("-0.01"), D("2100")), IocFill(D("0.01"), D("2100"), 3))
    ]
    account["fills"].append(
        {
            **account["fills"][0],
            "oid": 3,
            "tid": 3,
            "side": "A",
            "sz": "0.01",
            "time": 2_000,
        }
    )
    account["state"]["assetPositions"][0]["position"]["szi"] = "0.01"
    result = positioning_account_snapshot(**account)
    assert result.positions[1] == D("0.01") and result.net_return is None
    assert not result.unmanaged_trade


def test_opposite_live_position_is_never_owned(account):
    account["state"]["assetPositions"][0]["position"]["szi"] = "-0.02"
    result = positioning_account_snapshot(**account)
    assert result.positions[1] == 0 and result.net_return is None
