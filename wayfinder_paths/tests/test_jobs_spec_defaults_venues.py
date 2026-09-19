"""A harnessed job's contract follows the venue it names."""

from __future__ import annotations

import pytest

from wayfinder_paths.jobs.execution.spec_defaults import (
    HARNESSED_VENUES,
    harnessed_execution_params,
    harnessed_execution_spec,
)

WETH_BASE = "0x4200000000000000000000000000000000000006"


def test_perps_stay_the_default() -> None:
    spec = harnessed_execution_spec(["BTC"], "5m")
    params = harnessed_execution_params(["BTC"], leverage=3)
    assert spec["market_kind"] == "perp" and spec["venues"] == ["hyperliquid"]
    assert "token_resolution" not in spec["data_contract"]
    assert params["venue"] == "hyperliquid"
    assert (params["fee_bps"], params["slippage_bps"], params["leverage"]) == (
        4.5,
        3.5,
        3,
    )


def test_onchain_jobs_trade_token_ids_on_supported_intervals() -> None:
    pins = {"ethereum-robinhood": {"chain_id": 4663, "address": WETH_BASE}}
    spec = harnessed_execution_spec(
        ["ethereum-robinhood"], "1h", venue="onchain", token_resolution=pins
    )
    params = harnessed_execution_params(["ethereum-robinhood"], venue="onchain")
    assert spec["market_kind"] == "spot" and spec["venues"] == ["onchain"]
    assert spec["data_contract"]["token_resolution"] == pins
    assert spec["data_contract"]["symbols"] == ["ethereum-robinhood"]
    assert params["venue"] == "onchain"
    assert (params["fee_bps"], params["slippage_bps"]) == (30.0, 50.0)
    assert "leverage" not in params


def test_hyperliquid_spot_jobs_trade_pairs() -> None:
    spec = harnessed_execution_spec(["HYPE/USDC"], "15m", venue="hyperliquid_spot")
    params = harnessed_execution_params(["HYPE/USDC"], venue="hyperliquid_spot")
    assert spec["market_kind"] == "spot" and spec["venues"] == ["hyperliquid_spot"]
    assert (params["fee_bps"], params["slippage_bps"]) == (7.0, 10.0)


@pytest.mark.parametrize(
    "symbols, interval, venue, needle",
    [
        (["BTC"], "1h", "onchain", "not a token id"),
        (["ethereum-base"], "2h", "onchain", "1m|5m|15m|1h|4h|1d"),
        (["HYPE"], "1h", "hyperliquid_spot", "not a spot pair"),
        (["BTC"], "1h", "binance", "unknown harnessed venue"),
    ],
)
def test_refusals_name_the_fix(symbols, interval, venue, needle) -> None:
    with pytest.raises(ValueError, match=needle):
        harnessed_execution_spec(symbols, interval, venue=venue)


def test_spot_venues_take_no_leverage() -> None:
    with pytest.raises(ValueError, match="takes no leverage"):
        harnessed_execution_params(["ethereum-base"], venue="onchain", leverage=2)
    assert set(HARNESSED_VENUES) == {"hyperliquid", "onchain", "hyperliquid_spot"}
