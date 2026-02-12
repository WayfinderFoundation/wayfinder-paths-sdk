"""Planner for BorosHypeStrategy.

This module is intentionally mostly-pure: given the current Inventory snapshot
and configuration, it outputs a prioritized list of PlanOp steps.

Extracted from `strategy.py` to make the strategy easier to read/refactor and to
enable independent golden testing.
"""

from __future__ import annotations

from datetime import datetime

from wayfinder_paths.adapters.boros_adapter import BorosMarketQuote

from .constants import (
    BOROS_HYPE_TOKEN_ID,
    BOROS_MIN_DEPOSIT_HYPE,
    BOROS_MIN_TENOR_DAYS,
    MAX_HL_LEVERAGE,
    MIN_HYPE_GAS,
)
from .types import (
    AllocationStatus,
    DesiredState,
    HedgeConfig,
    Inventory,
    Mode,
    Plan,
    PlannerConfig,
    PlannerRuntime,
    PlanOp,
)


def _choose_boros_market(
    quotes: list[BorosMarketQuote],
    runtime: PlannerRuntime,
    config: PlannerConfig,
) -> tuple[int | None, str | None, float | None, int | None, str | None]:
    # Hysteresis to prevent thrashing. Updates runtime state when a new market is selected.
    if not quotes:
        return None, None, None, None, None

    valid_quotes = [q for q in quotes if q.market_id is not None]
    if not valid_quotes:
        return None, None, None, None, None

    # Prefer longer tenors for reduced rollover frequency
    sorted_quotes = sorted(
        valid_quotes,
        key=lambda q: (q.tenor_days or 0, q.mid_apr or 0),
        reverse=True,
    )

    sorted_quotes = [
        q for q in sorted_quotes if (q.tenor_days or 0) >= BOROS_MIN_TENOR_DAYS
    ]
    if not sorted_quotes:
        return None, None, None, None, None

    best = sorted_quotes[0]

    if runtime.current_boros_market_id is None:
        runtime.current_boros_market_id = best.market_id
        runtime.current_boros_token_id = best.collateral_token_id
        runtime.current_boros_collateral_address = best.collateral_address
        runtime.boros_market_selected_at = datetime.utcnow()
        return (
            best.market_id,
            best.symbol,
            best.tenor_days,
            best.collateral_token_id,
            best.collateral_address,
        )

    current = next(
        (q for q in sorted_quotes if q.market_id == runtime.current_boros_market_id),
        None,
    )

    if current is None:  # Current market gone/invalid - switch to best
        runtime.current_boros_market_id = best.market_id
        runtime.current_boros_token_id = best.collateral_token_id
        runtime.current_boros_collateral_address = best.collateral_address
        runtime.boros_market_selected_at = datetime.utcnow()
        return (
            best.market_id,
            best.symbol,
            best.tenor_days,
            best.collateral_token_id,
            best.collateral_address,
        )

    if runtime.boros_market_selected_at:  # Check cooldown
        hours_since = (
            datetime.utcnow() - runtime.boros_market_selected_at
        ).total_seconds() / 3600
        if hours_since < config.boros_market_switch_cooldown_hours:
            return (
                current.market_id,
                current.symbol,
                current.tenor_days,
                current.collateral_token_id,
                current.collateral_address,
            )

    # Only switch if APR improvement exceeds threshold
    if best.market_id != current.market_id:
        current_apr = current.mid_apr or 0
        best_apr = best.mid_apr or 0
        if best_apr - current_apr > config.boros_apr_improvement_threshold:
            runtime.current_boros_market_id = best.market_id
            runtime.current_boros_token_id = best.collateral_token_id
            runtime.current_boros_collateral_address = best.collateral_address
            runtime.boros_market_selected_at = datetime.utcnow()
            return (
                best.market_id,
                best.symbol,
                best.tenor_days,
                best.collateral_token_id,
                best.collateral_address,
            )

    return (
        current.market_id,
        current.symbol,
        current.tenor_days,
        current.collateral_token_id,
        current.collateral_address,
    )


