from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any

from .constants import (
    ALLOCATION_DEVIATION_THRESHOLD,
    BOROS_ENABLE_MIN_TOTAL_USD,
    FULL_REBALANCE_THRESHOLD,
    PARTIAL_TRIM_THRESHOLD,
)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class HedgeConfig:
    spot_pct: float  # Total allocation to spot (kHYPE + looped HYPE)
    khype_fraction: float  # Fraction of spot allocation for kHYPE
    looped_hype_fraction: float  # Fraction of spot allocation for looped HYPE
    hyperliquid_pct: float  # Allocation to Hyperliquid perp margin
    boros_pct: float  # Allocation to Boros rate lock

    @classmethod
    def default(cls) -> HedgeConfig:
        return cls(
            # New routing:
            # - Send 100% of Arbitrum USDC to Hyperliquid first.
            # - Target ~50% of portfolio value staying on Hyperliquid (margin + cash buffer).
            # - Target ~50% HYPE exposure split between HyperEVM spot (≈45%) and Boros HYPE collateral (≈5%).
            spot_pct=0.45,
            khype_fraction=0.5,
            looped_hype_fraction=0.5,
            hyperliquid_pct=0.50,
            boros_pct=0.05,
        )


# ─────────────────────────────────────────────────────────────────────────────
# INVENTORY (World Model)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Inventory:
    # HyperEVM wallet balances
    hype_hyperevm_balance: float
    hype_hyperevm_value_usd: float
    whype_balance: float  # Wrapped HYPE balance on HyperEVM
    whype_value_usd: float  # Value in USD (1:1 with HYPE)
    khype_balance: float
    khype_value_usd: float
    looped_hype_balance: float
    looped_hype_value_usd: float

    # Arbitrum wallet balances
    usdc_arb_idle: float
    usdt_arb_idle: float
    eth_arb_balance: float  # For gas

    # Arbitrum OFT HYPE (bridged from HyperEVM)
    hype_oft_arb_balance: float
    hype_oft_arb_value_usd: float

    # Hyperliquid venue
    hl_perp_margin: float
    hl_spot_usdc: float
    hl_spot_hype: float
    hl_spot_hype_value_usd: float
    hl_short_size_hype: float
    hl_short_value_usd: float
    hl_unrealized_pnl: float
    hl_withdrawable_usd: float

    # Boros venue
    boros_idle_collateral_isolated: float  # HYPE units
    boros_idle_collateral_cross: float  # HYPE units
    boros_collateral_hype: float  # Total HYPE collateral in Boros
    boros_collateral_usd: float  # USD value (HYPE * hype_price_usd)
    boros_pending_withdrawal_hype: float  # HYPE units
    boros_pending_withdrawal_usd: float  # USD value
    boros_committed_collateral_usd: float  # boros_collateral_usd + hype_oft_arb_value_usd (+ any in-flight OFT bridge)
    boros_position_size: float  # YU notional
    boros_position_value: float  # Unrealized PnL

    # Exchange rates
    khype_to_hype_ratio: float
    looped_hype_to_hype_ratio: float
    hype_price_usd: float

    # Aggregates
    spot_value_usd: float
    total_hype_exposure: float
    total_value: float

    # Optional
    boros_position_market_ids: list[int] | None = None

    # Liquidation detection
    hl_liquidation_detected: bool = False
    hl_liquidation_fills: list[dict] = field(default_factory=list)


@dataclass
class AllocationStatus:
    # Actual values
    spot_value: float
    hl_value: float
    boros_value: float
    idle_value: float
    total_value: float

    # Actual percentages
    spot_pct_actual: float
    hl_pct_actual: float
    boros_pct_actual: float

    # Deviations from target (negative = underallocated)
    spot_deviation: float
    hl_deviation: float
    boros_deviation: float

    # Dollar amounts needed to reach target
    spot_needed_usd: float
    hl_needed_usd: float
    boros_needed_usd: float


