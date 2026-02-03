"""Golden tests for the Boros HYPE planner.

These tests lock in the current Observe→Plan behavior so we can safely refactor
the strategy into smaller modules without changing what it *does*.
"""

from __future__ import annotations

import pytest

from wayfinder_paths.adapters.boros_adapter import BorosMarketQuote
from wayfinder_paths.strategies.boros_hype_strategy.constants import (
    BOROS_HYPE_MARKET_ID,
    BOROS_HYPE_TOKEN_ID,
    HYPE_OFT_ADDRESS,
    MIN_HYPE_GAS,
)
from wayfinder_paths.strategies.boros_hype_strategy.strategy import build_plan
from wayfinder_paths.strategies.boros_hype_strategy.types import (
    AllocationStatus,
    HedgeConfig,
    Inventory,
    PlannerConfig,
    PlannerRuntime,
    PlanOp,
)


def _inv(**overrides) -> Inventory:
    base = {
        "hype_hyperevm_balance": 0.0,
        "hype_hyperevm_value_usd": 0.0,
        "whype_balance": 0.0,
        "whype_value_usd": 0.0,
        "khype_balance": 0.0,
        "khype_value_usd": 0.0,
        "looped_hype_balance": 0.0,
        "looped_hype_value_usd": 0.0,
        "usdc_arb_idle": 0.0,
        "usdt_arb_idle": 0.0,
        "eth_arb_balance": 0.0,
        "hype_oft_arb_balance": 0.0,
        "hype_oft_arb_value_usd": 0.0,
        "hl_perp_margin": 0.0,
        "hl_spot_usdc": 0.0,
        "hl_spot_hype": 0.0,
        "hl_spot_hype_value_usd": 0.0,
        "hl_short_size_hype": 0.0,
        "hl_short_value_usd": 0.0,
        "hl_unrealized_pnl": 0.0,
        "hl_withdrawable_usd": 0.0,
        "boros_idle_collateral_isolated": 0.0,
        "boros_idle_collateral_cross": 0.0,
        "boros_collateral_hype": 0.0,
        "boros_collateral_usd": 0.0,
        "boros_pending_withdrawal_hype": 0.0,
        "boros_pending_withdrawal_usd": 0.0,
        "boros_committed_collateral_usd": 0.0,
        "boros_position_size": 0.0,
        "boros_position_value": 0.0,
        "khype_to_hype_ratio": 1.0,
        "looped_hype_to_hype_ratio": 1.0,
        "hype_price_usd": 25.0,
        "spot_value_usd": 0.0,
        "total_hype_exposure": 0.0,
        "total_value": 0.0,
        "boros_position_market_ids": None,
    }
    base.update(overrides)
    return Inventory(**base)


def _alloc(**overrides) -> AllocationStatus:
    base = {
        "spot_value": 0.0,
        "hl_value": 0.0,
        "boros_value": 0.0,
        "idle_value": 0.0,
        "total_value": 1.0,
        "spot_pct_actual": 0.0,
        "hl_pct_actual": 0.0,
        "boros_pct_actual": 0.0,
        "spot_deviation": 0.0,
        "hl_deviation": 0.0,
        "boros_deviation": 0.0,
        "spot_needed_usd": 0.0,
        "hl_needed_usd": 0.0,
        "boros_needed_usd": 0.0,
    }
    base.update(overrides)
    return AllocationStatus(**base)


def _one_hype_quote() -> list[BorosMarketQuote]:
    return [
        BorosMarketQuote(
            market_id=BOROS_HYPE_MARKET_ID,
            market_address="0x0000000000000000000000000000000000000000",
            symbol="HYPE-USD",
            underlying="HYPE",
            tenor_days=7.0,
            maturity_ts=1_800_000_000,
            collateral_address=HYPE_OFT_ADDRESS,
            collateral_token_id=BOROS_HYPE_TOKEN_ID,
            tick_step=1,
            mid_apr=0.10,
            best_bid_apr=0.095,
            best_ask_apr=0.105,
        )
    ]


