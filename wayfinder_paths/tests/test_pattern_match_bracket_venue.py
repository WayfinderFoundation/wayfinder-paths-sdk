from datetime import UTC, datetime
from decimal import Decimal as D
from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.adapters.hyperliquid_adapter.prepared_orders import PerpIocOrder
from wayfinder_paths.quant.pattern_match_bracket_venue import (
    prepare_native_bracket,
    prepare_native_bracket_exit,
)


@pytest.fixture(params=["", "xyz"])
def provider(request):
    dex = request.param
    coin = f"{dex}:ABC" if dex else "ABC"
    address = "0x" + "a1" * 20
    data = {
        "perpDexs": [None, {"name": "other"}, {"name": "xyz"}],
        "metaAndAssetCtxs": [
            {
                "collateralToken": 0,
                "universe": [{"name": coin, "szDecimals": 3, "deployerFeeScale": "1"}],
            },
            [{"midPx": "100.125", "markPx": "100.125"}],
        ],
        "clearinghouseState": {
            "time": int(datetime.now(UTC).timestamp() * 1000),
            "assetPositions": [],
        },
        "frontendOpenOrders": [],
        "userAbstraction": "unifiedAccount",
        "maxBuilderFee": 50,
        "userFees": {"userCrossRate": "0.00045"},
        "activeAssetData": {
            "coin": coin,
            "user": address,
            "leverage": {"value": 10},
            "maxTradeSzs": ["20", "20"],
            "availableToTrade": ["20", "20"],
        },
    }
    info = AsyncMock(side_effect=lambda payload: data[payload["type"]])
    args = {
        "info": info,
        "address": address,
        "coin": coin,
        "direction": 1,
        "gross_notional": D("100"),
        "slippage_bps": 50,
        "take_profit_price": D("102.1234"),
        "stop_loss_price": D("98.7654"),
    }
    return args, data, dex


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", [1, -1])
async def test_bracket_uses_live_dex_identity_perp_ticks_and_read_only_calls(
    provider, direction
):
    args, _, dex = provider
    args["direction"] = direction
    if direction == -1:
        args["take_profit_price"], args["stop_loss_price"] = (
            args["stop_loss_price"],
            args["take_profit_price"],
        )
    plan = await prepare_native_bracket(**args)
    asset = 120_000 if dex else 0
    assert plan.entry.asset_id == asset
    assert plan.entry.signed_size == direction * D("0.998")
    assert plan.entry.limit_price == (D("100.62") if direction == 1 else D("99.625"))
    assert plan.take_profit_price == (D("102.12") if direction == 1 else D("98.766"))
    assert plan.stop_loss_price == (D("98.766") if direction == 1 else D("102.12"))
    calls = [call.args[0] for call in args["info"].await_args_list]
    expected = {
        "metaAndAssetCtxs",
        "clearinghouseState",
        "frontendOpenOrders",
        "userAbstraction",
        "maxBuilderFee",
        "userFees",
        "activeAssetData",
    }
    assert {payload["type"] for payload in calls} == expected | (
        {"perpDexs"} if dex else set()
    )
    for payload in calls:
        if payload["type"] in {
            "metaAndAssetCtxs",
            "clearinghouseState",
            "frontendOpenOrders",
        }:
            assert payload.get("dex", "") == dex
    assert all(payload.get("coin", args["coin"]) == args["coin"] for payload in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        "account",
        "fee",
        "position",
        "order",
        "stale",
        "future",
        "wallet",
        "coin",
        "capacity",
        "margin",
        "minimum",
        "delisted",
        "mark",
        "no_mid",
        "negative_mid",
        "size_decimals",
        "duplicate",
        "universe",
        "crossed_mark",
        "crossed_limit",
    ],
)
async def test_preflight_rejects_unsafe_or_incomplete_live_state(provider, kind):
    args, data, _ = provider
    meta, contexts = data["metaAndAssetCtxs"]
    if kind == "account":
        data["userAbstraction"] = "default"
    elif kind == "fee":
        data["maxBuilderFee"] = 49
    elif kind == "position":
        data["clearinghouseState"]["assetPositions"] = [
            {"position": {"coin": args["coin"], "szi": "0.01"}}
        ]
    elif kind == "order":
        data["frontendOpenOrders"] = [{"coin": args["coin"]}]
    elif kind in {"stale", "future"}:
        data["clearinghouseState"]["time"] += -31_000 if kind == "stale" else 6_000
    elif kind in {"wallet", "coin"}:
        data["activeAssetData"]["user" if kind == "wallet" else "coin"] = "another"
    elif kind == "capacity":
        data["activeAssetData"]["maxTradeSzs"][0] = "0.1"
    elif kind == "margin":
        data["activeAssetData"]["availableToTrade"][0] = "10"
    elif kind == "minimum":
        args["gross_notional"] = D("9")
    elif kind == "delisted":
        meta["universe"][0]["isDelisted"] = True
    elif kind == "mark":
        contexts[0]["markPx"] = "NaN"
    elif kind in {"no_mid", "negative_mid"}:
        contexts[0]["midPx"] = None if kind == "no_mid" else "-1"
    elif kind == "size_decimals":
        meta["universe"][0]["szDecimals"] = True
    elif kind == "duplicate":
        meta["universe"] *= 2
        contexts *= 2
    elif kind == "universe":
        contexts.clear()
    elif kind == "crossed_mark":
        contexts[0]["markPx"] = "103"
    elif kind == "crossed_limit":
        args["take_profit_price"] = D("100.2")
    with pytest.raises(ValueError):
        await prepare_native_bracket(**args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("direction", 0),
        ("direction", True),
        ("gross_notional", D("NaN")),
        ("gross_notional", D("0")),
        ("stop_loss_price", D("Infinity")),
        ("take_profit_price", D("0")),
        ("slippage_bps", 101),
    ],
)
async def test_bracket_rejects_invalid_reviewed_terms(provider, field, value):
    args, _, _ = provider
    args[field] = value
    with pytest.raises(ValueError):
        await prepare_native_bracket(**args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["missing_dex", "duplicate_dex", "collateral", "wrong_prefix", "fee_scale"]
)
@pytest.mark.parametrize("provider", ["xyz"], indirect=True)
async def test_hip3_does_not_guess_identity_collateral_or_fee_rules(provider, kind):
    args, data, dex = provider
    if kind == "missing_dex":
        data["perpDexs"].pop()
    elif kind == "duplicate_dex":
        data["perpDexs"].append({"name": dex})
    elif kind == "collateral":
        data["metaAndAssetCtxs"][0]["collateralToken"] = 1
    elif kind == "wrong_prefix":
        data["metaAndAssetCtxs"][0]["universe"][0]["name"] = "other:ABC"
    else:
        data["metaAndAssetCtxs"][0]["universe"][0]["deployerFeeScale"] = "4"
    with pytest.raises(ValueError):
        await prepare_native_bracket(**args)


