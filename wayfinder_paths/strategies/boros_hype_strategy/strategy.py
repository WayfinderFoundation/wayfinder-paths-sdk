from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from eth_account import Account
from loguru import logger

from wayfinder_paths.adapters.balance_adapter.adapter import BalanceAdapter
from wayfinder_paths.adapters.boros_adapter import BorosAdapter, BorosMarketQuote
from wayfinder_paths.adapters.brap_adapter.adapter import BRAPAdapter
from wayfinder_paths.adapters.hyperliquid_adapter.adapter import (
    HyperliquidAdapter,
)
from wayfinder_paths.adapters.hyperliquid_adapter.paired_filler import MIN_NOTIONAL_USD
from wayfinder_paths.core.constants.hyperliquid import (
    DEFAULT_HYPERLIQUID_BUILDER_FEE_TENTHS_BP,
    HYPE_FEE_WALLET,
)
from wayfinder_paths.core.strategies import (
    OPAConfig,
    OPALoopMixin,
    StatusDict,
    StatusTuple,
    Strategy,
)
from wayfinder_paths.core.strategies import (
    Plan as OPAPlan,
)
from wayfinder_paths.core.strategies import (
    PlanStep as OPAPlanStep,
)
from wayfinder_paths.core.strategies.descriptors import (
    Complexity,
    Directionality,
    Frequency,
    StratDescriptor,
    TokenExposure,
    Volatility,
)

from .boros_ops_mixin import BorosHypeBorosOpsMixin
from .constants import (
    BOROS_HYPE_TOKEN_ID,
    BOROS_MIN_DEPOSIT_HYPE,
    ETH_ARB,
    MAX_HL_LEVERAGE,
    MIN_HYPE_GAS,
    MIN_NET_DEPOSIT,
    USDC_ARB,
    USDT_ARB,
)
from .hyperevm_ops_mixin import BorosHypeHyperEvmOpsMixin
from .hyperliquid_ops_mixin import BorosHypeHyperliquidOpsMixin
from .planner import build_plan
from .risk_ops_mixin import BorosHypeRiskOpsMixin
from .snapshot_mixin import BorosHypeSnapshotMixin, fetch_khype_apy, fetch_lhype_apy
from .types import (
    INVENTORY_CHANGING_OPS,
    AllocationStatus,
    HedgeConfig,
    Inventory,
    PlannerConfig,
    PlannerRuntime,
    PlanOp,
    YieldInfo,
)
from .withdraw_mixin import BorosHypeWithdrawMixin