@dataclass
class YieldInfo:
    khype_apy: float | None = None
    lhype_apy: float | None = None
    boros_apr: float | None = None
    hl_funding_rate: float | None = None

    khype_expected_yield_usd: float = 0.0
    lhype_expected_yield_usd: float = 0.0
    boros_expected_yield_usd: float = 0.0
    hl_expected_yield_usd: float = 0.0

    total_expected_yield_usd: float = 0.0
    blended_apy: float | None = None


# ─────────────────────────────────────────────────────────────────────────────
# PLANNING ENUMS
# ─────────────────────────────────────────────────────────────────────────────


class Mode(Enum):
    NORMAL = auto()  # Regular operations
    TRIM = auto()  # Risk at 75%+: reduce exposure
    REDEPLOY = auto()  # Risk at 90%+: emergency redeploy


class PlanOp(Enum):
    # Priority 0: Safety/Risk mitigation
    CLOSE_AND_REDEPLOY = "close_and_redeploy"
    PARTIAL_TRIM_SPOT = "partial_trim_spot"
    COMPLETE_PENDING_WITHDRAWAL = "complete_pending_withdrawal"

    # Priority 5: Gas routing (must happen first!)
    ENSURE_GAS_ON_HYPEREVM = "ensure_gas_on_hyperevm"
    ENSURE_GAS_ON_ARBITRUM = "ensure_gas_on_arbitrum"

    # Priority 10: Capital routing
    FUND_BOROS = "fund_boros"
    SEND_USDC_TO_HL = "send_usdc_to_hl"
    BRIDGE_TO_HYPEREVM = "bridge_to_hyperevm"
    TRANSFER_HL_SPOT_TO_HYPEREVM = "transfer_hl_spot_to_hyperevm"
    DEPLOY_EXCESS_HL_MARGIN = "deploy_excess_hl_margin"

    # Priority 20: Position management
    SWAP_HYPE_TO_LST = "swap_hype_to_lst"
    ENSURE_HL_SHORT = "ensure_hl_short"

    # Priority 30: Rate positions
    ENSURE_BOROS_POSITION = "ensure_boros_position"


# ─────────────────────────────────────────────────────────────────────────────
# PLANNING DATACLASSES
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class PlanStep:
    op: PlanOp
    priority: int  # Lower = execute first
    key: str  # Unique identifier for deduplication
    params: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def __lt__(self, other: PlanStep) -> bool:
        return self.priority < other.priority


@dataclass
class PlannerConfig:
    # Risk thresholds
    partial_trim_threshold: float = PARTIAL_TRIM_THRESHOLD
    full_rebalance_threshold: float = FULL_REBALANCE_THRESHOLD

    # Allocation thresholds
    allocation_deviation_threshold: float = ALLOCATION_DEVIATION_THRESHOLD
    position_size_tolerance: float = 0.05

    # Minimum amounts to act on
    min_usdc_action: float = 5.0
    min_usdt_action: float = 1.0
    min_hype_swap: float = 0.1

    # Boros guard
    min_total_for_boros: float = BOROS_ENABLE_MIN_TOTAL_USD

    # Hyperliquid guardrails
    hl_withdrawable_buffer_usd: float = 5.0
    hl_withdraw_for_boros_cooldown_minutes: int = 30
    hl_max_withdraw_for_boros_usd: float = 25.0

    # HL margin management
    hl_target_margin_ratio: float = 0.50
    hl_margin_buffer_ratio: float = 0.15

    # Boros hysteresis
    boros_market_switch_cooldown_hours: int = 24
    boros_apr_improvement_threshold: float = 0.02

    # Boros coverage - target 100%, resize threshold is the hysteresis band
    boros_coverage_target: float = 1.0
    boros_resize_min_excess_usd: float = (
        10.0  # Min YU diff to trigger resize (hysteresis)
    )

    # Delta neutral tolerances
    delta_neutral_rel_tol: float = 0.02
    delta_neutral_abs_tol_hype: float = 0.11

    # Execution limits
    max_steps_per_iteration: int = 5
    max_iterations_per_tick: int = 4
    max_total_steps_per_tick: int = 15

    @classmethod
    def default(cls) -> PlannerConfig:
        return cls()


