from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from wayfinder_paths.strategies.boros_hype_strategy.constants import BOROS_HYPE_TOKEN_ID
from wayfinder_paths.strategies.boros_hype_strategy.strategy import BorosHypeStrategy
from wayfinder_paths.strategies.boros_hype_strategy.types import Inventory


def _inv(*, boros_position_size: float, hype_price_usd: float) -> Inventory:
    return Inventory(
        # HyperEVM wallet balances
        hype_hyperevm_balance=0.0,
        hype_hyperevm_value_usd=0.0,
        whype_balance=0.0,
        whype_value_usd=0.0,
        khype_balance=0.0,
        khype_value_usd=1000.0,
        looped_hype_balance=0.0,
        looped_hype_value_usd=2000.0,
        # Arbitrum wallet balances
        usdc_arb_idle=0.0,
        usdt_arb_idle=0.0,
        eth_arb_balance=0.0,
        # Arbitrum OFT HYPE (bridged from HyperEVM)
        hype_oft_arb_balance=0.0,
        hype_oft_arb_value_usd=0.0,
        # Hyperliquid venue
        hl_perp_margin=0.0,
        hl_spot_usdc=0.0,
        hl_spot_hype=0.0,
        hl_spot_hype_value_usd=0.0,
        hl_short_size_hype=0.0,
        hl_short_value_usd=0.0,
        hl_unrealized_pnl=0.0,
        hl_withdrawable_usd=0.0,
        # Boros venue
        boros_idle_collateral_isolated=0.0,
        boros_idle_collateral_cross=0.0,
        boros_collateral_hype=0.0,
        boros_collateral_usd=0.0,
        boros_pending_withdrawal_hype=0.0,
        boros_pending_withdrawal_usd=0.0,
        boros_committed_collateral_usd=0.0,
        boros_position_size=boros_position_size,
        boros_position_value=0.0,
        # Exchange rates
        khype_to_hype_ratio=1.0,
        looped_hype_to_hype_ratio=1.0,
        hype_price_usd=hype_price_usd,
        # Aggregates
        spot_value_usd=0.0,
        total_hype_exposure=0.0,
        total_value=10000.0,
        boros_position_market_ids=None,
        hl_liquidation_detected=False,
        hl_liquidation_fills=[],
    )


@pytest.mark.asyncio
async def test_get_yield_info_boros_hype_collateral_converts_yu_to_usd(monkeypatch):
    monkeypatch.setattr(
        "wayfinder_paths.strategies.boros_hype_strategy.strategy.fetch_khype_apy",
        AsyncMock(return_value=0.10),
    )
    monkeypatch.setattr(
        "wayfinder_paths.strategies.boros_hype_strategy.strategy.fetch_lhype_apy",
        AsyncMock(return_value=0.20),
    )

    strat = BorosHypeStrategy(config={}, simulation=True)
    strat._planner_runtime.current_boros_token_id = BOROS_HYPE_TOKEN_ID
    strat.boros_adapter = type(
        "FakeBorosAdapter",
        (),
        {"get_active_positions": AsyncMock(return_value=(True, [{"fixedApr": 0.05}]))},
    )()

    inv = _inv(boros_position_size=10.0, hype_price_usd=25.0)
    yi = await strat._get_yield_info(inv)

    assert yi.boros_apr == 0.05
    assert yi.boros_expected_yield_usd == 0.05 * (10.0 * 25.0)
    assert yi.khype_expected_yield_usd == 1000.0 * 0.10
    assert yi.lhype_expected_yield_usd == 2000.0 * 0.20
    assert yi.total_expected_yield_usd == pytest.approx(
        yi.khype_expected_yield_usd
        + yi.lhype_expected_yield_usd
        + yi.boros_expected_yield_usd
    )
    assert yi.blended_apy == pytest.approx(yi.total_expected_yield_usd / 10000.0)


@pytest.mark.asyncio
async def test_get_yield_info_boros_usdt_collateral_treats_yu_as_usd(monkeypatch):
    monkeypatch.setattr(
        "wayfinder_paths.strategies.boros_hype_strategy.strategy.fetch_khype_apy",
        AsyncMock(return_value=0.0),
    )
    monkeypatch.setattr(
        "wayfinder_paths.strategies.boros_hype_strategy.strategy.fetch_lhype_apy",
        AsyncMock(return_value=0.0),
    )

    strat = BorosHypeStrategy(config={}, simulation=True)
    strat._planner_runtime.current_boros_token_id = 3  # e.g., USDT
    strat.boros_adapter = type(
        "FakeBorosAdapter",
        (),
        {
            "get_active_positions": AsyncMock(
                return_value=(True, [{"fixedApr": 0.05, "notionalSizeFloat": 9999}])
            )
        },
    )()

    inv = _inv(boros_position_size=10.0, hype_price_usd=999.0)
    yi = await strat._get_yield_info(inv)

    assert yi.boros_apr == 0.05
    assert yi.boros_expected_yield_usd == 0.05 * 10.0