class BorosHypeStrategy(
    BorosHypeSnapshotMixin,
    BorosHypeWithdrawMixin,
    BorosHypeHyperEvmOpsMixin,
    BorosHypeBorosOpsMixin,
    BorosHypeHyperliquidOpsMixin,
    BorosHypeRiskOpsMixin,
    OPALoopMixin[Inventory, PlanOp],
    Strategy,
):
    name = "HYPE Spot + Hyperliquid + Boros Strategy"

    INFO = StratDescriptor(
        description=(
            "Delta-neutral HYPE yield strategy combining three legs: "
            "1) Spot yield from kHYPE and looped HYPE on HyperEVM, "
            "2) Hyperliquid HYPE perp short for delta hedging, "
            "3) Boros fixed-rate markets for rate locking. "
            "Deposits are Arbitrum USDC + ETH (for gas). "
            "The strategy routes capital to all venues automatically."
        ),
        summary=(
            "Earns yield from HYPE spot (kHYPE/lHYPE) while hedging price exposure "
            "via Hyperliquid perp shorts and locking in rates on Boros."
        ),
        risk_description=(
            "Higher risk than pure funding rate strategies due to lack of limit orders "
            "on spot assets - entries and exits occur at market prices which can result "
            "in slippage during volatile conditions. Additional smart contract risk across "
            "multiple protocols (HyperEVM staking, Hyperliquid perps, Boros fixed-rate markets). "
            "Liquidation risk on the perp short if funding diverges significantly. "
            "Bridge risk when moving assets between chains."
        ),
        gas_token_symbol="ETH",
        gas_token_id="ethereum-arbitrum",
        deposit_token_id="usd-coin-arbitrum",
        minimum_net_deposit=MIN_NET_DEPOSIT,
        gas_maximum=0.1,
        gas_threshold=0.03,
        volatility=Volatility.LOW,
        volatility_description="Delta-neutral strategy minimizes price exposure.",
        directionality=Directionality.DELTA_NEUTRAL,
        directionality_description=(
            "Long HYPE spot (kHYPE/lHYPE) hedged by short HYPE perp on Hyperliquid."
        ),
        complexity=Complexity.HIGH,
        complexity_description=(
            "Complex multi-chain, multi-venue strategy requiring careful orchestration."
        ),
        token_exposure=TokenExposure.ALTS,
        token_exposure_description="Exposed to HYPE through hedged yield positions.",
        frequency=Frequency.LOW,
        frequency_description="Positions held for weeks to capture yield and funding.",
        return_drivers=[
            "kHYPE staking yield",
            "lHYPE loop yield",
            "funding rate",
            "Boros fixed rate",
        ],
        config={
            "deposit": {
                "description": "Deposit USDC and ETH to fund the strategy.",
                "parameters": {
                    "main_token_amount": {
                        "type": "float",
                        "unit": "USDC",
                        "description": "Amount of USDC (Arbitrum) to deposit.",
                        "minimum": MIN_NET_DEPOSIT,
                    },
                    "gas_token_amount": {
                        "type": "float",
                        "unit": "ETH",
                        "description": "Amount of ETH (Arbitrum) for gas on multiple chains.",
                        "minimum": 0.0,
                        "maximum": 0.1,
                    },
                },
            },
            "update": {
                "description": "Run the OPA control loop to manage positions.",
            },
            "withdraw": {
                "description": "Close all positions and return funds to main wallet.",
            },
        },
    )

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(
            config=config,
            **kwargs,
        )

        self._config = config or {}
        # Keep the base Strategy config in sync so helpers like
        # _get_strategy_wallet_address() work even when config=None.
        self.config = self._config

        # Configuration
        self.hedge_cfg = HedgeConfig.default()
        self._planner_config = PlannerConfig.default()
        self._planner_runtime = PlannerRuntime()
        # Hyperliquid builder attribution is mandatory and fixed to HYPE_FEE_WALLET.
        expected_builder = HYPE_FEE_WALLET.lower()
        cfg_builder_fee = self._config.get("builder_fee")
        fee = None
        if isinstance(cfg_builder_fee, dict):
            cfg_builder = str(cfg_builder_fee.get("b") or "").strip()
            if cfg_builder and cfg_builder.lower() != expected_builder:
                raise ValueError(
                    f"builder_fee.b must be {expected_builder} (got {cfg_builder})"
                )
            if cfg_builder_fee.get("f") is not None:
                fee = cfg_builder_fee.get("f")

        if fee is None:
            fee = DEFAULT_HYPERLIQUID_BUILDER_FEE_TENTHS_BP
        try:
            fee_i = int(fee)
        except (TypeError, ValueError) as exc:
            raise ValueError("builder_fee.f must be an int (tenths of bp)") from exc
        if fee_i <= 0:
            raise ValueError("builder_fee.f must be > 0 (tenths of bp)")

        self.builder_fee: dict[str, Any] = {"b": expected_builder, "f": fee_i}

        # OPA context (populated in observe())
        self._opa_alloc: AllocationStatus | None = None
        self._opa_risk_progress: float = 0.0
        self._opa_boros_quotes: list[BorosMarketQuote] = []
        self._opa_yield_info: YieldInfo | None = None

        # Pending withdrawal state tracking
        self._opa_pending_withdrawal: bool = False
        self._opa_completed_pending_withdrawal_this_tick: bool = False

        # Emergency flags (best-effort, per-process)
        self._failsafe_triggered: bool = False
        self._failsafe_message: str | None = None

        # Adapters (initialized in setup)
        self.boros_adapter: BorosAdapter | None = None
        self.hyperliquid_adapter: HyperliquidAdapter | None = None
        self.balance_adapter: BalanceAdapter | None = None
        self.brap_adapter: BRAPAdapter | None = None
        self._sign_callback = None

    def _require_adapters(self, *adapter_attrs: str) -> tuple[bool, str]:
        error_messages = {
            "balance_adapter": "Balance adapter not configured",
            "hyperliquid_adapter": "Hyperliquid adapter not configured",
            "brap_adapter": "BRAP adapter not configured",
            "boros_adapter": "Boros adapter not configured",
        }
        for attr in adapter_attrs:
            if getattr(self, attr, None) is None:
                return False, error_messages.get(attr, f"{attr} not configured")
        return True, ""

    def _require_strategy_wallet_address(self) -> tuple[bool, str]:
        try:
            return True, self._get_strategy_wallet_address()
        except Exception:
            return False, "No strategy wallet address configured"

    def _make_sign_callback(self, private_key: str):
        account = Account.from_key(private_key)

        async def sign_callback(transaction: dict) -> bytes:
            signed = account.sign_transaction(transaction)
            return signed.raw_transaction

        return sign_callback

    async def setup(self) -> None:
        strategy_wallet = self._config.get("strategy_wallet", {})
        main_wallet = self._config.get("main_wallet", {})
        user_address = strategy_wallet.get("address") if strategy_wallet else None

        # Create signing callbacks from wallet private keys
        strategy_pk = (
            strategy_wallet.get("private_key") or strategy_wallet.get("private_key_hex")
            if strategy_wallet
            else None
        )
        main_pk = (
            main_wallet.get("private_key") or main_wallet.get("private_key_hex")
            if main_wallet
            else None
        )

        self._sign_callback = (
            self._make_sign_callback(strategy_pk) if strategy_pk else None
        )
        main_sign_callback = self._make_sign_callback(main_pk) if main_pk else None

        # Initialize Boros adapter
        self.boros_adapter = BorosAdapter(
            config=self._config,
            sign_callback=self._sign_callback,
            user_address=user_address,
        )

        self.hyperliquid_adapter = HyperliquidAdapter(
            config=self._config,
            sign_callback=self.strategy_sign_typed_data,
        )

        self.balance_adapter = BalanceAdapter(
            config=self._config,
            main_wallet_signing_callback=main_sign_callback,
            strategy_wallet_signing_callback=self._sign_callback,
        )
        self.brap_adapter = BRAPAdapter(
            config=self._config,
            strategy_wallet_signing_callback=self._sign_callback,
        )

        logger.info("BorosHypeStrategy setup complete")

    async def analyze(
        self, deposit_usdc: float = 1000.0, verbose: bool = True
    ) -> dict[str, Any]:
        # Read-only market analysis returning Boros fixed-rate markets for HYPE
        # Client ownership: BorosAdapter owns the client; we require adapter to be set up
        if not self.boros_adapter:
            return {
                "success": False,
                "error": "BorosAdapter not initialized - call setup() first",
                "deposit_usdc": float(deposit_usdc),
                "hype_markets": [],
            }
        client = self.boros_adapter.boros_client
        try:
            markets = await client.list_markets(is_whitelisted=True, skip=0, limit=250)
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "error": str(exc),
                "deposit_usdc": float(deposit_usdc),
                "hype_markets": [],
            }

        out: list[dict[str, Any]] = []
        for m in markets:
            if not isinstance(m, dict):
                continue
            _im = m.get("imData")
            im: dict[str, Any] = _im if isinstance(_im, dict) else {}
            _meta = m.get("metadata")
            meta: dict[str, Any] = _meta if isinstance(_meta, dict) else {}
            _platform = m.get("platform")
            platform_obj: dict[str, Any] = (
                _platform if isinstance(_platform, dict) else {}
            )
            _data = m.get("data")
            data: dict[str, Any] = _data if isinstance(_data, dict) else {}

            name = str(im.get("name") or meta.get("marketName") or "").strip()
            symbol = str(im.get("symbol") or "").strip()
            platform = str(
                platform_obj.get("name") or meta.get("platformName") or ""
            ).strip()
            asset_symbol = str(
                meta.get("assetSymbol") or meta.get("name") or ""
            ).strip()

            hay = " ".join([name, symbol, platform, asset_symbol]).upper()
            if "HYPE" not in hay:
                continue

            try:
                market_id = int(m.get("marketId") or m.get("id") or 0)
            except Exception:
                market_id = 0

            maturity_ts = im.get("maturity")
            try:
                maturity_ts_i = int(maturity_ts) if maturity_ts is not None else None
            except Exception:
                maturity_ts_i = None

            def _f(x: Any) -> float | None:
                try:
                    if x is None:
                        return None
                    return float(x)
                except Exception:
                    return None

            out.append(
                {
                    "market_id": market_id,
                    "name": name or None,
                    "symbol": symbol or None,
                    "platform": platform or None,
                    "asset_symbol": asset_symbol.upper() if asset_symbol else None,
                    "maturity_ts": maturity_ts_i,
                    "time_to_maturity_s": _f(data.get("timeToMaturity")),
                    "mark_apr": _f(data.get("markApr")),
                    "mid_apr": _f(data.get("midApr")),
                    "best_bid_apr": _f(data.get("bestBid")),
                    "best_ask_apr": _f(data.get("bestAsk")),
                    "floating_apr": _f(data.get("floatingApr")),
                }
            )

        out.sort(key=lambda x: (x.get("maturity_ts") or 0, x.get("market_id") or 0))

        primary = next(
            (
                x
                for x in out
                if (x.get("platform") or "").lower() == "hyperliquid"
                and (x.get("asset_symbol") or "").upper() == "HYPE"
            ),
            None,
        )

        return {
            "success": True,
            "deposit_usdc": float(deposit_usdc),
            "primary_market": primary,
            "hype_markets": out,
            "notes": {
                "apr_units": "Decimals (0.10 = 10% APR).",
                "lock_hint": "The fixed rate to lock is typically around best_bid_apr ↔ best_ask_apr (depends on side).",
            },
        }

    @property
    def opa_config(self) -> OPAConfig:
        return OPAConfig(
            max_iterations_per_tick=self._planner_config.max_iterations_per_tick,
            max_steps_per_iteration=self._planner_config.max_steps_per_iteration,
            max_total_steps_per_tick=self._planner_config.max_total_steps_per_tick,
        )

    def plan(self, inventory: Inventory) -> OPAPlan[PlanOp]:
        plan = build_plan(
            inv=inventory,
            alloc=self._opa_alloc or self._get_allocation_status(inventory),
            risk_progress=self._opa_risk_progress,
            hedge_cfg=self.hedge_cfg,
            config=self._planner_config,
            runtime=self._planner_runtime,
            boros_quotes=self._opa_boros_quotes,
            pending_withdrawal_completion=self._opa_pending_withdrawal,
        )

        # Convert to OPA Plan format
        opa_plan = OPAPlan[PlanOp](
            steps=[
                OPAPlanStep(
                    op=step.op,
                    priority=step.priority,
                    key=step.key,
                    params=step.params,
                    reason=step.reason,
                )
                for step in plan.steps
            ],
            desired_state={
                "mode": plan.desired_state.mode.name,
                "target_spot_usd": plan.desired_state.target_spot_usd,
                "target_hl_margin_usd": plan.desired_state.target_hl_margin_usd,
                "boros_market_id": plan.desired_state.boros_market_id,
            },
        )
        return opa_plan

    async def execute_step(
        self, step: OPAPlanStep[PlanOp], inventory: Inventory
    ) -> tuple[bool, str]:
        op = step.op
        params = step.params

        logger.info(f"Executing {op.name}: {step.reason}")

        if self._failsafe_triggered:
            return False, "Failsafe already triggered; skipping further actions"

        # Dispatch to handlers - complete mapping for all PlanOps
        handlers = {
            # Priority 0: Safety/Risk mitigation
            PlanOp.CLOSE_AND_REDEPLOY: self._close_and_redeploy,
            PlanOp.PARTIAL_TRIM_SPOT: self._partial_trim_spot,
            PlanOp.COMPLETE_PENDING_WITHDRAWAL: self._complete_pending_withdrawal,
            # Priority 5: Gas routing
            PlanOp.ENSURE_GAS_ON_HYPEREVM: self._ensure_gas_on_hyperevm,
            PlanOp.ENSURE_GAS_ON_ARBITRUM: self._ensure_gas_on_arbitrum,
            # Priority 10-14: Capital routing
            PlanOp.FUND_BOROS: self._fund_boros,
            PlanOp.SEND_USDC_TO_HL: self._send_usdc_to_hl,
            PlanOp.BRIDGE_TO_HYPEREVM: self._bridge_to_hyperevm,
            PlanOp.DEPLOY_EXCESS_HL_MARGIN: self._deploy_excess_hl_margin,
            PlanOp.TRANSFER_HL_SPOT_TO_HYPEREVM: self._transfer_hl_spot_to_hyperevm,
            # Priority 20: Position management
            PlanOp.SWAP_HYPE_TO_LST: self._swap_hype_to_lst,
            PlanOp.ENSURE_HL_SHORT: self._ensure_hl_short,
            # Priority 30: Rate positions
            PlanOp.ENSURE_BOROS_POSITION: self._ensure_boros_position,
        }

        handler = handlers.get(op)
        if handler:
            return await handler(params, inventory)

        logger.warning(f"No handler implemented for {op.name}")
        return False, f"No handler for {op.name}"

    def get_inventory_changing_ops(self) -> set[PlanOp]:
        return INVENTORY_CHANGING_OPS

    async def on_loop_start(self) -> tuple[bool, str] | None:
        # Pre-loop setup: reset tick flags, check pending withdrawals, approve builder fee
        self._planner_runtime.reset_virtual_ledger()
        self._planner_runtime.reset_tick_flags()
        self._planner_runtime.last_update_at = datetime.utcnow()
        self._opa_completed_pending_withdrawal_this_tick = False
        self._failsafe_triggered = False
        self._failsafe_message = None

        # Pre-check for pending withdrawal from Boros
        # This allows build_plan() to prioritize withdrawal completion
        self._opa_pending_withdrawal = False
        if self.boros_adapter:
            try:
                token_id = (
                    self._planner_runtime.current_boros_token_id or BOROS_HYPE_TOKEN_ID
                )
                (
                    ok_pending,
                    pending_amt,
                ) = await self.boros_adapter.get_pending_withdrawal_amount(
                    token_id=int(token_id),
                    token_decimals=18,
                )
                if ok_pending and pending_amt > 0:
                    self._opa_pending_withdrawal = True
                    logger.info(
                        f"Pending Boros withdrawal detected: {pending_amt:.6f} collateral units"
                    )
                    # We do NOT perform any OPA actions while Boros withdrawals are pending.
                    # Withdrawal settlement can take 10-20 minutes; running update() in the
                    # meantime risks redeploying or hedging against an in-flight withdrawal.
                    return (
                        True,
                        f"Pending Boros withdrawal ({pending_amt:.6f}) - skipping update tick",
                    )
            except Exception as e:
                logger.warning(f"Failed to check pending withdrawal: {e}")

        # Ensure Hyperliquid builder fee is approved (required prerequisite).
        if self.hyperliquid_adapter and self.builder_fee:
            ok_addr, address = self._require_strategy_wallet_address()
            if ok_addr:
                ok, msg = await self.hyperliquid_adapter.ensure_builder_fee_approved(
                    address=address,
                    builder_fee=self.builder_fee,
                )
                if not ok:
                    # Hyperliquid requires a first deposit before certain actions (including builder fee approval).
                    # Defer approval so the strategy can still route the initial deposit to HL.
                    if "must deposit before performing actions" in str(msg).lower():
                        logger.warning(
                            f"Deferring Hyperliquid builder fee approval until after first deposit: {msg}"
                        )
                    else:
                        return (
                            False,
                            f"Failed to approve Hyperliquid builder fee: {msg}",
                        )

        # Ensure Hyperliquid HYPE leverage is set *before* any paired fills/perp orders.
        if self.hyperliquid_adapter and not self._planner_runtime.leverage_set_for_hype:
            ok_addr, address = self._require_strategy_wallet_address()
            if ok_addr:
                ok_lev, lev_msg = await self._ensure_hl_hype_leverage_set(address)
                if not ok_lev:
                    return False, lev_msg

        return None  # Continue with loop

    async def on_step_executed(
        self,
        step: OPAPlanStep[PlanOp],
        success: bool,
        message: str,
    ) -> None:
        if step.op == PlanOp.COMPLETE_PENDING_WITHDRAWAL and success:
            # Mark that we completed the pending withdrawal this tick
            self._opa_pending_withdrawal = False
            self._opa_completed_pending_withdrawal_this_tick = True
            logger.info("Pending withdrawal completed this tick")

        if step.op == PlanOp.FUND_BOROS and success:
            # Prevent repeated Boros funding within same tick
            self._planner_runtime.funded_boros_this_tick = True
            logger.debug("Boros funded this tick - preventing duplicate funding")

    def should_stop_early(
        self, inventory: Inventory, iteration: int
    ) -> tuple[bool, str] | None:
        if inventory.boros_pending_withdrawal_usd > 1.0:
            self._opa_pending_withdrawal = True
            return (
                True,
                f"Pending Boros withdrawal (${inventory.boros_pending_withdrawal_usd:.2f}) - skipping update tick",
            )

        # Stop if we completed a pending withdrawal this tick
        # (we don't want to redeploy capital in the same tick)
        if self._opa_completed_pending_withdrawal_this_tick:
            return (
                True,
                "Pending withdrawal completed - stopping to avoid same-tick redeployment",
            )

        if inventory.hype_price_usd <= 0:
            return True, "HYPE price unavailable - skipping update tick"

        return None

    def _get_allocation_status(self, inv: Inventory) -> AllocationStatus:
        total_value = float(inv.total_value)
        denom = total_value if total_value > 0 else 1.0

        spot_actual = inv.spot_value_usd
        hl_actual = inv.hl_perp_margin + inv.hl_spot_usdc
        boros_actual = inv.boros_committed_collateral_usd
        idle_actual = inv.usdc_arb_idle + inv.usdt_arb_idle

        # Compute allocation targets with a Boros floor (min deposit) and a
        # budget constraint so targets always fit within total AUM.
        boros_target = 0.0
        if total_value >= self._planner_config.min_total_for_boros:
            hype_price = float(inv.hype_price_usd or 0.0)
            # If we can't get HYPE price, skip Boros targeting (can't compute min deposit)
            if hype_price > 0:
                boros_target = max(
                    float(self.hedge_cfg.boros_pct) * total_value,
                    (BOROS_MIN_DEPOSIT_HYPE + 0.01) * hype_price,
                )
                boros_target = min(boros_target, total_value)

        remaining = max(0.0, total_value - boros_target)
        weight_spot = float(self.hedge_cfg.spot_pct or 0.0)
        weight_hl = float(self.hedge_cfg.hyperliquid_pct or 0.0)
        weight_sum = weight_spot + weight_hl
        if weight_sum <= 0:
            weight_spot = 1.0
            weight_hl = 1.0
            weight_sum = 2.0

        spot_target = remaining * (weight_spot / weight_sum)
        hl_target = remaining * (weight_hl / weight_sum)

        spot_pct_target = spot_target / denom
        hl_pct_target = hl_target / denom
        boros_pct_target = boros_target / denom

        return AllocationStatus(
            spot_value=spot_actual,
            hl_value=hl_actual,
            boros_value=boros_actual,
            idle_value=idle_actual,
            total_value=total_value,
            spot_pct_actual=spot_actual / denom,
            hl_pct_actual=hl_actual / denom,
            boros_pct_actual=boros_actual / denom,
            spot_deviation=(spot_actual / denom) - spot_pct_target,
            hl_deviation=(hl_actual / denom) - hl_pct_target,
            boros_deviation=(boros_actual / denom) - boros_pct_target,
            spot_needed_usd=max(0.0, spot_target - spot_actual),
            hl_needed_usd=max(0.0, hl_target - hl_actual),
            boros_needed_usd=max(0.0, boros_target - boros_actual),
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Risk & Invariant Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _delta_neutral_ok(self, inv: Inventory) -> tuple[bool, str]:
        # Combined absolute + relative tolerance to avoid dust chasing
        exp = float(inv.total_hype_exposure or 0.0)
        short = float(inv.hl_short_size_hype or 0.0)
        if exp < 0.1:
            if abs(short) < 0.01:
                return True, "No meaningful HYPE exposure"
            return (
                False,
                f"Unexpected HL short without spot exposure (short={short:.4f} HYPE, spot={exp:.4f} HYPE)",
            )
        diff = abs(short - exp)

        # Combined tolerance: max of absolute and relative
        tol = max(
            self._planner_config.delta_neutral_abs_tol_hype,  # ~$2 at $25 HYPE
            exp * self._planner_config.delta_neutral_rel_tol,  # 2% relative
        )
        ok = diff <= tol
        return ok, f"Δ={diff:.4f} tol={tol:.4f} (spot={exp:.4f}, short={short:.4f})"

    def _boros_coverage_ok(
        self, inv: Inventory, quotes: list[BorosMarketQuote] | None = None
    ) -> tuple[bool, str]:
        if not self._planner_runtime.current_boros_market_id:
            return True, "No Boros market selected"

        if inv.total_value < self._planner_config.min_total_for_boros:
            return True, "Below Boros minimum - skipping coverage check"

        hype_price = float(inv.hype_price_usd or 0.0)
        if hype_price <= 0:
            return True, "HYPE price unavailable"

        short_hype = float(inv.hl_short_size_hype or 0.0)
        spot_usd = short_hype * hype_price
        if spot_usd < 10:
            return True, "Target Boros position too small"

        token_id = int(
            self._planner_runtime.current_boros_token_id or BOROS_HYPE_TOKEN_ID
        )
        current_position_yu = float(inv.boros_position_size or 0.0)

        if token_id == BOROS_HYPE_TOKEN_ID:
            target_position_yu = short_hype * float(
                self._planner_config.boros_coverage_target
            )
            diff_yu = abs(current_position_yu - target_position_yu)
            diff_usd = diff_yu * hype_price
        else:
            target_position_yu = spot_usd * float(
                self._planner_config.boros_coverage_target
            )  # 1 YU = $1
            diff_yu = abs(current_position_yu - target_position_yu)
            diff_usd = diff_yu

        # Use resize threshold as hysteresis band
        ok = diff_usd <= self._planner_config.boros_resize_min_excess_usd

        return ok, f"diff≈${diff_usd:.2f} (current={current_position_yu:.4f} YU)"

    # ─────────────────────────────────────────────────────────────────────────
    # Step Handlers
    # ─────────────────────────────────────────────────────────────────────────

    # Implemented in mixins:
    # - boros_ops_mixin.py
    # - hyperliquid_ops_mixin.py
    # - hyperevm_ops_mixin.py
    # - risk_ops_mixin.py

    # ─────────────────────────────────────────────────────────────────────────
    # Strategy Interface
    # ─────────────────────────────────────────────────────────────────────────

    async def deposit(
        self,
        main_token_amount: float = 0.0,
        gas_token_amount: float = 0.0,
        **kwargs,
    ) -> StatusTuple:
        if main_token_amount < MIN_NET_DEPOSIT:
            return (
                False,
                f"Minimum deposit is ${MIN_NET_DEPOSIT:.0f} USDC, got ${main_token_amount:.2f}",
            )

        if not self.balance_adapter:
            return False, "Balance adapter not configured"

        main_wallet = self._config.get("main_wallet")
        strategy_wallet = self._config.get("strategy_wallet")
        main_address = (
            main_wallet.get("address") if isinstance(main_wallet, dict) else None
        )
        strategy_address = (
            strategy_wallet.get("address")
            if isinstance(strategy_wallet, dict)
            else None
        )

        if not main_address or not strategy_address:
            return False, "main_wallet or strategy_wallet missing address"

        # USDC (Arbitrum) deposit
        usdc_to_move = float(main_token_amount)
        if main_address.lower() == strategy_address.lower():
            usdc_to_move = 0.0
        else:
            ok, usdc_raw = await self.balance_adapter.get_vault_wallet_balance(USDC_ARB)
            existing_usdc = (int(usdc_raw) / 1e6) if ok else 0.0
            usdc_to_move = max(0.0, float(main_token_amount) - existing_usdc)

        if usdc_to_move > 0.01:
            (
                move_ok,
                move_res,
            ) = await self.balance_adapter.move_from_main_wallet_to_strategy_wallet(
                token_id=USDC_ARB,
                amount=usdc_to_move,
                strategy_name="boros_hype_strategy",
            )
            if not move_ok:
                return False, f"Failed to move USDC to strategy wallet: {move_res}"

        # ETH (Arbitrum) gas deposit
        eth_to_move = float(gas_token_amount)
        if main_address.lower() == strategy_address.lower():
            eth_to_move = 0.0
        else:
            ok, eth_raw = await self.balance_adapter.get_vault_wallet_balance(ETH_ARB)
            existing_eth = (int(eth_raw) / 1e18) if ok else 0.0
            eth_to_move = max(0.0, float(gas_token_amount) - existing_eth)

        if eth_to_move > 0.00001:
            (
                move_ok,
                move_res,
            ) = await self.balance_adapter.move_from_main_wallet_to_strategy_wallet(
                token_id=ETH_ARB,
                amount=eth_to_move,
                strategy_name="boros_hype_strategy",
            )
            if not move_ok:
                return False, f"Failed to move ETH to strategy wallet: {move_res}"

        return True, (
            f"Deposit ready. Moved {usdc_to_move:.2f} USDC + {eth_to_move:.4f} ETH to strategy wallet. "
            "Run update() to deploy."
        )

    async def update(self) -> StatusTuple:
        # Run OPA loop, then enforce invariants: LST allocation, delta-neutrality, Boros coverage
        success, message, _ = await self.run_opa_loop()

        # If a failsafe liquidation was triggered inside the loop, treat update as failed.
        if self._failsafe_triggered:
            return (False, self._failsafe_message or message)

        # If OPA loop failed, return immediately
        if not success:
            return (success, message)

        # If a withdrawal is pending, do nothing else this tick.
        # We intentionally avoid the safety pass and any redeployment/hedging.
        if self._opa_pending_withdrawal:
            return (success, message)

        # FINAL SAFETY PASS - Enforce invariants after OPA loop
        # Even with good planning, we can end a tick offside because:
        # - A hedge order partially fills
        # - Step budget exhausted before hedge steps run
        # - Never re-observed after final hedge step
        # This pass uses fresh inventory and fixes drift immediately.

        try:
            inv = await self.observe()
            safety_messages: list[str] = []

            # 1) Spot should be allocated into LSTs (kHYPE + looped HYPE)
            swappable_hype = max(
                0.0, float(inv.hype_hyperevm_balance or 0.0) - MIN_HYPE_GAS
            )
            if swappable_hype > self._planner_config.min_hype_swap:
                logger.info(
                    f"[SAFETY] Unallocated spot HYPE detected: {swappable_hype:.4f} HYPE"
                )
                ok, msg = await self._swap_hype_to_lst(
                    {"hype_amount": swappable_hype}, inv
                )
                if ok:
                    safety_messages.append(f"Swapped {swappable_hype:.2f} HYPE to LST")
                    inv = await self.observe()  # Refresh after swap

            # 2) Delta neutrality must hold
            ok_delta, delta_msg = self._delta_neutral_ok(inv)
            if not ok_delta:
                logger.warning(f"[SAFETY] Delta imbalance detected: {delta_msg}")
                target_short = inv.total_hype_exposure
                ok, msg = await self._ensure_hl_short(
                    {
                        "target_size": target_short,
                        "current_size": inv.hl_short_size_hype,
                    },
                    inv,
                )
                if not ok:
                    trim_attempted = False
                    trim_result: str | None = None

                    # If we can't increase the short due to margin constraints, trim spot to
                    # raise HL withdrawable and retry instead of immediately full-failsafing.
                    if (
                        "insufficient free margin" in str(msg).lower()
                        or "trim" in str(msg).lower()
                    ):
                        trim_attempted = True
                        try:
                            hype_price = float(inv.hype_price_usd or 0.0)
                            tol = float(self._planner_config.delta_neutral_abs_tol_hype)
                            diff = abs(
                                float(target_short or 0.0)
                                - float(inv.hl_short_size_hype or 0.0)
                            )
                            min_increase_needed = max(0.0, diff - tol)
                            free_margin = float(inv.hl_withdrawable_usd or 0.0)
                            buffer_usd = float(
                                getattr(
                                    self._planner_config,
                                    "hl_withdrawable_buffer_usd",
                                    5.0,
                                )
                                or 0.0
                            )

                            required_margin = (
                                (min_increase_needed * hype_price)
                                / float(MAX_HL_LEVERAGE)
                                if hype_price > 0
                                else float(MIN_NOTIONAL_USD)
                            )
                            # Aim to restore the hedge and keep a small withdrawable buffer.
                            trim_usd_needed = max(
                                float(MIN_NOTIONAL_USD),
                                (required_margin + buffer_usd) - free_margin,
                            )
                            spot_value_usd = float(inv.spot_value_usd or 0.0)
                            if spot_value_usd > 0:
                                trim_pct = min(
                                    0.95, max(0.05, trim_usd_needed / spot_value_usd)
                                )
                            else:
                                trim_pct = 0.25

                            ok_trim, trim_msg = await self._partial_trim_spot(
                                {"trim_pct": float(trim_pct)}, inv
                            )
                            trim_result = trim_msg
                            if ok_trim:
                                safety_messages.append(
                                    f"Spot trimmed to restore margin: {trim_msg}"
                                )
                                inv = await self.observe()
                                ok, msg = True, "trimmed"
                        except Exception as exc:  # noqa: BLE001
                            trim_result = f"trim exception: {exc}"

                    if not ok:
                        reason = (
                            "Failed to restore delta neutrality: "
                            f"{delta_msg} | hedge_err={msg}"
                        )
                        if trim_attempted:
                            reason += f" | trim={trim_result}"
                        ok_fs, msg_fs = await self._failsafe_liquidate_all(reason)
                        return (ok_fs, msg_fs)

                # Recheck after hedge
                inv = await self.observe()
                ok_delta, delta_msg = self._delta_neutral_ok(inv)
                if not ok_delta:
                    ok_fs, msg_fs = await self._failsafe_liquidate_all(
                        f"Delta neutrality still broken after hedge: {delta_msg}"
                    )
                    return (ok_fs, msg_fs)
                safety_messages.append("Delta-neutral hedge fixed")

            # 3) Boros coverage should be ~85% (best effort, don't hard-fail)
            ok_boros, boros_msg = self._boros_coverage_ok(inv, self._opa_boros_quotes)
            if not ok_boros:
                logger.info(f"[SAFETY] Boros coverage drift: {boros_msg}")
                depositable_collateral_hype = float(
                    inv.boros_collateral_hype or 0.0
                ) + float(inv.hype_oft_arb_balance or 0.0)
                if depositable_collateral_hype < BOROS_MIN_DEPOSIT_HYPE:
                    # Collateral isn't ready yet (or still in-flight via OFT); don't fail-safe.
                    logger.info(
                        "[SAFETY] Skipping Boros coverage fix: collateral not funded yet "
                        f"({depositable_collateral_hype:.6f} HYPE)"
                    )
                else:
                    # Attempt fix (best effort). If it fails, we log and keep running.
                    token_id = int(
                        self._planner_runtime.current_boros_token_id
                        or BOROS_HYPE_TOKEN_ID
                    )
                    hype_price = float(inv.hype_price_usd or 0.0)
                    if hype_price <= 0:
                        logger.info(
                            "[SAFETY] Skipping Boros coverage fix: HYPE price unavailable"
                        )
                        target_yu = None
                    else:
                        short_hype = float(inv.hl_short_size_hype or 0.0)
                        spot_usd = short_hype * hype_price
                        if spot_usd < 10:
                            target_yu = None
                        elif token_id == BOROS_HYPE_TOKEN_ID:
                            target_yu = (
                                short_hype * self._planner_config.boros_coverage_target
                            )
                        else:
                            target_yu = (
                                spot_usd * self._planner_config.boros_coverage_target
                            )

                    if self._planner_runtime.current_boros_market_id and target_yu:
                        ok, msg = await self._ensure_boros_position(
                            {
                                "market_id": self._planner_runtime.current_boros_market_id,
                                "target_size_yu": target_yu,
                            },
                            inv,
                        )
                        if ok:
                            safety_messages.append(
                                f"Boros coverage adjusted to {target_yu:.4f} YU"
                            )
                        else:
                            logger.warning(f"[SAFETY] Boros coverage fix failed: {msg}")

            # Append safety messages to result
            if safety_messages:
                message = f"{message} | SAFETY: {'; '.join(safety_messages)}"

        except Exception as e:
            logger.error(f"Safety pass failed: {e}")
            # Don't fail the whole update if safety pass has an exception
            # The main OPA loop already succeeded

        return (success, message)

    async def _get_yield_info(self, inv: Inventory) -> YieldInfo:
        yield_info = YieldInfo()

        # Fetch external APYs in parallel
        khype_apy, lhype_apy = await asyncio.gather(
            fetch_khype_apy(), fetch_lhype_apy()
        )
        yield_info.khype_apy = khype_apy
        yield_info.lhype_apy = lhype_apy

        # Get Boros APR from active position
        boros_notional_usd = 0.0
        if self.boros_adapter:
            try:
                success, positions = await self.boros_adapter.get_active_positions()
                if success and positions:
                    pos = positions[0]
                    # fixedApr is the locked-in rate
                    fixed_apr = pos.get("fixedApr")
                    if fixed_apr is not None:
                        yield_info.boros_apr = float(fixed_apr)
                    # Position size is in YU - convert to USD based on collateral type
                    position_yu = abs(inv.boros_position_size or 0)
                    token_id = int(
                        self._planner_runtime.current_boros_token_id
                        or BOROS_HYPE_TOKEN_ID
                    )
                    if token_id == BOROS_HYPE_TOKEN_ID:
                        # HYPE collateral: 1 YU = 1 HYPE exposure
                        boros_notional_usd = position_yu * inv.hype_price_usd
                    else:
                        # USDT collateral: 1 YU ≈ $1 exposure
                        boros_notional_usd = position_yu
            except Exception as e:
                logger.warning(f"Failed to get Boros APR: {e}")

        # Calculate expected annual yields in USD

        # kHYPE yield: APY applied to kHYPE value
        if yield_info.khype_apy and inv.khype_value_usd > 0:
            yield_info.khype_expected_yield_usd = (
                inv.khype_value_usd * yield_info.khype_apy
            )

        # lHYPE yield: APY applied to lHYPE value
        if yield_info.lhype_apy and inv.looped_hype_value_usd > 0:
            yield_info.lhype_expected_yield_usd = (
                inv.looped_hype_value_usd * yield_info.lhype_apy
            )

        # Boros yield: Locks in funding rate on perp position
        if yield_info.boros_apr is not None and boros_notional_usd > 0:
            yield_info.boros_expected_yield_usd = (
                yield_info.boros_apr * boros_notional_usd
            )

        # Total expected yield
        yield_info.total_expected_yield_usd = (
            yield_info.khype_expected_yield_usd
            + yield_info.lhype_expected_yield_usd
            + yield_info.boros_expected_yield_usd
        )

        # Blended APY based on total value
        if inv.total_value > 0:
            yield_info.blended_apy = (
                yield_info.total_expected_yield_usd / inv.total_value
            )

        return yield_info

    async def quote(self, deposit_amount: float | None = None) -> dict:
        inv = await self.observe()
        yield_info = await self._get_yield_info(inv)

        components = {}
        if yield_info.khype_apy is not None:
            components["khype_apy"] = yield_info.khype_apy
        if yield_info.lhype_apy is not None:
            components["lhype_apy"] = yield_info.lhype_apy
        if yield_info.boros_apr is not None:
            components["boros_apr"] = yield_info.boros_apr

        apy = yield_info.blended_apy or 0.0
        summary_parts = []
        if yield_info.khype_apy:
            summary_parts.append(f"kHYPE {yield_info.khype_apy * 100:.1f}%")
        if yield_info.lhype_apy:
            summary_parts.append(f"lHYPE {yield_info.lhype_apy * 100:.1f}%")
        if yield_info.boros_apr:
            summary_parts.append(f"Boros {yield_info.boros_apr * 100:.1f}%")

        summary = f"Blended APY: {apy * 100:.2f}%"
        if summary_parts:
            summary += f" ({', '.join(summary_parts)})"

        return {
            "expected_apy": float(apy),
            "apy_type": "blended",
            "confidence": "medium",
            "methodology": "Weighted average of kHYPE staking, lHYPE loop, and Boros fixed-rate yields based on current allocations",
            "components": components,
            "deposit_amount": deposit_amount,
            "as_of": datetime.now(UTC).isoformat(),
            "summary": summary,
        }

    async def exit(self, **kwargs) -> StatusTuple:
        # Transfer remaining balances to main wallet (does NOT close positions - call withdraw() first)
        if not self.balance_adapter:
            return False, "Balance adapter not configured"

        strategy_wallet = self._config.get("strategy_wallet", {})
        main_wallet = self._config.get("main_wallet", {})
        strategy_addr = strategy_wallet.get("address") if strategy_wallet else None
        main_addr = main_wallet.get("address") if main_wallet else None

        if not strategy_addr or not main_addr:
            return False, "Strategy or main wallet address not configured"

        if strategy_addr.lower() == main_addr.lower():
            return True, "Strategy wallet is main wallet, no transfer needed"

        transferred = []

        # Transfer USDC on Arbitrum
        ok, usdc_raw = await self.balance_adapter.get_vault_wallet_balance(USDC_ARB)
        if ok and isinstance(usdc_raw, int) and usdc_raw > 0:
            usdc_amount = usdc_raw / 1e6
            if usdc_amount > 0.01:
                (
                    ok,
                    msg,
                ) = await self.balance_adapter.move_from_strategy_wallet_to_main_wallet(
                    USDC_ARB, usdc_amount, "boros_hype_strategy"
                )
                if ok:
                    transferred.append(f"{usdc_amount:.2f} USDC")
                else:
                    return False, f"USDC transfer failed: {msg}"

        # Transfer USDT on Arbitrum
        ok, usdt_raw = await self.balance_adapter.get_vault_wallet_balance(USDT_ARB)
        if ok and isinstance(usdt_raw, int) and usdt_raw > 0:
            usdt_amount = usdt_raw / 1e6
            if usdt_amount > 0.01:
                (
                    ok,
                    msg,
                ) = await self.balance_adapter.move_from_strategy_wallet_to_main_wallet(
                    USDT_ARB, usdt_amount, "boros_hype_strategy"
                )
                if ok:
                    transferred.append(f"{usdt_amount:.2f} USDT")
                else:
                    return False, f"USDT transfer failed: {msg}"

        if transferred:
            return True, f"Transferred to main wallet: {', '.join(transferred)}"
        return True, "No balances to transfer"

    async def _status(self) -> StatusDict:
        inv = await self.observe()
        alloc = self._get_allocation_status(inv)
        yield_info = await self._get_yield_info(inv)

        # Build human-readable summary with full breakdown
        spot_parts = []
        if inv.khype_balance > 0.001:
            spot_parts.append(
                f"{inv.khype_balance:.4f} kHYPE (${inv.khype_value_usd:.2f})"
            )
        if inv.looped_hype_balance > 0.001:
            spot_parts.append(
                f"{inv.looped_hype_balance:.4f} lHYPE (${inv.looped_hype_value_usd:.2f})"
            )
        if inv.whype_balance > 0.001:
            spot_parts.append(
                f"{inv.whype_balance:.4f} WHYPE (${inv.whype_value_usd:.2f})"
            )
        if inv.hype_hyperevm_balance > 0.001:
            spot_parts.append(
                f"{inv.hype_hyperevm_balance:.4f} HYPE (${inv.hype_hyperevm_value_usd:.2f})"
            )
        if inv.hl_spot_hype > 0.001:
            spot_parts.append(
                f"{inv.hl_spot_hype:.4f} HYPE on HL spot (${inv.hl_spot_hype_value_usd:.2f})"
            )

        if spot_parts:
            spot_summary = (
                f"Spot: {' + '.join(spot_parts)} = "
                f"{inv.total_hype_exposure:.4f} HYPE equivalent (${inv.spot_value_usd:.2f})"
            )
        else:
            spot_summary = f"Spot: No HYPE exposure (${inv.spot_value_usd:.2f})"
        hl_summary = (
            f"Hyperliquid: ${inv.hl_perp_margin:.2f} margin, "
            f"{inv.hl_short_size_hype:.4f} HYPE short (${inv.hl_short_value_usd:.2f} notional)"
        )
        boros_summary = (
            f"Boros: ${inv.boros_collateral_usd:.2f} collateral, "
            f"{inv.boros_position_size:.2f} YU position"
        )

        # Yield summary
        yield_parts = []
        if yield_info.khype_apy is not None:
            yield_parts.append(
                f"kHYPE: {yield_info.khype_apy * 100:.2f}% APY (${yield_info.khype_expected_yield_usd:.2f}/yr)"
            )
        if yield_info.lhype_apy is not None:
            yield_parts.append(
                f"lHYPE: {yield_info.lhype_apy * 100:.2f}% APY (${yield_info.lhype_expected_yield_usd:.2f}/yr)"
            )
        if yield_info.boros_apr is not None:
            yield_parts.append(
                f"Boros: {yield_info.boros_apr * 100:.2f}% APR locked (${yield_info.boros_expected_yield_usd:.2f}/yr)"
            )
        yield_summary = (
            "Yields: " + ", ".join(yield_parts) if yield_parts else "Yields: N/A"
        )

        if yield_info.blended_apy is not None:
            yield_summary += f"\nBlended APY: {yield_info.blended_apy * 100:.2f}% (${yield_info.total_expected_yield_usd:.2f}/yr expected)"

        strategy_summary = (
            f"{spot_summary}\n{hl_summary}\n{boros_summary}\n{yield_summary}"
        )

        # Fetch net deposit from ledger
        net_deposit = 0.0
        try:
            success, deposit_data = await self.ledger_adapter.get_strategy_net_deposit(
                wallet_address=self._get_strategy_wallet_address()
            )
            if success and deposit_data is not None:
                net_deposit = float(deposit_data)
        except Exception as e:
            logger.warning(f"Could not fetch net deposit from ledger: {e}")

        return {
            "portfolio_value": inv.total_value,
            "net_deposit": net_deposit,
            "strategy_summary": strategy_summary,
            "strategy_status": {
                "mode": "NORMAL",
                "allocations": {
                    "spot": {
                        "value": alloc.spot_value,
                        "pct": alloc.spot_pct_actual,
                        "target_pct": self.hedge_cfg.spot_pct,
                    },
                    "hyperliquid": {
                        "value": alloc.hl_value,
                        "pct": alloc.hl_pct_actual,
                        "target_pct": self.hedge_cfg.hyperliquid_pct,
                    },
                    "boros": {
                        "value": alloc.boros_value,
                        "pct": alloc.boros_pct_actual,
                        "target_pct": self.hedge_cfg.boros_pct,
                    },
                },
            },
            "positions": {
                "spot": {
                    "khype_balance": inv.khype_balance,
                    "khype_value_usd": inv.khype_value_usd,
                    "lhype_balance": inv.looped_hype_balance,
                    "lhype_value_usd": inv.looped_hype_value_usd,
                    "whype_balance": inv.whype_balance,
                    "whype_value_usd": inv.whype_value_usd,
                    "hype_hyperevm_balance": inv.hype_hyperevm_balance,
                    "hype_hyperevm_value_usd": inv.hype_hyperevm_value_usd,
                    "hl_spot_hype": inv.hl_spot_hype,
                    "hl_spot_hype_value_usd": inv.hl_spot_hype_value_usd,
                    "total_hype_equivalent": inv.total_hype_exposure,
                    "total_spot_value_usd": inv.spot_value_usd,
                },
                "hyperliquid": {
                    "perp_margin": inv.hl_perp_margin,
                    "short_size_hype": inv.hl_short_size_hype,
                    "short_value_usd": inv.hl_short_value_usd,
                },
                "boros": {
                    "collateral_usd": inv.boros_collateral_usd,
                    "position_size_yu": inv.boros_position_size,
                },
            },
            "yield_info": {
                "khype_apy": yield_info.khype_apy,
                "lhype_apy": yield_info.lhype_apy,
                "boros_apr": yield_info.boros_apr,
                "khype_expected_yield_usd": yield_info.khype_expected_yield_usd,
                "lhype_expected_yield_usd": yield_info.lhype_expected_yield_usd,
                "boros_expected_yield_usd": yield_info.boros_expected_yield_usd,
                "total_expected_yield_usd": yield_info.total_expected_yield_usd,
                "blended_apy": yield_info.blended_apy,
            },
            "gas_available": inv.hype_hyperevm_balance,
            "gassed_up": inv.hype_hyperevm_balance >= MIN_HYPE_GAS,
        }