@dataclass
class PlannerRuntime:
    # Boros market selection (persistent state for hysteresis)
    current_boros_market_id: int | None = None
    current_boros_token_id: int | None = None
    current_boros_collateral_address: str | None = None
    boros_market_selected_at: datetime | None = None

    # Execution tracking
    last_update_at: datetime | None = None
    steps_executed_this_session: int = 0

    # Virtual ledger for same-tick tracking
    committed_usdc_arb: float = 0.0
    committed_usdt_arb: float = 0.0
    funded_boros_this_tick: bool = False

    last_hl_withdraw_for_boros_at: datetime | None = None

    # OFT bridge tracking (HyperEVM native HYPE -> Arbitrum OFT HYPE) so we don't
    # repeatedly fund Boros while the bridge is still settling.
    in_flight_boros_oft_hype: float = 0.0
    in_flight_boros_oft_hype_balance_before: float = 0.0
    in_flight_boros_oft_hype_started_at: datetime | None = None

    # HL state
    leverage_set_for_hype: bool = False

    def reset_virtual_ledger(self) -> None:
        self.committed_usdc_arb = 0.0
        self.committed_usdt_arb = 0.0

    def reset_tick_flags(self) -> None:
        self.funded_boros_this_tick = False

    def available_usdc_arb(self, inv_usdc: float) -> float:
        return max(0.0, inv_usdc - self.committed_usdc_arb)

    def available_usdt_arb(self, inv_usdt: float) -> float:
        return max(0.0, inv_usdt - self.committed_usdt_arb)

    def commit_usdc(self, amount: float) -> None:
        self.committed_usdc_arb += amount

    def commit_usdt(self, amount: float) -> None:
        self.committed_usdt_arb += amount


@dataclass
class DesiredState:
    mode: Mode

    # Target allocations (USD)
    target_spot_usd: float
    target_hl_margin_usd: float
    target_boros_collateral_usd: float

    # Target positions
    target_hype_short_size: float
    # Boros order sizing is in YU. YU units depend on collateral:
    # - HYPE collateral: 1 YU = 1 HYPE
    # - USDT collateral: 1 YU = 1 USDT (≈$1)
    target_boros_position_yu: float

    # Selected Boros market
    boros_market_id: int | None = None
    boros_market_symbol: str | None = None
    boros_tenor_days: float | None = None


@dataclass
class Plan:
    desired_state: DesiredState
    steps: list[PlanStep] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    def add_step(
        self,
        op: PlanOp,
        priority: int,
        key: str,
        params: dict[str, Any] | None = None,
        reason: str = "",
    ) -> None:
        if not any(s.key == key for s in self.steps):
            self.steps.append(
                PlanStep(
                    op=op,
                    priority=priority,
                    key=key,
                    params=params or {},
                    reason=reason,
                )
            )

    def sort_steps(self) -> None:
        self.steps.sort()


# Operations that change inventory and require re-observing
# Note: ENSURE_GAS_ON_HYPEREVM is excluded because it may return early
# without actually changing anything (e.g., "will be provisioned during routing")
INVENTORY_CHANGING_OPS: set[PlanOp] = {
    PlanOp.CLOSE_AND_REDEPLOY,
    PlanOp.PARTIAL_TRIM_SPOT,
    PlanOp.COMPLETE_PENDING_WITHDRAWAL,
    PlanOp.FUND_BOROS,
    PlanOp.SEND_USDC_TO_HL,
    PlanOp.BRIDGE_TO_HYPEREVM,
    PlanOp.TRANSFER_HL_SPOT_TO_HYPEREVM,
    PlanOp.DEPLOY_EXCESS_HL_MARGIN,
    PlanOp.SWAP_HYPE_TO_LST,
    PlanOp.ENSURE_HL_SHORT,
    PlanOp.ENSURE_BOROS_POSITION,
}