@pytest.mark.asyncio
async def test_hip3_fee_scale_is_included_in_capacity_not_assumed_core_rate(provider):
    args, data, dex = provider
    # Core margin + fees fits; HIP-3's configured 6x protocol fee does not.
    data["activeAssetData"]["availableToTrade"][0] = "10.2"
    data["metaAndAssetCtxs"][0]["universe"][0]["deployerFeeScale"] = "3"
    if dex:
        with pytest.raises(ValueError, match="margin"):
            await prepare_native_bracket(**args)
    else:
        assert (await prepare_native_bracket(**args)).entry.asset_id == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", [1, -1])
async def test_exit_preserves_identity_and_closes_subminimum_without_account_setup(
    provider, direction
):
    args, data, dex = provider
    data["userAbstraction"] = "default"
    asset = 120_000 if dex else 0
    orders = await prepare_native_bracket_exit(
        args["info"],
        coin=args["coin"],
        asset_id=asset,
        remaining=direction * D("0.0199"),
        slippage_bps=50,
    )
    assert orders == (
        PerpIocOrder(
            asset,
            -direction * D("0.019"),
            D("99.625") if direction == 1 else D("100.62"),
        ),
    )
    assert {call.args[0]["type"] for call in args["info"].await_args_list} == {
        "metaAndAssetCtxs"
    } | ({"perpDexs"} if dex else set())


@pytest.mark.asyncio
async def test_exit_refuses_market_reindexing(provider):
    args, data, dex = provider
    asset = 120_000 if dex else 0
    if dex:
        data["perpDexs"][1:] = reversed(data["perpDexs"][1:])
    else:
        data["metaAndAssetCtxs"][0]["universe"].insert(
            0, {"name": "another", "szDecimals": 3}
        )
        data["metaAndAssetCtxs"][1].insert(0, {"midPx": "10"})
    with pytest.raises(ValueError, match="identity"):
        await prepare_native_bracket_exit(
            args["info"],
            coin=args["coin"],
            asset_id=asset,
            remaining=D("0.1"),
            slippage_bps=50,
        )