def test_build_plan_pending_withdrawal_is_noop():
    inv = _inv(total_value=1000.0, boros_pending_withdrawal_usd=5.0)
    alloc = _alloc(total_value=1000.0)
    runtime = PlannerRuntime()
    hedge_cfg = HedgeConfig(
        spot_pct=0.60,
        khype_fraction=0.5,
        looped_hype_fraction=0.5,
        hyperliquid_pct=0.35,
        boros_pct=0.05,
    )
    config = PlannerConfig()

    plan = build_plan(
        inv=inv,
        alloc=alloc,
        risk_progress=0.0,
        hedge_cfg=hedge_cfg,
        config=config,
        runtime=runtime,
        boros_quotes=_one_hype_quote(),
        pending_withdrawal_completion=False,
    )

    assert plan.steps == []
    assert any("pending boros withdrawal" in msg.lower() for msg in plan.messages)


def test_build_plan_routes_capital_idle_usdc():
    total = 1000.0
    hedge_cfg = HedgeConfig(
        spot_pct=0.60,
        khype_fraction=0.5,
        looped_hype_fraction=0.5,
        hyperliquid_pct=0.35,
        boros_pct=0.05,
    )
    config = PlannerConfig()
    runtime = PlannerRuntime()

    inv = _inv(
        total_value=total,
        usdc_arb_idle=total,
        hype_hyperevm_balance=max(0.0, MIN_HYPE_GAS - 0.01),  # force gas step
    )

    alloc = _alloc(
        spot_value=0.0,
        hl_value=0.0,
        boros_value=0.0,
        idle_value=total,
        total_value=total,
        spot_pct_actual=0.0,
        hl_pct_actual=0.0,
        boros_pct_actual=0.0,
        spot_deviation=-0.60,
        hl_deviation=-0.35,
        boros_deviation=-0.05,
        spot_needed_usd=600.0,
        hl_needed_usd=350.0,
        boros_needed_usd=50.0,
    )

    plan = build_plan(
        inv=inv,
        alloc=alloc,
        risk_progress=0.0,
        hedge_cfg=hedge_cfg,
        config=config,
        runtime=runtime,
        boros_quotes=_one_hype_quote(),
        pending_withdrawal_completion=False,
    )

    ops = [s.op for s in plan.steps]
    assert ops == [
        PlanOp.ENSURE_GAS_ON_HYPEREVM,
        PlanOp.FUND_BOROS,
        PlanOp.SEND_USDC_TO_HL,
        PlanOp.BRIDGE_TO_HYPEREVM,
    ]

    fund_step = plan.steps[1]
    assert abs(fund_step.params["amount_usd"] - 50.0) < 1e-9  # 5% target

    send_step = plan.steps[2]
    assert abs(send_step.params["amount_usd"] - 1000.0) < 1e-9  # send all to HL

    bridge_step = plan.steps[3]
    # Bridge covers both spot deployment and sourcing HYPE on HyperEVM to fund Boros.
    assert abs(bridge_step.params["amount_usd"] - 650.0) < 1e-9
    assert abs(bridge_step.params["reserve_hl_margin_usd"] - 350.0) < 1e-9


def test_build_plan_sizes_hedge_and_boros_position():
    total = 1000.0
    hedge_cfg = HedgeConfig(
        spot_pct=0.60,
        khype_fraction=0.5,
        looped_hype_fraction=0.5,
        hyperliquid_pct=0.35,
        boros_pct=0.05,
    )
    config = PlannerConfig()
    runtime = PlannerRuntime()

    inv = _inv(
        total_value=total,
        spot_value_usd=600.0,
        hl_perp_margin=350.0,
        hl_withdrawable_usd=350.0,
        boros_collateral_hype=50.0 / 24.0,
        boros_collateral_usd=50.0,
        boros_committed_collateral_usd=50.0,
        usdc_arb_idle=0.0,
        usdt_arb_idle=0.0,
        hype_hyperevm_balance=MIN_HYPE_GAS,  # avoid gas + LST swap steps
        total_hype_exposure=10.0,
        hl_short_size_hype=0.0,
        hype_price_usd=24.0,  # avoids .5 rounding in target Boros size
        boros_position_size=0.0,
    )

    alloc = _alloc(
        spot_value=600.0,
        hl_value=350.0,
        boros_value=50.0,
        idle_value=0.0,
        total_value=total,
        spot_pct_actual=0.60,
        hl_pct_actual=0.35,
        boros_pct_actual=0.05,
        spot_deviation=0.0,
        hl_deviation=0.0,
        boros_deviation=0.0,
        spot_needed_usd=0.0,
        hl_needed_usd=0.0,
        boros_needed_usd=0.0,
    )

    plan = build_plan(
        inv=inv,
        alloc=alloc,
        risk_progress=0.0,
        hedge_cfg=hedge_cfg,
        config=config,
        runtime=runtime,
        boros_quotes=_one_hype_quote(),
        pending_withdrawal_completion=False,
    )

    ops = [s.op for s in plan.steps]
    assert ops == [PlanOp.ENSURE_HL_SHORT, PlanOp.ENSURE_BOROS_POSITION]

    hedge_step = plan.steps[0]
    assert hedge_step.params == {"target_size": 10.0, "current_size": 0.0}

    boros_step = plan.steps[1]
    assert boros_step.params["market_id"] == BOROS_HYPE_MARKET_ID
    # Boros rate position is sized off the HYPE short notional (target short = 10 HYPE).
    assert boros_step.params["target_size_yu"] == pytest.approx(10.0)

    # Market selection should persist in runtime (hysteresis)
    assert runtime.current_boros_market_id == BOROS_HYPE_MARKET_ID