def build_plan(
    inv: Inventory,
    alloc: AllocationStatus,
    risk_progress: float,
    hedge_cfg: HedgeConfig,
    config: PlannerConfig,
    runtime: PlannerRuntime,
    boros_quotes: list[BorosMarketQuote],
    *,
    pending_withdrawal_completion: bool = False,
) -> Plan:
    total = inv.total_value

    # Pending withdrawal: do not run any actions in update().
    if pending_withdrawal_completion or inv.boros_pending_withdrawal_usd > 0:
        plan = Plan(
            desired_state=DesiredState(
                mode=Mode.NORMAL,
                target_spot_usd=0,
                target_hl_margin_usd=0,
                target_boros_collateral_usd=0,
                target_hype_short_size=0,
                target_boros_position_yu=0,
            ),
        )
        plan.messages.append(
            f"Pending Boros withdrawal detected (${inv.boros_pending_withdrawal_usd:.2f}); skipping plan"
        )
        return plan  # Skip all other operations

    # Check for HL liquidation detection
    if inv.hl_liquidation_detected:
        # Log the liquidation alert in plan messages
        plan = Plan(
            desired_state=DesiredState(
                mode=Mode.REDEPLOY,
                target_spot_usd=0,
                target_hl_margin_usd=0,
                target_boros_collateral_usd=0,
                target_hype_short_size=0,
                target_boros_position_yu=0,
            ),
        )
        plan.messages.append(
            "[LIQUIDATION] HL short was liquidated - entering recovery mode"
        )
        # Add close and redeploy step
        plan.add_step(
            PlanOp.CLOSE_AND_REDEPLOY,
            priority=0,
            key="close_and_redeploy_post_liquidation",
            reason="HL position was liquidated - need to rebalance",
        )
        return plan

    # Determine mode based on risk
    if risk_progress >= config.full_rebalance_threshold:
        mode = Mode.REDEPLOY
    elif risk_progress >= config.partial_trim_threshold:
        mode = Mode.TRIM
    else:
        mode = Mode.NORMAL

    # Gap 3: Boros enable/disable + min-deposit floor logic
    # Disable Boros if:
    # - Total AUM below minimum threshold
    # - Pending withdrawal exists (prevents double-collateralizing)
    # Note: funded_boros_this_tick only prevents repeated FUND_BOROS steps,
    # not Boros positions/coverage entirely
    boros_enabled = (
        total >= config.min_total_for_boros and inv.boros_pending_withdrawal_usd <= 0
    )

    # IMPORTANT: Boros has a hard minimum deposit in HYPE terms, which can cause
    # the naive pct-based targets to sum > 100% on smaller portfolios.
    #
    # We treat Boros as a "fixed floor" (min deposit or pct target, whichever is
    # larger) and then allocate the *remaining* budget between spot + HL using
    # their relative weights so targets always fit within total AUM.
    hype_price = float(inv.hype_price_usd or 0.0)

    # If we can't get HYPE price, disable Boros targeting (can't compute min deposit)
    if hype_price <= 0:
        target_boros = 0.0
    elif boros_enabled:
        min_boros_usd = (BOROS_MIN_DEPOSIT_HYPE + 0.01) * hype_price
        target_boros = max(hedge_cfg.boros_pct * total, min_boros_usd)
    else:
        target_boros = 0.0
    target_boros = min(target_boros, total)

    remaining = max(0.0, total - target_boros)
    weight_spot = float(hedge_cfg.spot_pct or 0.0)
    weight_hl = float(hedge_cfg.hyperliquid_pct or 0.0)
    weight_sum = weight_spot + weight_hl
    if weight_sum <= 0:
        weight_spot = 1.0
        weight_hl = 1.0
        weight_sum = 2.0

    target_spot = remaining * (weight_spot / weight_sum)
    target_hl = remaining * (weight_hl / weight_sum)

    # Select Boros market with hysteresis
    (
        market_id,
        market_symbol,
        tenor_days,
        boros_token_id,
        boros_collateral_address,
    ) = _choose_boros_market(boros_quotes, runtime, config)

    # Gap 5: Delta-neutral targeting - use actual spot exposure, not target
    # Short target = current HYPE exposure (to hedge what we actually have)
    target_hype_short = inv.total_hype_exposure

    # Boros position sizing is in YU, but YU units depend on the collateral token:
    # - HYPE collateral: 1 YU = 1 HYPE
    # - USDT collateral: 1 YU = 1 USDT (≈$1)
    #
    # Size the Boros *rate* position off the HYPE short notional. This hedges
    # the funding-rate risk (the short pays/receives funding), while the Boros
    # collateral allocation is handled separately via the min-deposit + pct floor.
    if boros_enabled and market_id:
        short_hype = float(inv.hl_short_size_hype or 0.0)
        if short_hype <= 0.0:
            short_hype = float(target_hype_short or 0.0)
        spot_usd = short_hype * float(hype_price)
        if boros_token_id == BOROS_HYPE_TOKEN_ID:
            target_boros_position_yu = short_hype * float(config.boros_coverage_target)
        else:
            target_boros_position_yu = float(spot_usd) * float(
                config.boros_coverage_target
            )
    else:
        target_boros_position_yu = 0.0

    desired = DesiredState(
        mode=mode,
        target_spot_usd=target_spot,
        target_hl_margin_usd=target_hl,
        target_boros_collateral_usd=target_boros,
        target_hype_short_size=target_hype_short,
        target_boros_position_yu=target_boros_position_yu,
        boros_market_id=market_id,
        boros_market_symbol=market_symbol,
        boros_tenor_days=tenor_days,
    )

    plan = Plan(desired_state=desired)

    # Priority 0: Safety - emergency actions
    if mode == Mode.REDEPLOY:
        plan.add_step(
            PlanOp.CLOSE_AND_REDEPLOY,
            priority=0,
            key="close_and_redeploy",
            reason=f"Risk at {risk_progress:.0%}, triggering full rebalance",
        )
        plan.sort_steps()
        return plan

    if mode == Mode.TRIM:
        # Calculate margin shortfall instead of blunt 25% trim
        # This prevents the whipsaw of "trim → deploy excess margin back"
        short_notional = inv.hl_short_size_hype * inv.hype_price_usd
        required_margin = short_notional * config.hl_target_margin_ratio  # 50% for 2x
        buffer_margin = short_notional * config.hl_margin_buffer_ratio  # 15% buffer
        current_margin = inv.hl_perp_margin
        margin_shortfall = max(0, required_margin + buffer_margin - current_margin)

        # Only trim what's needed (plus 5% buffer for slippage)
        trim_amount_usd = margin_shortfall * 1.05 if margin_shortfall > 5 else 0

        if trim_amount_usd > 0 and inv.spot_value_usd > 0:
            # Convert to percentage of spot value, capped at 50%
            trim_pct = min(trim_amount_usd / inv.spot_value_usd, 0.50)
            if trim_pct > 0.02:  # Only trim if > 2%
                plan.add_step(
                    PlanOp.PARTIAL_TRIM_SPOT,
                    priority=0,
                    key="partial_trim_spot",
                    params={"trim_pct": trim_pct},
                    reason=f"Risk at {risk_progress:.0%} - need ${margin_shortfall:.2f} margin",
                )

    # Priority 5: Gas routing (must happen before capital routing)
    if inv.hype_hyperevm_balance < MIN_HYPE_GAS:
        plan.add_step(
            PlanOp.ENSURE_GAS_ON_HYPEREVM,
            priority=5,
            key="ensure_gas_hyperevm",
            params={"min_hype": MIN_HYPE_GAS},
            reason=f"HyperEVM HYPE balance ({inv.hype_hyperevm_balance:.4f}) below minimum gas",
        )

    # Gap 4: Priority 10 - Capital routing with virtual ledger
    # Use runtime.available_usdc_arb() to avoid double-spending within a tick

    # Fund Boros (via HyperEVM HYPE -> Arbitrum OFT -> Boros cross margin)
    # Check funded_boros_this_tick to prevent repeated funding in same tick.
    #
    # IMPORTANT: Use the Boros target (includes minimum+buffer) instead of pct-based allocation,
    # otherwise small deposits get skipped (Boros min deposit).
    boros_shortfall = 0.0
    if boros_enabled and not runtime.funded_boros_this_tick:
        boros_shortfall = max(
            0.0, target_boros - float(inv.boros_committed_collateral_usd or 0.0)
        )
        if boros_shortfall >= config.min_usdt_action:
            plan.add_step(
                PlanOp.FUND_BOROS,
                priority=10,
                key=f"fund_boros_{boros_shortfall:.2f}",
                params={
                    "amount_usd": float(boros_shortfall),
                    "market_id": int(market_id) if market_id else None,
                    "token_id": int(boros_token_id) if boros_token_id else None,
                    "collateral_address": str(boros_collateral_address or ""),
                },
                reason=f"Funding Boros toward target (${target_boros:.2f}) via HYPE collateral",
            )

    # Send USDC to Hyperliquid ONCE to cover both:
    # - HL margin allocation
    # - HyperEVM spot deployment pipeline (paired fill + bridge)
    need_hl = alloc.hl_deviation < -config.allocation_deviation_threshold
    need_spot = alloc.spot_deviation < -config.allocation_deviation_threshold
    # If Boros needs funding and we don't already have enough HYPE on HyperEVM
    # above the gas reserve, we must source additional HYPE via the HL pipeline.
    bridgeable_hyperevm_hype = max(
        0.0, float(inv.hype_hyperevm_balance or 0.0) - float(MIN_HYPE_GAS)
    ) + max(0.0, float(inv.whype_balance or 0.0))
    bridgeable_hyperevm_usd = bridgeable_hyperevm_hype * float(hype_price)
    boros_bridge_needed_usd = (
        max(0.0, float(boros_shortfall) - float(bridgeable_hyperevm_usd))
        if boros_shortfall >= config.min_usdt_action
        else 0.0
    )

    available_usdc = runtime.available_usdc_arb(inv.usdc_arb_idle)
    if available_usdc > config.min_usdc_action and (
        need_hl or need_spot or boros_bridge_needed_usd > config.min_usdc_action
    ):
        # New routing: always send all Arbitrum USDC to Hyperliquid first.
        amount = float(available_usdc)
        plan.add_step(
            PlanOp.SEND_USDC_TO_HL,
            priority=10,
            key=f"send_usdc_hl_{amount:.0f}",
            params={"amount_usd": amount},
            reason="Routing Arbitrum USDC to Hyperliquid (primary venue)",
        )
        runtime.commit_usdc(amount)  # Mark as committed

    # Bridge to HyperEVM for spot using HL margin (the HL deposit is handled by SEND_USDC_TO_HL).
    bridge_needed_usd = float(alloc.spot_needed_usd or 0.0) + float(
        boros_bridge_needed_usd or 0.0
    )
    if bridge_needed_usd >= config.min_usdc_action and (
        need_spot or boros_bridge_needed_usd > 0
    ):
        plan.add_step(
            PlanOp.BRIDGE_TO_HYPEREVM,
            priority=10,
            key=f"bridge_hyperevm_{bridge_needed_usd:.0f}",
            params={
                "amount_usd": float(bridge_needed_usd),
                # Keep target HL margin in perp while transferring to spot for the paired fill.
                "reserve_hl_margin_usd": float(target_hl),
            },
            reason=f"Spot underallocated by {abs(alloc.spot_deviation):.1%}",
        )

    # Deploy excess HL margin to spot if margin ratio is too high
    if inv.hl_short_size_hype > 0:
        short_notional = inv.hl_short_size_hype * inv.hype_price_usd
        current_margin_ratio = (
            inv.hl_perp_margin / short_notional if short_notional > 0 else 0
        )
        target_margin_ratio = (
            config.hl_target_margin_ratio + config.hl_margin_buffer_ratio
        )  # ~65%
        if current_margin_ratio > target_margin_ratio + 0.10:  # 10% excess
            excess_margin = (
                current_margin_ratio - target_margin_ratio
            ) * short_notional
            if excess_margin > config.min_usdc_action:
                plan.add_step(
                    PlanOp.DEPLOY_EXCESS_HL_MARGIN,
                    priority=13,
                    key=f"deploy_excess_margin_{excess_margin:.0f}",
                    params={"excess_margin_usd": excess_margin},
                    reason=f"Deploy ${excess_margin:.2f} excess HL margin to spot",
                )

    # Transfer HYPE stuck on HL spot to HyperEVM
    if inv.hl_spot_hype > 0.1:
        plan.add_step(
            PlanOp.TRANSFER_HL_SPOT_TO_HYPEREVM,
            priority=14,
            key=f"transfer_hl_spot_{inv.hl_spot_hype:.4f}",
            params={"hype_amount": inv.hl_spot_hype},
            reason=f"Transfer {inv.hl_spot_hype:.4f} HYPE from HL spot to HyperEVM",
        )

    # Priority 20: Position management
    # Swap unallocated HYPE to LSTs (leave MIN_HYPE_GAS for gas)
    swappable_hype = max(0.0, inv.hype_hyperevm_balance - MIN_HYPE_GAS)
    if swappable_hype > config.min_hype_swap:
        plan.add_step(
            PlanOp.SWAP_HYPE_TO_LST,
            priority=20,
            key=f"swap_hype_lst_{swappable_hype:.2f}",
            params={"hype_amount": swappable_hype},
            reason=f"Converting {swappable_hype:.2f} HYPE to yield-bearing LST",
        )

    # Gap 5: Ensure delta neutral short using actual exposure
    # Use combined absolute + relative tolerance to avoid dust chasing
    current_short = inv.hl_short_size_hype
    short_delta = abs(target_hype_short - current_short)

    # Combined tolerance: max of absolute and relative
    tolerance = max(
        config.delta_neutral_abs_tol_hype,
        target_hype_short * config.delta_neutral_rel_tol,
    )

    # If we can't hedge to within tolerance due to margin constraints, trim spot first.
    # (This avoids safety-triggered liquidation loops when HL free margin is too low.)
    hedge_increase_needed = float(target_hype_short) - float(current_short)
    if hedge_increase_needed > 0 and target_hype_short > 0.1:
        hype_price = float(inv.hype_price_usd or 0.0)
        free_margin = float(inv.hl_withdrawable_usd or 0.0)
        max_increase_by_margin = (
            (free_margin * MAX_HL_LEVERAGE) / hype_price if hype_price > 0 else 0.0
        )
        if hedge_increase_needed > max_increase_by_margin + tolerance:
            trim_hype = hedge_increase_needed - (max_increase_by_margin + tolerance)
            trim_usd = trim_hype * hype_price
            if inv.spot_value_usd > 0:
                trim_pct = min(0.50, trim_usd / float(inv.spot_value_usd))
                if trim_pct > 0.02:
                    plan.add_step(
                        PlanOp.PARTIAL_TRIM_SPOT,
                        priority=0,
                        key="partial_trim_spot_for_margin",
                        params={"trim_pct": float(trim_pct)},
                        reason=(
                            "Insufficient HL free margin to hedge within tolerance; "
                            f"trimming spot by ~${trim_usd:.2f}"
                        ),
                    )

    if short_delta > tolerance and target_hype_short > 0.1:
        plan.add_step(
            PlanOp.ENSURE_HL_SHORT,
            priority=20,
            key=f"ensure_hl_short_{target_hype_short:.2f}",
            params={"target_size": target_hype_short, "current_size": current_short},
            reason=f"Delta imbalance: short={current_short:.4f}, exposure={target_hype_short:.4f}",
        )

    # Priority 30: Rate positions (Boros)
    if boros_enabled and market_id:
        # Only try to manage rate positions once collateral is actually available.
        # (In-flight OFT bridges are tracked separately; they can't be used yet.)
        depositable_collateral_hype = float(inv.boros_collateral_hype or 0.0) + float(
            inv.hype_oft_arb_balance or 0.0
        )
        if depositable_collateral_hype < BOROS_MIN_DEPOSIT_HYPE:
            plan.messages.append(
                "Skipping Boros position: collateral not funded yet "
                f"({depositable_collateral_hype:.6f} HYPE)"
            )
            plan.sort_steps()
            return plan

        current_boros_size_yu = float(inv.boros_position_size or 0.0)
        size_diff_yu = abs(target_boros_position_yu - current_boros_size_yu)
        size_diff_usd = size_diff_yu * float(inv.hype_price_usd or 0.0)

        # Trigger if:
        # 1. Position size needs adjustment (resize threshold met)
        # 2. There's isolated collateral that needs to move to cross (market expiry/rotation)
        # 3. There's cross collateral but no position (collateral sitting idle)
        needs_position_resize = size_diff_usd > config.boros_resize_min_excess_usd
        isolated_usd = float(inv.boros_idle_collateral_isolated or 0.0) * float(
            inv.hype_price_usd or 0.0
        )
        cross_usd = float(inv.boros_idle_collateral_cross or 0.0) * float(
            inv.hype_price_usd or 0.0
        )
        has_stranded_isolated = isolated_usd > 0.5  # $0.50 threshold
        has_idle_cross = cross_usd > 1.0 and current_boros_size_yu < 0.01

        if needs_position_resize or has_stranded_isolated or has_idle_cross:
            reason = f"Adjusting Boros position to {target_boros_position_yu:.4f} YU"
            if has_stranded_isolated and not needs_position_resize:
                reason = (
                    "Moving "
                    f"{inv.boros_idle_collateral_isolated:.6f} HYPE (≈${isolated_usd:.2f}) "
                    "from isolated to cross margin"
                )
            elif has_idle_cross and not needs_position_resize:
                reason = (
                    "Deploying "
                    f"{inv.boros_idle_collateral_cross:.6f} HYPE (≈${cross_usd:.2f}) "
                    "idle cross collateral as rate position"
                )
            plan.add_step(
                PlanOp.ENSURE_BOROS_POSITION,
                priority=20,  # Same as ENSURE_HL_SHORT so both execute before re-observe
                key=f"ensure_boros_pos_{target_boros_position_yu:.4f}",
                params={
                    "market_id": market_id,
                    "target_size_yu": target_boros_position_yu,
                },
                reason=reason,
            )

    plan.sort_steps()
    return plan