def test_build_plan_redeploy_mode_is_single_step():
    total = 1000.0
    runtime = PlannerRuntime()
    hedge_cfg = HedgeConfig(
        spot_pct=0.60,
        khype_fraction=0.5,
        looped_hype_fraction=0.5,
        hyperliquid_pct=0.35,
        boros_pct=0.05,
    )
    config = PlannerConfig()

    inv = _inv(total_value=total, usdc_arb_idle=total)
    alloc = _alloc(total_value=total)

    plan = build_plan(
        inv=inv,
        alloc=alloc,
        risk_progress=0.95,  # above full_rebalance_threshold
        hedge_cfg=hedge_cfg,
        config=config,
        runtime=runtime,
        boros_quotes=_one_hype_quote(),
        pending_withdrawal_completion=False,
    )

    assert [s.op for s in plan.steps] == [PlanOp.CLOSE_AND_REDEPLOY]


def test_build_plan_trim_mode_adds_partial_trim_first():
    total = 1000.0
    runtime = PlannerRuntime()
    hedge_cfg = HedgeConfig(
        spot_pct=0.60,
        khype_fraction=0.5,
        looped_hype_fraction=0.5,
        hyperliquid_pct=0.35,
        boros_pct=0.05,
    )
    config = PlannerConfig()

    inv = _inv(
        total_value=total,
        spot_value_usd=600.0,
        hype_price_usd=25.0,
        hl_short_size_hype=10.0,  # $250 notional short
        hl_perp_margin=50.0,  # intentionally low margin to force trim
        hype_hyperevm_balance=MIN_HYPE_GAS,  # avoid gas step
    )
    alloc = _alloc(total_value=total)

    plan = build_plan(
        inv=inv,
        alloc=alloc,
        risk_progress=0.80,  # TRIM band
        hedge_cfg=hedge_cfg,
        config=config,
        runtime=runtime,
        boros_quotes=_one_hype_quote(),
        pending_withdrawal_completion=False,
    )

    assert plan.steps, "Expected at least one step in TRIM mode"
    assert plan.steps[0].op == PlanOp.PARTIAL_TRIM_SPOT
    assert plan.steps[0].params["trim_pct"] == pytest.approx(0.196875)


def test_build_plan_skips_boros_funding_when_collateral_already_committed():
    total = 1000.0
    runtime = PlannerRuntime()
    hedge_cfg = HedgeConfig(
        spot_pct=0.60,
        khype_fraction=0.5,
        looped_hype_fraction=0.5,
        hyperliquid_pct=0.35,
        boros_pct=0.05,
    )
    config = PlannerConfig()

    inv = _inv(
        total_value=total,
        usdc_arb_idle=0.0,
        usdt_arb_idle=0.0,
        boros_committed_collateral_usd=60.0,  # already above target
        hype_hyperevm_balance=MIN_HYPE_GAS,  # avoid gas step
    )
    alloc = _alloc(total_value=total)

    plan = build_plan(
        inv=inv,
        alloc=alloc,
        risk_progress=0.0,
        hedge_cfg=hedge_cfg,
        config=config,
        runtime=runtime,
        boros_quotes=_one_hype_quote(),
        pending_withdrawal_completion=False,
    )

    assert all(s.op != PlanOp.FUND_BOROS for s in plan.steps)
