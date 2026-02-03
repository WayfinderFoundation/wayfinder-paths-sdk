import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Optional

import httpx
from eth_utils import to_checksum_address
from loguru import logger

from wayfinder_paths.adapters.balance_adapter.adapter import BalanceAdapter
from wayfinder_paths.adapters.brap_adapter.adapter import BRAPAdapter
from wayfinder_paths.adapters.ledger_adapter.adapter import LedgerAdapter
from wayfinder_paths.adapters.moonwell_adapter.adapter import MoonwellAdapter
from wayfinder_paths.adapters.token_adapter.adapter import TokenAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.contracts import BASE_USDC, BASE_WSTETH
from wayfinder_paths.core.constants.erc20_abi import ERC20_ABI
from wayfinder_paths.core.constants.tokens import (
    TOKEN_ID_ETH_BASE,
    TOKEN_ID_STETH,
    TOKEN_ID_USDC_BASE,
    TOKEN_ID_WELL_BASE,
    TOKEN_ID_WETH_BASE,
    TOKEN_ID_WSTETH_BASE,
)
from wayfinder_paths.core.strategies.descriptors import (
    Complexity,
    Directionality,
    Frequency,
    StratDescriptor,
    TokenExposure,
    Volatility,
)
from wayfinder_paths.core.strategies.Strategy import StatusDict, StatusTuple, Strategy
from wayfinder_paths.core.utils.web3 import web3_from_chain_id
from wayfinder_paths.policies.enso import ENSO_ROUTER, enso_swap
from wayfinder_paths.policies.erc20 import erc20_spender_for_any_token
from wayfinder_paths.policies.moonwell import (
    M_USDC,
    M_WETH,
    M_WSTETH,
    WETH,
    moonwell_comptroller_enter_markets_or_claim_rewards,
    musdc_mint_or_approve_or_redeem,
    mweth_approve_or_borrow_or_repay,
    mwsteth_approve_or_mint_or_redeem,
    weth_deposit,
)

USDC = BASE_USDC
WSTETH = BASE_WSTETH

USDC_TOKEN_ID = TOKEN_ID_USDC_BASE
WETH_TOKEN_ID = TOKEN_ID_WETH_BASE
WSTETH_TOKEN_ID = TOKEN_ID_WSTETH_BASE
ETH_TOKEN_ID = TOKEN_ID_ETH_BASE
WELL_TOKEN_ID = TOKEN_ID_WELL_BASE
STETH_TOKEN_ID = TOKEN_ID_STETH

BASE_CHAIN_ID = CHAIN_ID_BASE
COLLATERAL_SAFETY_FACTOR = 0.98


class SwapOutcomeUnknownError(RuntimeError):
    pass


class MoonwellWstethLoopStrategy(Strategy):
    name = "Moonwell wstETH Loop Strategy"
    description = "Leveraged wstETH yield strategy using Moonwell lending protocol."
    summary = "Loop wstETH on Moonwell for amplified staking yields."

    # Strategy parameters
    # Minimum Base ETH (in ETH) required for gas fees (Base L2)
    MIN_GAS = 0.002
    MAINTENANCE_GAS = MIN_GAS / 10
    # When wrapping ETH to WETH for swaps/repayment, avoid draining gas below this floor.
    # We can dip below MIN_GAS temporarily, but should not wipe the wallet.
    WRAP_GAS_RESERVE = 0.0014
    MIN_USDC_DEPOSIT = 10.0
    MIN_REWARD_CLAIM_USD = 0.30
    MAX_DEPEG = 0.01
    MAX_HEALTH_FACTOR = 1.5
    MIN_HEALTH_FACTOR = 1.2
    # Operational target HF (keep some buffer above MIN_HEALTH_FACTOR).
    TARGET_HEALTH_FACTOR = 1.25
    # Lever up if HF is more than this amount above TARGET_HEALTH_FACTOR
    HF_LEVER_UP_BUFFER = 0.05
    # Deleverage if HF drops below (TARGET - buffer).
    HF_DELEVERAGE_BUFFER = 0.05
    # Deleverage if leverage multiplier exceeds target by this much.
    LEVERAGE_DELEVERAGE_BUFFER = 0.05
    # During leverage-delever, allow HF to dip temporarily during the withdraw tx
    # (swap+repay follows immediately). Keep well above liquidation.
    LEVERAGE_DELEVER_HF_FLOOR = 1.05
    # How close we need to be to "delta neutral" before we stop trying.
    DELTA_TOL_USD = 5.0
    # Prevent the post-run guard from spinning forever.
    POST_RUN_MAX_PASSES = 2
    # Full-exit (withdraw) dust behavior: do fewer, larger actions so we can repay_full in one go.
    FULL_EXIT_BUFFER_MULT = 1.05
    FULL_EXIT_MIN_BATCH_USD = 10.0
    _MAX_LOOP_LIMIT = 30

    # Parameters
    leverage_limit = 10
    min_withdraw_usd = 2
    sweep_min_usd = 0.20
    max_swap_retries = 3
    swap_slippage_tolerance = 0.005
    MAX_SLIPPAGE_TOLERANCE = 0.03
    PRICE_STALENESS_THRESHOLD = 300

    # 50 basis points (0.0050) - minimum leverage gain per loop iteration to continue
    # If marginal gain drops below this, stop looping as gas costs outweigh benefit
    _MIN_LEVERAGE_GAIN_BPS = 50e-4

    INFO = StratDescriptor(
        description="Leveraged wstETH carry: loops USDC → borrow WETH → swap wstETH → lend. "
        "Depeg-aware sizing with safety factor. ETH-neutral: WETH debt vs wstETH collateral.",
        summary="Leveraged wstETH carry on Base with depeg-aware sizing.",
        risk_description="Protocol risk is always present when engaging with DeFi strategies, this includes underlying DeFi protocols and Wayfinder itself. Additional risks include wstETH/ETH depeg events which could trigger liquidations, health factor deterioration requiring emergency deleveraging, smart contract risk on Moonwell, and swap slippage during position adjustments. Strategy monitors peg ratio and adjusts leverage ceiling accordingly.",
        gas_token_symbol="ETH",
        gas_token_id=ETH_TOKEN_ID,
        deposit_token_id=USDC_TOKEN_ID,
        minimum_net_deposit=20,
        gas_maximum=0.05,
        gas_threshold=0.01,
        volatility=Volatility.LOW,
        volatility_description="APYs can vary significantly but are almost always positive",
        directionality=Directionality.DELTA_NEUTRAL,
        directionality_description="Balances wstETH collateral and WETH debt so ETH delta stays close to flat.",
        complexity=Complexity.MEDIUM,
        complexity_description="Manages recursive lend/borrow loops, peg monitoring, and health-factor controls.",
        token_exposure=TokenExposure.MAJORS,
        token_exposure_description="Risk is concentrated in ETH (wstETH vs WETH) and USDC on Base.",
        frequency=Frequency.LOW,
        frequency_description="Runs every 2 hours but will trade rarely to minimize transaction fees.",
        return_drivers=["leveraged lend APY"],
        config={
            "deposit": {
                "description": "Lend USDC as seed collateral, then execute leverage loop.",
                "parameters": {
                    "main_token_amount": {
                        "type": "float",
                        "unit": "USDC tokens",
                        "description": "Amount of USDC to deposit as initial collateral.",
                        "minimum": 20.0,
                        "examples": ["100.0", "500.0", "1000.0"],
                    },
                    "gas_token_amount": {
                        "type": "float",
                        "unit": "ETH tokens",
                        "description": "Amount of ETH to transfer for gas.",
                        "minimum": 0.0,
                        "recommended": 0.01,
                    },
                },
                "result": "Delta-neutral leveraged wstETH position.",
            },
            "withdraw": {
                "description": "Unwind positions, repay debt, and return funds.",
                "parameters": {},
                "result": "All debt repaid, collateral returned in USDC.",
            },
            "update": {
                "description": "Rebalance positions and manage leverage.",
                "parameters": {},
                "result": "Position maintained at target leverage.",
            },
        },
    )

    def __init__(
        self,
        config: dict | None = None,
        *,
        main_wallet: dict | None = None,
        strategy_wallet: dict | None = None,
        main_wallet_signing_callback: Callable[[dict], Awaitable[str]] | None = None,
        strategy_wallet_signing_callback: Callable[[dict], Awaitable[str]]
        | None = None,
        strategy_sign_typed_data: Callable[[dict], Awaitable[str]] | None = None,
    ):
        super().__init__(
            main_wallet_signing_callback=main_wallet_signing_callback,
            strategy_wallet_signing_callback=strategy_wallet_signing_callback,
            strategy_sign_typed_data=strategy_sign_typed_data,
        )
        merged_config: dict[str, Any] = dict(config or {})
        if main_wallet is not None:
            merged_config["main_wallet"] = main_wallet
        if strategy_wallet is not None:
            merged_config["strategy_wallet"] = strategy_wallet

        self.config = merged_config

        self.balance_adapter: BalanceAdapter | None = None
        self.moonwell_adapter: MoonwellAdapter | None = None
        self.brap_adapter: BRAPAdapter | None = None
        self.token_adapter: TokenAdapter | None = None
        self.ledger_adapter: LedgerAdapter | None = None

        self._token_info_cache: dict[str, dict] = {}
        self._token_price_cache: dict[str, float] = {}
        self._token_price_timestamps: dict[str, float] = {}

        try:
            main_wallet_cfg = self.config.get("main_wallet")
            strategy_wallet_cfg = self.config.get("strategy_wallet")

            if not strategy_wallet_cfg or not strategy_wallet_cfg.get("address"):
                raise ValueError(
                    "strategy_wallet not configured. Provide strategy_wallet address in config."
                )

            adapter_config = {
                "main_wallet": main_wallet_cfg or None,
                "strategy_wallet": strategy_wallet_cfg or None,
                "strategy": self.config,
            }

            balance = BalanceAdapter(
                adapter_config,
                main_wallet_signing_callback=self.main_wallet_signing_callback,
                strategy_wallet_signing_callback=self.strategy_wallet_signing_callback,
            )
            token_adapter = TokenAdapter()
            ledger_adapter = LedgerAdapter()
            brap_adapter = BRAPAdapter(
                adapter_config,
                strategy_wallet_signing_callback=self.strategy_wallet_signing_callback,
            )
            moonwell_adapter = MoonwellAdapter(
                adapter_config,
                strategy_wallet_signing_callback=self.strategy_wallet_signing_callback,
            )

            self.balance_adapter = balance
            self.token_adapter = token_adapter
            self.ledger_adapter = ledger_adapter
            self.brap_adapter = brap_adapter
            self.moonwell_adapter = moonwell_adapter

        except Exception as e:
            logger.error(f"Failed to initialize strategy adapters: {e}")
            raise

    def _max_safe_F(self, cf_w: float) -> float:
        a = 1 - self.MAX_DEPEG
        if not (0 < a):
            return 0.0
        if not (0 <= cf_w < 1):
            return 0.0

        f_bound = 1.0 / (1.0 + cf_w * (1.0 - a))
        # Extra feasibility guard (usually >1, but keep for safety).
        f_feasible = 1.0 / (cf_w * a) if cf_w > 0 else 1.0
        return max(0.0, min(1.0, min(f_bound, f_feasible, 1.0)))

    def _get_strategy_wallet_address(self) -> str:
        wallet = self.config.get("strategy_wallet", {})
        return wallet.get("address", "")

    def _get_main_wallet_address(self) -> str:
        wallet = self.config.get("main_wallet", {})
        return wallet.get("address", "")

    def _gas_keep_wei(self) -> int:
        # Extra buffer for a couple txs (wrap + swap/repay).
        tx_buffer = int(0.0003 * 10**18)
        return max(
            int(self.MIN_GAS * 10**18),
            int(self.WRAP_GAS_RESERVE * 10**18) + tx_buffer,
        )

    @dataclass
    class AccountingSnapshot:
        # raw balances
        wallet_eth: int
        wallet_weth: int
        wallet_wsteth: int
        wallet_usdc: int

        usdc_supplied: int
        wsteth_supplied: int
        weth_debt: int

        # prices/decimals
        eth_price: float
        weth_price: float
        wsteth_price: float
        usdc_price: float

        eth_dec: int
        weth_dec: int
        wsteth_dec: int
        usdc_dec: int

        # derived USD values
        wallet_usd: float
        supplies_usd: float
        debt_usd: float
        net_equity_usd: float

        # borrow capacity + risk
        capacity_usd: float
        ltv: float
        hf: float

        # gas
        gas_keep_wei: int
        eth_usable_wei: int

        # convenient totals dict for HF simulations (same key shape as existing helpers)
        totals_usd: dict[str, float]

    async def _accounting_snapshot(
        self, collateral_factors: tuple[float, float] | None = None
    ) -> tuple["MoonwellWstethLoopStrategy.AccountingSnapshot", tuple[float, float]]:
        if collateral_factors is None:
            collateral_factors = await self._get_collateral_factors()
        cf_u, cf_w = collateral_factors

        # Prices + decimals
        (
            (eth_price, eth_dec),
            (weth_price, weth_dec),
            (wsteth_price, wsteth_dec),
            (usdc_price, usdc_dec),
        ) = await asyncio.gather(
            self._get_token_data(ETH_TOKEN_ID),
            self._get_token_data(WETH_TOKEN_ID),
            self._get_token_data(WSTETH_TOKEN_ID),
            self._get_token_data(USDC_TOKEN_ID),
        )

        addr = self._get_strategy_wallet_address()

        # Wallet balances
        wallet_eth, wallet_weth, wallet_wsteth, wallet_usdc = await asyncio.gather(
            self._get_balance_raw(token_id=ETH_TOKEN_ID, wallet_address=addr),
            self._get_balance_raw(token_id=WETH_TOKEN_ID, wallet_address=addr),
            self._get_balance_raw(token_id=WSTETH_TOKEN_ID, wallet_address=addr),
            self._get_balance_raw(token_id=USDC_TOKEN_ID, wallet_address=addr),
        )

        # Protocol positions (sequential to avoid RPC burst)
        usdc_pos_ok, usdc_pos = await self.moonwell_adapter.get_pos(mtoken=M_USDC)
        wsteth_pos_ok, wsteth_pos = await self.moonwell_adapter.get_pos(mtoken=M_WSTETH)
        weth_pos_ok, weth_pos = await self.moonwell_adapter.get_pos(mtoken=M_WETH)

        usdc_supplied = (
            int((usdc_pos or {}).get("underlying_balance", 0) or 0)
            if usdc_pos_ok
            else 0
        )
        wsteth_supplied = (
            int((wsteth_pos or {}).get("underlying_balance", 0) or 0)
            if wsteth_pos_ok
            else 0
        )
        weth_debt = (
            int((weth_pos or {}).get("borrow_balance", 0) or 0) if weth_pos_ok else 0
        )

        gas_keep_wei = int(self._gas_keep_wei())
        eth_usable_wei = max(0, int(wallet_eth) - int(gas_keep_wei))

        def _usd(raw: int, price: float, dec: int) -> float:
            if raw <= 0 or not price or price <= 0:
                return 0.0
            return (raw / (10**dec)) * float(price)

        wallet_usd = (
            _usd(wallet_eth, eth_price, eth_dec)
            + _usd(wallet_weth, weth_price, weth_dec)
            + _usd(wallet_wsteth, wsteth_price, wsteth_dec)
            + _usd(wallet_usdc, usdc_price, usdc_dec)
        )
        supplies_usd = _usd(usdc_supplied, usdc_price, usdc_dec) + _usd(
            wsteth_supplied, wsteth_price, wsteth_dec
        )
        debt_usd = _usd(weth_debt, weth_price, weth_dec)

        net_equity_usd = wallet_usd + supplies_usd - debt_usd

        capacity_usd = cf_u * _usd(usdc_supplied, usdc_price, usdc_dec) + cf_w * _usd(
            wsteth_supplied, wsteth_price, wsteth_dec
        )
        ltv = (
            (debt_usd / capacity_usd)
            if (capacity_usd > 0 and debt_usd > 0)
            else (0.0 if debt_usd <= 0 else float("nan"))
        )
        hf = (capacity_usd / debt_usd) if debt_usd > 0 else float("inf")

        totals_usd = {
            f"Base_{M_USDC}": _usd(usdc_supplied, usdc_price, usdc_dec),
            f"Base_{M_WSTETH}": _usd(wsteth_supplied, wsteth_price, wsteth_dec),
            f"Base_{WETH}": -debt_usd,
        }

        snap = self.AccountingSnapshot(
            wallet_eth=int(wallet_eth),
            wallet_weth=int(wallet_weth),
            wallet_wsteth=int(wallet_wsteth),
            wallet_usdc=int(wallet_usdc),
            usdc_supplied=usdc_supplied,
            wsteth_supplied=wsteth_supplied,
            weth_debt=weth_debt,
            eth_price=float(eth_price or 0.0),
            weth_price=float(weth_price or 0.0),
            wsteth_price=float(wsteth_price or 0.0),
            usdc_price=float(usdc_price or 0.0),
            eth_dec=int(eth_dec or 18),
            weth_dec=int(weth_dec or 18),
            wsteth_dec=int(wsteth_dec or 18),
            usdc_dec=int(usdc_dec or 6),
            wallet_usd=float(wallet_usd),
            supplies_usd=float(supplies_usd),
            debt_usd=float(debt_usd),
            net_equity_usd=float(net_equity_usd),
            capacity_usd=float(capacity_usd),
            ltv=float(ltv),
            hf=float(hf),
            gas_keep_wei=gas_keep_wei,
            eth_usable_wei=eth_usable_wei,
            totals_usd=totals_usd,
        )
        return snap, collateral_factors

    async def _ensure_markets_for_state(
        self, snap: "MoonwellWstethLoopStrategy.AccountingSnapshot"
    ) -> tuple[bool, str]:
        errors: list[str] = []

        if snap.usdc_supplied > 0:
            ok, msg = await self.moonwell_adapter.set_collateral(mtoken=M_USDC)
            if not ok:
                errors.append(f"USDC: {msg}")

        if snap.wsteth_supplied > 0:
            ok, msg = await self.moonwell_adapter.set_collateral(mtoken=M_WSTETH)
            if not ok:
                errors.append(f"wstETH: {msg}")

        if snap.weth_debt > 0:
            ok, msg = await self.moonwell_adapter.set_collateral(mtoken=M_WETH)
            if not ok:
                errors.append(f"WETH: {msg}")

        if errors:
            return (False, "; ".join(errors))
        return (True, "markets ensured")

    def _debt_gap_report(
        self, snap: "MoonwellWstethLoopStrategy.AccountingSnapshot"
    ) -> dict[str, float]:
        eth_usable_usd = (
            (snap.eth_usable_wei / (10**snap.eth_dec)) * snap.eth_price
            if snap.eth_price
            else 0.0
        )
        repayable_wallet_usd = (
            (snap.wallet_weth / (10**snap.weth_dec)) * snap.weth_price
            + (snap.wallet_wsteth / (10**snap.wsteth_dec)) * snap.wsteth_price
            + (snap.wallet_usdc / (10**snap.usdc_dec)) * snap.usdc_price
            + eth_usable_usd
        )

        missing_to_repay_usd = max(0.0, snap.debt_usd - repayable_wallet_usd)

        gas_keep_usd = (
            (snap.gas_keep_wei / (10**snap.eth_dec)) * snap.eth_price
            if snap.eth_price
            else 0.0
        )
        expected_final_usdc_usd = max(0.0, snap.net_equity_usd - gas_keep_usd)

        return {
            "debt_usd": float(snap.debt_usd),
            "repayable_wallet_usd": float(repayable_wallet_usd),
            "missing_to_repay_usd": float(missing_to_repay_usd),
            "net_equity_usd": float(snap.net_equity_usd),
            "expected_final_usdc_usd_if_fully_unwound": float(expected_final_usdc_usd),
        }

    def _delta_mismatch_usd(
        self, snap: "MoonwellWstethLoopStrategy.AccountingSnapshot"
    ) -> float:
        wsteth_coll_usd = float(snap.totals_usd.get(f"Base_{M_WSTETH}", 0.0))
        return float(snap.debt_usd) - wsteth_coll_usd

    def _max_safe_withdraw_usd(
        self,
        *,
        totals_usd: dict[str, float],
        withdraw_key: str,
        collateral_factors: tuple[float, float],
        hf_floor: float,
        precision_usd: float = 0.50,
    ) -> float:
        current_val = float(totals_usd.get(withdraw_key, 0.0))
        if current_val <= 0:
            return 0.0

        debt_usd = abs(float(totals_usd.get(f"Base_{WETH}", 0.0)))
        if debt_usd <= 0:
            return current_val

        cf_u, cf_w = collateral_factors
        usdc_key = f"Base_{M_USDC}"
        wsteth_key = f"Base_{M_WSTETH}"
        usdc_coll = float(totals_usd.get(usdc_key, 0.0))
        wsteth_coll = float(totals_usd.get(wsteth_key, 0.0))

        def hf_after(withdraw_usd: float) -> float:
            u = usdc_coll - withdraw_usd if withdraw_key == usdc_key else usdc_coll
            w = (
                wsteth_coll - withdraw_usd
                if withdraw_key == wsteth_key
                else wsteth_coll
            )
            u = max(0.0, u)
            w = max(0.0, w)
            cap = cf_u * u + cf_w * w
            return cap / debt_usd if debt_usd > 0 else float("inf")

        lo, hi = 0.0, current_val
        for _ in range(30):
            mid = 0.5 * (lo + hi)
            if hf_after(mid) >= hf_floor:
                lo = mid
            else:
                hi = mid
            if (hi - lo) <= precision_usd:
                break

        return max(0.0, lo)

    async def _post_run_guard(
        self,
        *,
        mode: str = "operate",
        prior_error: Exception | None = None,
    ) -> tuple[bool, str]:
        logger.info("-" * 40)
        logger.info(f"POST-RUN GUARD: mode={mode}")
        if prior_error:
            logger.warning(f"  Prior error: {prior_error}")
        logger.info("-" * 40)

        try:
            gas = await self._get_gas_balance()
            if gas < int(self.MAINTENANCE_GAS * 10**18):
                return (
                    False,
                    f"post-run guard: insufficient gas ({gas / 1e18:.6f} ETH)",
                )

            collateral_factors = await self._get_collateral_factors()

            snap, _ = await self._accounting_snapshot(
                collateral_factors=collateral_factors
            )
            ok, msg = await self._ensure_markets_for_state(snap)
            if not ok:
                return (False, f"post-run guard: failed ensuring markets: {msg}")

            # 0) Post collateral already in the wallet: lend loose wstETH and ensure collateral.
            if snap.wallet_wsteth > 0:
                ok, msg = await self.moonwell_adapter.lend(
                    mtoken=M_WSTETH,
                    underlying_token=WSTETH,
                    amount=int(snap.wallet_wsteth),
                )
                if not ok:
                    return (
                        False,
                        f"post-run guard: failed lending wallet wstETH: {msg}",
                    )
                ok, msg = await self.moonwell_adapter.set_collateral(mtoken=M_WSTETH)
                if not ok:
                    return (
                        False,
                        f"post-run guard: failed ensuring wstETH collateral: {msg}",
                    )
                snap, _ = await self._accounting_snapshot(
                    collateral_factors=collateral_factors
                )

            # 1) HF safety check: deleverage until HF >= MIN_HEALTH_FACTOR.
            if snap.hf < float(self.MIN_HEALTH_FACTOR):
                if snap.capacity_usd <= 0:
                    return (
                        False,
                        "post-run guard: HF low but capacity_usd<=0; cannot compute deleverage target",
                    )
                try:
                    ok, msg = await self._settle_weth_debt_to_target_usd(
                        target_debt_usd=0.0,
                        target_hf=float(self.MIN_HEALTH_FACTOR),
                        collateral_factors=collateral_factors,
                        mode="exit",
                        max_batch_usd=2500.0,
                        max_steps=20,
                    )
                except SwapOutcomeUnknownError as exc:
                    return (
                        False,
                        f"post-run guard: swap outcome unknown during deleverage: {exc}",
                    )
                if not ok:
                    return (False, f"post-run guard: deleverage failed: {msg}")

                snap, _ = await self._accounting_snapshot(
                    collateral_factors=collateral_factors
                )

            # 2) Delta guard: keep ETH delta roughly neutral.
            # We treat delta mismatch as (debt_usd - wstETH_collateral_usd).
            tol = max(float(self.DELTA_TOL_USD), float(self.min_withdraw_usd))

            for _ in range(int(self.POST_RUN_MAX_PASSES)):
                mismatch = self._delta_mismatch_usd(snap)
                short_usd = max(0.0, float(mismatch))
                long_usd = max(0.0, float(-mismatch))

                if snap.weth_debt <= 1:
                    return (True, f"post-run guard: no debt (hf={snap.hf:.3f})")

                if abs(float(mismatch)) <= float(tol):
                    return (
                        True,
                        "post-run guard: delta ok "
                        f"(short=${short_usd:.2f}, long=${long_usd:.2f}, hf={snap.hf:.3f})",
                    )

                # Net long: unwind excess wstETH into USDC collateral (operate mode only).
                if mismatch < -tol:
                    if mode == "exit":
                        return (
                            True,
                            "post-run guard: delta ok for exit "
                            f"(short=${short_usd:.2f}, long=${long_usd:.2f}, hf={snap.hf:.3f})",
                        )

                    unwind_usd = max(0.0, float(long_usd) - float(tol))
                    try:
                        ok, msg = await self._reduce_wsteth_long_to_usdc_collateral(
                            collateral_factors=collateral_factors,
                            unwind_usd=float(unwind_usd),
                            hf_floor=float(self.MIN_HEALTH_FACTOR),
                            max_batch_usd=8000.0,
                        )
                        if not ok:
                            logger.warning(f"post-run guard: long unwind failed: {msg}")
                        else:
                            logger.info(f"post-run guard: long unwind: {msg}")
                    except SwapOutcomeUnknownError as exc:
                        return (
                            False,
                            f"post-run guard: swap outcome unknown during long unwind: {exc}",
                        )

                    snap, _ = await self._accounting_snapshot(
                        collateral_factors=collateral_factors
                    )
                    continue

                # 2a) Try to fix delta without touching collateral: complete borrow→swap→lend if stuck.
                if mode != "exit":
                    try:
                        ok, msg = await self._reconcile_wallet_into_position(
                            collateral_factors=collateral_factors,
                            max_batch_usd=8000.0,
                        )
                        if not ok:
                            logger.warning(f"post-run guard: reconcile failed: {msg}")
                    except SwapOutcomeUnknownError as exc:
                        logger.warning(
                            f"post-run guard: swap outcome unknown during reconcile: {exc}"
                        )

                    snap, _ = await self._accounting_snapshot(
                        collateral_factors=collateral_factors
                    )
                    mismatch = self._delta_mismatch_usd(snap)
                    short_usd = max(0.0, float(mismatch))
                    long_usd = max(0.0, float(-mismatch))

                    if abs(float(mismatch)) <= float(tol):
                        return (
                            True,
                            "post-run guard: delta restored by reconcile "
                            f"(short=${short_usd:.2f}, long=${long_usd:.2f}, hf={snap.hf:.3f})",
                        )

                # 2b) Still net short: reduce debt down to what wstETH collateral can cover.
                wsteth_coll_usd = float(snap.totals_usd.get(f"Base_{M_WSTETH}", 0.0))
                target_debt_usd = max(0.0, wsteth_coll_usd - 1.0)
                try:
                    ok, msg = await self._settle_weth_debt_to_target_usd(
                        target_debt_usd=float(target_debt_usd),
                        collateral_factors=collateral_factors,
                        mode="exit",
                        max_batch_usd=2500.0,
                        max_steps=20,
                    )
                except SwapOutcomeUnknownError as exc:
                    return (
                        False,
                        f"post-run guard: swap outcome unknown during delta delever: {exc}",
                    )
                if not ok:
                    return (False, f"post-run guard: could not delever to delta: {msg}")

                snap, _ = await self._accounting_snapshot(
                    collateral_factors=collateral_factors
                )

            mismatch = self._delta_mismatch_usd(snap)
            short_usd = max(0.0, float(mismatch))
            long_usd = max(0.0, float(-mismatch))
            if mismatch > tol:
                return (
                    False,
                    "post-run guard: exceeded passes; "
                    f"remaining short=${short_usd:.2f}, long=${long_usd:.2f}, hf={snap.hf:.3f}",
                )
            return (
                True,
                "post-run guard: exceeded passes; "
                f"remaining short=${short_usd:.2f}, long=${long_usd:.2f}, hf={snap.hf:.3f}",
            )

        except Exception as exc:
            if prior_error is not None:
                logger.warning(
                    f"post-run guard crashed after prior error {type(prior_error).__name__}: {prior_error}"
                )
            return (False, f"post-run guard crashed: {type(exc).__name__}: {exc}")

    def _target_leverage(
        self,
        *,
        collateral_factors: tuple[float, float],
        target_hf: float | None = None,
    ) -> float:
        cf_u, cf_w = collateral_factors
        hf = (
            float(target_hf)
            if target_hf is not None
            else float(self.TARGET_HEALTH_FACTOR)
        )

        if cf_u <= 0 or hf <= 0:
            return 0.0

        denominator = float(hf) + 0.001 - float(cf_w)
        if denominator <= 0:
            return 0.0

        return float(cf_u) / float(denominator) + 1.0

    async def _delever_wsteth_to_target_leverage(
        self,
        *,
        target_leverage: float,
        collateral_factors: tuple[float, float],
        max_over_leverage: float | None = None,
        max_batch_usd: float = 4000.0,
        max_steps: int = 10,
    ) -> tuple[bool, str]:
        band = (
            float(max_over_leverage)
            if max_over_leverage is not None
            else float(self.LEVERAGE_DELEVERAGE_BUFFER)
        )
        band = max(0.0, float(band))

        if not target_leverage or float(target_leverage) <= 0:
            return (True, "Target leverage unavailable; skipping leverage delever")

        addr = self._get_strategy_wallet_address()

        for _step in range(int(max_steps)):
            snap, _ = await self._accounting_snapshot(
                collateral_factors=collateral_factors
            )

            usdc_key = f"Base_{M_USDC}"
            wsteth_key = f"Base_{M_WSTETH}"
            usdc_lend_value = float(snap.totals_usd.get(usdc_key, 0.0))
            wsteth_lend_value = float(snap.totals_usd.get(wsteth_key, 0.0))

            if usdc_lend_value <= 0 or wsteth_lend_value <= 0:
                return (True, "No leveraged wstETH position to delever")

            current_leverage = wsteth_lend_value / usdc_lend_value + 1.0
            if current_leverage <= float(target_leverage) + float(band):
                return (
                    True,
                    f"Leverage within band. leverage={current_leverage:.2f}x "
                    f"<= target+buffer={(float(target_leverage) + float(band)):.2f}x",
                )

            if snap.weth_debt <= 0:
                return (True, "No WETH debt; nothing to delever")

            if not snap.wsteth_price or snap.wsteth_price <= 0:
                return (False, "wstETH price unavailable; cannot delever safely")

            target_wsteth_usd = max(
                0.0, (float(target_leverage) - 1.0) * float(usdc_lend_value)
            )
            remaining_usd = max(
                0.0, float(wsteth_lend_value) - float(target_wsteth_usd)
            )
            batch_usd = min(float(max_batch_usd), float(remaining_usd))

            safe_withdraw_usd = self._max_safe_withdraw_usd(
                totals_usd=snap.totals_usd,
                withdraw_key=wsteth_key,
                collateral_factors=collateral_factors,
                hf_floor=float(self.LEVERAGE_DELEVER_HF_FLOOR),
            )
            withdraw_usd = min(float(batch_usd), float(safe_withdraw_usd))

            if withdraw_usd <= max(1.0, float(self.min_withdraw_usd)):
                return (
                    False,
                    "Unable to safely withdraw wstETH collateral to delever "
                    f"(safe_withdraw_usd=${safe_withdraw_usd:.2f}, remaining_usd=${remaining_usd:.2f}, hf={snap.hf:.3f})",
                )

            underlying_raw = (
                int(withdraw_usd / snap.wsteth_price * 10**snap.wsteth_dec) + 1
            )
            if underlying_raw <= 0:
                return (False, "Calculated delever withdrawal amount was 0")

            mw_res = await self.moonwell_adapter.max_withdrawable_mtoken(
                mtoken=M_WSTETH
            )
            if not mw_res[0]:
                return (
                    False,
                    f"Failed to compute max withdrawable wstETH: {mw_res[1]}",
                )
            withdraw_info = mw_res[1]
            if not isinstance(withdraw_info, dict):
                return (False, f"Bad withdraw info for wstETH: {withdraw_info}")

            mtoken_amt = self._mtoken_amount_for_underlying(
                withdraw_info, underlying_raw
            )
            if mtoken_amt <= 0:
                return (
                    False,
                    "Could not compute a withdrawable mToken amount for delever",
                )

            ok, unlend_res = await self.moonwell_adapter.unlend(
                mtoken=M_WSTETH, amount=mtoken_amt
            )
            if not ok:
                return (False, f"Failed to unlend wstETH for delever: {unlend_res}")

            pinned_block = self._pinned_block(unlend_res)

            wallet_wsteth = await self._get_balance_raw(
                token_id=WSTETH_TOKEN_ID,
                wallet_address=addr,
                block_identifier=pinned_block,
            )
            amount_to_swap = min(int(wallet_wsteth), int(underlying_raw))
            if amount_to_swap <= 0:
                return (
                    False,
                    "Delever unlend succeeded but no wstETH observed in wallet "
                    f"(wallet_wsteth={wallet_wsteth}, pinned_block={pinned_block})",
                )

            repaid = await self._swap_to_weth_and_repay(
                WSTETH_TOKEN_ID, amount_to_swap, snap.weth_debt
            )
            if repaid <= 0:
                logger.warning(
                    "Leverage delever swap->repay failed; re-lending wstETH to restore position"
                )
                relend_bal = await self._get_balance_raw(
                    token_id=WSTETH_TOKEN_ID, wallet_address=addr
                )
                if relend_bal > 0:
                    await self.moonwell_adapter.lend(
                        mtoken=M_WSTETH, underlying_token=WSTETH, amount=relend_bal
                    )
                    await self.moonwell_adapter.set_collateral(mtoken=M_WSTETH)

                return (
                    False,
                    "Failed swapping wstETH->WETH and repaying during leverage delever",
                )

        return (False, f"Exceeded max_steps={max_steps} while deleveraging leverage")

    async def _reconcile_wallet_into_position(
        self,
        *,
        collateral_factors: tuple[float, float],
        max_batch_usd: float = 5000.0,
    ) -> tuple[bool, str]:
        snap, _ = await self._accounting_snapshot(collateral_factors=collateral_factors)
        addr = self._get_strategy_wallet_address()

        # 1) Lend loose wallet wstETH first
        if snap.wallet_wsteth > 0:
            usd_val = (
                (snap.wallet_wsteth / 10**snap.wsteth_dec) * snap.wsteth_price
                if snap.wsteth_price
                else 0.0
            )
            if usd_val >= float(self.min_withdraw_usd):
                ok, msg = await self.moonwell_adapter.lend(
                    mtoken=M_WSTETH,
                    underlying_token=WSTETH,
                    amount=int(snap.wallet_wsteth),
                )
                if not ok:
                    return (False, f"Failed to lend wallet wstETH: {msg}")
                # Ensure it's collateral (idempotent).
                await self.moonwell_adapter.set_collateral(mtoken=M_WSTETH)

        # Refresh (cheap) for next decisions
        snap, _ = await self._accounting_snapshot(collateral_factors=collateral_factors)

        if snap.weth_debt <= 0 or not snap.weth_price or snap.weth_price <= 0:
            logger.info(
                "  Result: No WETH debt (or missing price); wallet reconciliation done"
            )
            return (True, "No WETH debt (or missing price); wallet reconciliation done")

        # 2) How much wstETH collateral are we missing vs debt (delta-neutral intent)
        wsteth_coll_usd = snap.totals_usd.get(f"Base_{M_WSTETH}", 0.0)
        deficit_usd = max(0.0, float(snap.debt_usd) - float(wsteth_coll_usd))

        if deficit_usd <= max(1.0, float(self.min_withdraw_usd)):
            logger.info(
                "  Result: wstETH collateral roughly matches WETH debt; wallet reconciliation done"
            )
            return (
                True,
                "wstETH collateral roughly matches WETH debt; wallet reconciliation done",
            )

        deficit_usd = min(deficit_usd, float(max_batch_usd))
        needed_weth_raw = (
            int(deficit_usd / snap.weth_price * 10**snap.weth_dec / (1 - 0.005)) + 1
        )

        # 3) Use wallet WETH -> wstETH
        used_any = False
        if snap.wallet_weth > 0 and needed_weth_raw > 0:
            amt = min(int(snap.wallet_weth), int(needed_weth_raw))
            if amt > 0:
                wsteth_before = await self._get_balance_raw(
                    token_id=WSTETH_TOKEN_ID, wallet_address=addr
                )
                swap_res = await self._swap_with_retries(
                    from_token_id=WETH_TOKEN_ID,
                    to_token_id=WSTETH_TOKEN_ID,
                    amount=amt,
                    preferred_providers=["aerodrome", "enso"],
                )
                if swap_res is None:
                    return (
                        False,
                        "Failed swapping wallet WETH->wstETH during reconciliation",
                    )
                pinned_block = self._pinned_block(swap_res)
                wsteth_after = await self._get_balance_raw(
                    token_id=WSTETH_TOKEN_ID,
                    wallet_address=addr,
                    block_identifier=pinned_block,
                )
                got = max(0, int(wsteth_after) - int(wsteth_before))
                if got > 0:
                    ok, msg = await self.moonwell_adapter.lend(
                        mtoken=M_WSTETH, underlying_token=WSTETH, amount=got
                    )
                    if not ok:
                        return (False, f"Failed lending swapped wstETH: {msg}")
                    await self.moonwell_adapter.set_collateral(mtoken=M_WSTETH)
                    used_any = True

                needed_weth_raw = max(0, int(needed_weth_raw) - int(amt))

        # 4) Use wallet ETH (above reserve) -> wstETH
        if needed_weth_raw > 0 and snap.eth_usable_wei > 0:
            eth_amt = min(int(snap.eth_usable_wei), int(needed_weth_raw))
            if eth_amt > 0:
                wsteth_before = await self._get_balance_raw(
                    token_id=WSTETH_TOKEN_ID, wallet_address=addr
                )
                swap_res = await self._swap_with_retries(
                    from_token_id=ETH_TOKEN_ID,
                    to_token_id=WSTETH_TOKEN_ID,
                    amount=eth_amt,
                    preferred_providers=["aerodrome", "enso"],
                )
                if swap_res is None:
                    return (
                        False,
                        "Failed swapping usable wallet ETH->wstETH during reconciliation",
                    )
                pinned_block = self._pinned_block(swap_res)
                wsteth_after = await self._get_balance_raw(
                    token_id=WSTETH_TOKEN_ID,
                    wallet_address=addr,
                    block_identifier=pinned_block,
                )
                got = max(0, int(wsteth_after) - int(wsteth_before))
                if got > 0:
                    ok, msg = await self.moonwell_adapter.lend(
                        mtoken=M_WSTETH, underlying_token=WSTETH, amount=got
                    )
                    if not ok:
                        return (False, f"Failed lending swapped wstETH: {msg}")
                    await self.moonwell_adapter.set_collateral(mtoken=M_WSTETH)
                    used_any = True

        return (
            True,
            "Wallet reconciliation completed"
            if used_any
            else "No usable wallet assets to reconcile further",
        )

    async def _reduce_wsteth_long_to_usdc_collateral(
        self,
        *,
        collateral_factors: tuple[float, float],
        unwind_usd: float,
        hf_floor: float,
        max_batch_usd: float = 8000.0,
    ) -> tuple[bool, str]:
        if unwind_usd <= 0:
            return (True, "No wstETH long to unwind")

        addr = self._get_strategy_wallet_address()
        snap, _ = await self._accounting_snapshot(collateral_factors=collateral_factors)

        if not snap.wsteth_price or snap.wsteth_price <= 0:
            return (False, "wstETH price unavailable; cannot unwind long safely")

        safe_unlend_usd = self._max_safe_withdraw_usd(
            totals_usd=snap.totals_usd,
            withdraw_key=f"Base_{M_WSTETH}",
            collateral_factors=collateral_factors,
            hf_floor=float(hf_floor),
        )
        desired_usd = min(
            float(unwind_usd), float(max_batch_usd), float(safe_unlend_usd)
        )

        min_usd = max(1.0, float(self.min_withdraw_usd))
        if desired_usd < min_usd:
            return (
                True,
                f"wstETH long unwind not needed/too small (desired=${desired_usd:.2f})",
            )

        desired_underlying_raw = (
            int(desired_usd / snap.wsteth_price * 10**snap.wsteth_dec) + 1
        )
        if desired_underlying_raw <= 0:
            return (False, "Computed 0 wstETH to unlend")

        mw_ok, mw_info = await self.moonwell_adapter.max_withdrawable_mtoken(
            mtoken=M_WSTETH
        )
        if not mw_ok or not isinstance(mw_info, dict):
            return (False, f"Failed to compute withdrawable mwstETH: {mw_info}")

        mtoken_amt = self._mtoken_amount_for_underlying(mw_info, desired_underlying_raw)
        if mtoken_amt <= 0:
            return (False, "mwstETH withdrawable amount is 0 (cash/shortfall bound)")

        ok, msg = await self.moonwell_adapter.unlend(mtoken=M_WSTETH, amount=mtoken_amt)
        if not ok:
            return (False, f"Failed to redeem mwstETH to unwind long: {msg}")

        pinned_block = self._pinned_block(msg)
        wsteth_wallet_raw = await self._balance_after_tx(
            token_id=WSTETH_TOKEN_ID,
            wallet=addr,
            pinned_block=pinned_block,
            min_expected=1,
            attempts=5,
        )
        if wsteth_wallet_raw <= 0 and pinned_block is not None:
            wsteth_wallet_raw = await self._balance_after_tx(
                token_id=WSTETH_TOKEN_ID,
                wallet=addr,
                pinned_block=None,
                min_expected=1,
                attempts=5,
            )

        amount_to_swap = min(int(wsteth_wallet_raw), int(desired_underlying_raw))
        if amount_to_swap <= 0:
            return (False, "No wstETH available in wallet after unlend")

        swap_res = await self._swap_with_retries(
            from_token_id=WSTETH_TOKEN_ID,
            to_token_id=USDC_TOKEN_ID,
            amount=amount_to_swap,
            preferred_providers=["aerodrome", "enso"],
        )
        if swap_res is None:
            # Restore collateral: re-lend wstETH if the swap fails.
            restore_amt = await self._get_balance_raw(
                token_id=WSTETH_TOKEN_ID,
                wallet_address=addr,
                block_identifier=pinned_block,
            )
            if restore_amt > 0:
                await self.moonwell_adapter.lend(
                    mtoken=M_WSTETH,
                    underlying_token=WSTETH,
                    amount=restore_amt,
                )
                await self.moonwell_adapter.set_collateral(mtoken=M_WSTETH)
            return (False, "Failed swapping wstETH->USDC while unwinding long")

        # Lend resulting USDC back as collateral (idempotent).
        usdc_wallet_raw = await self._get_balance_raw(
            token_id=USDC_TOKEN_ID, wallet_address=addr
        )
        if usdc_wallet_raw > 0:
            lend_ok, lend_msg = await self.moonwell_adapter.lend(
                mtoken=M_USDC, underlying_token=USDC, amount=int(usdc_wallet_raw)
            )
            if not lend_ok:
                return (
                    False,
                    f"wstETH->USDC swap succeeded but lending USDC failed: {lend_msg}",
                )
            await self.moonwell_adapter.set_collateral(mtoken=M_USDC)

        return (
            True,
            f"Reduced long wstETH by ≈${desired_usd:.2f} via mwstETH->wstETH->USDC->mUSDC",
        )

    async def _settle_weth_debt_to_target_usd(
        self,
        *,
        target_debt_usd: float,
        target_hf: float | None = None,
        collateral_factors: tuple[float, float],
        mode: str,
        max_batch_usd: float = 4000.0,
        max_steps: int = 20,
    ) -> tuple[bool, str]:
        logger.info(
            f"SETTLE DEBT: target_debt=${target_debt_usd:.2f}, target_hf={target_hf}, mode={mode}"
        )
        addr = self._get_strategy_wallet_address()
        effective_target_hf = float(target_hf) if target_hf is not None else None
        if effective_target_hf is not None and effective_target_hf <= 0:
            effective_target_hf = None

        is_full_exit = (
            mode == "exit"
            and float(target_debt_usd) <= 0.0
            and effective_target_hf is None
        )

        def _debt_target_usd(
            snap: "MoonwellWstethLoopStrategy.AccountingSnapshot",
        ) -> float:
            if is_full_exit:
                return 0.0
            if effective_target_hf is not None:
                if snap.capacity_usd <= 0:
                    return 0.0
                return float(snap.capacity_usd) / float(effective_target_hf)
            return float(target_debt_usd)

        for step in range(max_steps):
            snap, _ = await self._accounting_snapshot(
                collateral_factors=collateral_factors
            )

            if is_full_exit:
                # Treat 1 wei dust as cleared (repay_full allows <=1 wei remaining).
                if snap.weth_debt <= 1:
                    return (
                        True,
                        f"Debt settled. debt=${snap.debt_usd:.6f} (weth_debt={snap.weth_debt})",
                    )
            elif effective_target_hf is not None:
                if snap.hf >= effective_target_hf:
                    return (
                        True,
                        f"HF target reached. hf={snap.hf:.3f} >= target={effective_target_hf:.3f}",
                    )
            elif snap.debt_usd <= target_debt_usd + 1.0:
                return (
                    True,
                    f"Debt settled to target. debt=${snap.debt_usd:.2f} <= target=${target_debt_usd:.2f}",
                )

            if not snap.weth_price or snap.weth_price <= 0:
                return (False, "WETH price unavailable; cannot settle debt safely")

            debt_target_usd = _debt_target_usd(snap)
            remaining_usd = max(0.0, float(snap.debt_usd) - float(debt_target_usd))
            if is_full_exit:
                # For full exit, aim to source a small WETH buffer so repayBorrow(MAX_UINT256)
                # can fully clear the debt even with minor interest/rounding drift.
                remaining_usd = snap.debt_usd
                batch_usd = min(
                    float(max_batch_usd),
                    max(
                        float(remaining_usd) * float(self.FULL_EXIT_BUFFER_MULT),
                        float(self.FULL_EXIT_MIN_BATCH_USD),
                    ),
                )
            else:
                batch_usd = min(float(max_batch_usd), float(remaining_usd))
            batch_weth_raw = int(batch_usd / snap.weth_price * 10**snap.weth_dec) + 1

            progressed = False

            # 1) Wallet WETH -> repay
            if snap.wallet_weth > 0:
                repay_amt = min(int(snap.wallet_weth), int(batch_weth_raw))
                if repay_amt > 0:
                    repaid = await self._repay_weth(repay_amt, snap.weth_debt)
                    if repaid > 0:
                        progressed = True
                        continue

            # 2) Wallet ETH (above reserve) -> wrap -> repay
            if snap.eth_usable_wei > 0:
                wrap_amt = min(int(snap.eth_usable_wei), int(batch_weth_raw))
                if wrap_amt > 0:
                    wrap_ok, wrap_msg = await self.moonwell_adapter.wrap_eth(
                        amount=wrap_amt
                    )
                    if wrap_ok:
                        pinned_block = self._pinned_block(wrap_msg)
                        weth_now = await self._get_balance_raw(
                            token_id=WETH_TOKEN_ID,
                            wallet_address=addr,
                            block_identifier=pinned_block,
                        )
                        if weth_now > 0:
                            repaid = await self._repay_weth(weth_now, snap.weth_debt)
                            if repaid > 0:
                                progressed = True
                                continue
                    else:
                        logger.warning(
                            f"wrap_eth failed during debt settle: {wrap_msg}"
                        )

            # 3) Wallet wstETH -> swap -> repay (skip dust)
            wallet_wsteth_usd = (
                (snap.wallet_wsteth / (10**snap.wsteth_dec)) * float(snap.wsteth_price)
                if snap.wsteth_price
                else 0.0
            )
            if (
                snap.wallet_wsteth > 0
                and snap.wsteth_price
                and snap.wsteth_price > 0
                and wallet_wsteth_usd >= 1.0
            ):
                needed_wsteth_usd = batch_usd * 1.02
                needed_wsteth_raw = (
                    int(needed_wsteth_usd / snap.wsteth_price * 10**snap.wsteth_dec) + 1
                )
                swap_amt = min(int(snap.wallet_wsteth), int(needed_wsteth_raw))
                if swap_amt > 0:
                    repaid = await self._swap_to_weth_and_repay(
                        WSTETH_TOKEN_ID, swap_amt, snap.weth_debt
                    )
                    if repaid > 0:
                        progressed = True
                        continue

            # 4) Wallet USDC -> swap -> repay (skip dust)
            wallet_usdc_usd = (
                (snap.wallet_usdc / (10**snap.usdc_dec)) * float(snap.usdc_price)
                if snap.usdc_price
                else 0.0
            )
            if (
                snap.wallet_usdc > 0
                and snap.usdc_price
                and snap.usdc_price > 0
                and wallet_usdc_usd >= 1.0
            ):
                needed_usdc_usd = batch_usd * 1.02
                needed_usdc_raw = (
                    int(needed_usdc_usd / snap.usdc_price * 10**snap.usdc_dec) + 1
                )
                swap_amt = min(int(snap.wallet_usdc), int(needed_usdc_raw))
                if swap_amt > 0:
                    repaid = await self._swap_to_weth_and_repay(
                        USDC_TOKEN_ID, swap_amt, snap.weth_debt
                    )
                    if repaid > 0:
                        progressed = True
                        continue

            # 5) Need more: redeem collateral in HF-safe batches, then swap -> repay.
            if mode == "operate":
                hf_floor = float(self.MIN_HEALTH_FACTOR)
            elif is_full_exit:
                # Full unwind can allow HF to dip temporarily during the unlend tx
                # (swap+repay follows immediately). This reduces the number of small
                # redeem+swap+repay cycles when HF is just above MIN_HEALTH_FACTOR.
                hf_floor = float(self.LEVERAGE_DELEVER_HF_FLOOR)
            else:
                hf_floor = max(
                    self.LEVERAGE_DELEVER_HF_FLOOR,
                    min(
                        float(self.MIN_HEALTH_FACTOR),
                        float(snap.hf) - 0.02
                        if snap.hf != float("inf")
                        else float(self.MIN_HEALTH_FACTOR),
                    ),
                )

            # Choose the collateral source that can actually be withdrawn (HF-safe + cash bound)
            # in the largest size. This avoids getting stuck redeeming ever-smaller amounts when
            snap, _ = await self._accounting_snapshot(
                collateral_factors=collateral_factors
            )

            if is_full_exit:
                if snap.weth_debt <= 1:
                    logger.info("  Result: Debt fully settled")
                    return (True, "Debt settled")
            elif effective_target_hf is not None:
                if snap.hf >= effective_target_hf:
                    logger.info(f"  Result: HF target reached (HF={snap.hf:.3f})")
                    return (True, "HF target reached")
            elif snap.debt_usd <= target_debt_usd + 1.0:
                logger.info(
                    f"  Result: Debt settled to target (debt=${snap.debt_usd:.2f})"
                )
                return (True, "Debt settled to target")

            debt_target_usd = _debt_target_usd(snap)
            remaining_usd = max(0.0, float(snap.debt_usd) - float(debt_target_usd))
            if is_full_exit:
                remaining_usd = snap.debt_usd
                batch_usd = min(
                    float(max_batch_usd),
                    max(
                        float(remaining_usd) * float(self.FULL_EXIT_BUFFER_MULT),
                        float(self.FULL_EXIT_MIN_BATCH_USD),
                    ),
                )
            else:
                batch_usd = min(float(max_batch_usd), float(remaining_usd))

            # Withdraw a small extra buffer so the subsequent swap->repay can tolerate slippage,
            # while still respecting the HF-safe withdrawal bound.
            slip = float(self.swap_slippage_tolerance)
            slip = max(0.0, min(slip, float(self.MAX_SLIPPAGE_TOLERANCE)))
            buffer_factor = 1.0 / (1.0 - slip) if slip < 0.999 else 1.0

            # For full exit, allow smaller redemptions than operate-mode, but skip sub-$1 dust
            # to avoid spinning on tiny swaps.
            min_redeem_usd = (
                1.0 if is_full_exit else max(1.0, float(self.min_withdraw_usd))
            )

            candidates: list[dict[str, Any]] = []
            for withdraw_mtoken, withdraw_token_id, withdraw_key in [
                (M_WSTETH, WSTETH_TOKEN_ID, f"Base_{M_WSTETH}"),
                (M_USDC, USDC_TOKEN_ID, f"Base_{M_USDC}"),
            ]:
                safe_withdraw_usd = self._max_safe_withdraw_usd(
                    totals_usd=snap.totals_usd,
                    withdraw_key=withdraw_key,
                    collateral_factors=collateral_factors,
                    hf_floor=hf_floor,
                )

                desired_withdraw_usd = min(
                    float(batch_usd) * buffer_factor, float(safe_withdraw_usd)
                )
                if desired_withdraw_usd <= float(min_redeem_usd):
                    continue

                if withdraw_token_id == WSTETH_TOKEN_ID:
                    price = float(snap.wsteth_price)
                    dec = int(snap.wsteth_dec)
                else:
                    price = float(snap.usdc_price)
                    dec = int(snap.usdc_dec)

                if not price or price <= 0:
                    continue

                desired_underlying_raw = int(desired_withdraw_usd / price * 10**dec) + 1
                if desired_underlying_raw <= 0:
                    continue

                mw_ok, mw_info = await self.moonwell_adapter.max_withdrawable_mtoken(
                    mtoken=withdraw_mtoken
                )
                if not mw_ok or not isinstance(mw_info, dict):
                    continue

                max_underlying_raw = int(mw_info.get("underlying_raw", 0) or 0)
                if max_underlying_raw <= 0:
                    continue

                expected_underlying_raw = min(
                    int(desired_underlying_raw), max_underlying_raw
                )
                expected_usd = (expected_underlying_raw / (10**dec)) * price
                if expected_usd <= float(min_redeem_usd):
                    continue

                mtoken_amt = self._mtoken_amount_for_underlying(
                    mw_info, int(desired_underlying_raw)
                )
                if mtoken_amt <= 0:
                    continue

                candidates.append(
                    {
                        "expected_usd": float(expected_usd),
                        "safe_withdraw_usd": float(safe_withdraw_usd),
                        "cash_bound_usd": float(
                            (max_underlying_raw / (10**dec)) * price
                        ),
                        "withdraw_mtoken": withdraw_mtoken,
                        "withdraw_token_id": withdraw_token_id,
                        "underlying_raw": int(desired_underlying_raw),
                        "mtoken_amt": int(mtoken_amt),
                    }
                )

            if not candidates:
                logger.warning(
                    "Debt settle: no HF-safe withdrawable collateral found "
                    f"(batch=${float(batch_usd):.2f}, hf_floor={float(hf_floor):.2f})"
                )

            candidates.sort(key=lambda c: float(c["expected_usd"]), reverse=True)

            for chosen in candidates:
                withdraw_mtoken = str(chosen["withdraw_mtoken"])
                withdraw_token_id = str(chosen["withdraw_token_id"])
                underlying_raw = int(chosen["underlying_raw"])
                mtoken_amt = int(chosen["mtoken_amt"])

                logger.info(
                    f"Debt settle: redeem {withdraw_token_id} "
                    f"(expected≈${float(chosen['expected_usd']):.2f}, "
                    f"safe=${float(chosen['safe_withdraw_usd']):.2f}, "
                    f"cash≤${float(chosen['cash_bound_usd']):.2f}, "
                    f"batch=${float(batch_usd):.2f}, hf_floor={float(hf_floor):.2f})"
                )

                ok, msg = await self.moonwell_adapter.unlend(
                    mtoken=withdraw_mtoken, amount=mtoken_amt
                )
                if not ok:
                    logger.warning(f"unlend failed for {withdraw_mtoken}: {msg}")
                    continue

                pinned_block = self._pinned_block(msg)
                wallet_underlying = await self._balance_after_tx(
                    token_id=withdraw_token_id,
                    wallet=addr,
                    pinned_block=pinned_block,
                    min_expected=1,
                    attempts=5,
                )
                if wallet_underlying <= 0 and pinned_block is not None:
                    wallet_underlying = await self._balance_after_tx(
                        token_id=withdraw_token_id,
                        wallet=addr,
                        pinned_block=None,
                        min_expected=1,
                        attempts=5,
                    )

                amount_to_swap = min(int(wallet_underlying), int(underlying_raw))
                if amount_to_swap <= 0:
                    continue

                repaid = await self._swap_to_weth_and_repay(
                    withdraw_token_id, amount_to_swap, snap.weth_debt
                )
                if repaid > 0:
                    progressed = True
                    break

                # Swap failed: restore collateral to avoid leaving risk worsened.
                logger.warning(
                    f"swap->repay failed after unlend ({withdraw_token_id}); re-lending to restore"
                )
                relend_bal = await self._get_balance_raw(
                    token_id=withdraw_token_id,
                    wallet_address=addr,
                )
                if relend_bal > 0:
                    underlying_addr = WSTETH if withdraw_mtoken == M_WSTETH else USDC
                    await self.moonwell_adapter.lend(
                        mtoken=withdraw_mtoken,
                        underlying_token=underlying_addr,
                        amount=relend_bal,
                    )
                    if withdraw_mtoken == M_WSTETH:
                        await self.moonwell_adapter.set_collateral(mtoken=M_WSTETH)

            if progressed:
                continue

            gap = self._debt_gap_report(snap)
            return (
                False,
                f"Could not progress debt settlement (step={step + 1}/{max_steps}). Gap report: {gap}",
            )

        return (False, f"Exceeded max_steps={max_steps} while settling debt")

    async def setup(self):
        if self.token_adapter is None:
            raise RuntimeError("Token adapter not initialized.")

        for token_id in [USDC_TOKEN_ID, WETH_TOKEN_ID, WSTETH_TOKEN_ID, ETH_TOKEN_ID]:
            try:
                success, info = await self.token_adapter.get_token(token_id)
                if success:
                    self._token_info_cache[token_id] = info
            except Exception as e:
                logger.warning(f"Failed to fetch token info for {token_id}: {e}")

    async def _get_token_info(self, token_id: str) -> dict:
        if token_id in self._token_info_cache:
            return self._token_info_cache[token_id]

        success, info = await self.token_adapter.get_token(token_id)
        if success:
            self._token_info_cache[token_id] = info
            return info
        return {}

    async def _get_token_price(self, token_id: str) -> float:
        now = time.time()

        if token_id in self._token_price_cache:
            timestamp = self._token_price_timestamps.get(token_id, 0)
            if now - timestamp < self.PRICE_STALENESS_THRESHOLD:
                return self._token_price_cache[token_id]
            else:
                logger.debug(f"Price cache stale for {token_id}, refreshing")

        success, price_data = await self.token_adapter.get_token_price(token_id)
        if success and isinstance(price_data, dict):
            price = price_data.get("current_price", 0.0)
            if price and price > 0:
                self._token_price_cache[token_id] = price
                self._token_price_timestamps[token_id] = now
                return price

        logger.warning(
            f"Failed to get fresh price for {token_id}, success={success}, price_data={price_data}"
        )
        return 0.0

    def _clear_price_cache(self):
        self._token_price_cache.clear()
        self._token_price_timestamps.clear()

    async def _get_token_data(self, token_id: str) -> tuple[float, int]:
        price = await self._get_token_price(token_id)
        info = await self._get_token_info(token_id)
        return price, info.get("decimals", 18)

    async def _swap_with_retries(
        self,
        from_token_id: str,
        to_token_id: str,
        amount: int,
        max_retries: int | None = None,
        base_slippage: float | None = None,
        preferred_providers: list[str] | None = None,
    ) -> dict | None:
        if max_retries is None:
            max_retries = self.max_swap_retries
        if base_slippage is None:
            base_slippage = self.swap_slippage_tolerance

        from_decimals = (
            18 if from_token_id in (ETH_TOKEN_ID, WETH_TOKEN_ID, WSTETH_TOKEN_ID) else 6
        )
        from_symbol = (
            from_token_id.split("-")[0].upper()
            if "-" in from_token_id
            else from_token_id[-6:].upper()
        )
        to_symbol = (
            to_token_id.split("-")[0].upper()
            if "-" in to_token_id
            else to_token_id[-6:].upper()
        )
        logger.info(
            f"SWAP: {amount / 10**from_decimals:.6f} {from_symbol} → {to_symbol}"
        )

        last_error: Exception | None = None
        strategy_address = self._get_strategy_wallet_address()

        # Always balance-check swap inputs to avoid on-chain reverts from stale/rounded values.
        try:
            wallet_balance = await self._get_balance_raw(
                token_id=from_token_id,
                wallet_address=strategy_address,
            )
            logger.debug(
                f"  Balance check: {from_symbol} wallet={wallet_balance / 10**from_decimals:.6f}"
            )
            if from_token_id == ETH_TOKEN_ID:
                reserve = int(self._gas_keep_wei())
                wallet_balance = max(0, wallet_balance - reserve)
                logger.debug(
                    f"  After gas reserve: {wallet_balance / 10**from_decimals:.6f}"
                )
            if int(amount) > wallet_balance:
                logger.info(
                    f"  Adjusting amount from {amount / 10**from_decimals:.6f} to {wallet_balance / 10**from_decimals:.6f} (wallet limit)"
                )
            amount = min(int(amount), wallet_balance)
        except Exception as exc:
            logger.warning(f"Failed to check swap balance for {from_token_id}: {exc}")

        if amount <= 0:
            logger.warning(
                f"Swap skipped: no available balance for {from_symbol} (post-reserve)"
            )
            return None

        # Wrap ETH to WETH before swapping - direct ETH swaps get bad fills
        if from_token_id == ETH_TOKEN_ID:
            logger.info(f"Wrapping {amount / 10**18:.6f} ETH to WETH before swap")
            wrap_success, wrap_msg = await self.moonwell_adapter.wrap_eth(amount=amount)
            if not wrap_success:
                logger.error(f"Failed to wrap ETH to WETH: {wrap_msg}")
                return None
            from_token_id = WETH_TOKEN_ID

        def _is_unknown_outcome_message(msg: str) -> bool:
            m = (msg or "").lower()
            return (
                "transaction pending" in m
                or "dropped/unknown" in m
                or "not in the chain after" in m
                or "no receipt after" in m
            )

        for i in range(max_retries):
            # Cap slippage at MAX_SLIPPAGE_TOLERANCE to prevent MEV attacks
            slippage = min(base_slippage * (i + 1), self.MAX_SLIPPAGE_TOLERANCE)

            # On the final retry, try a different provider ordering to avoid getting stuck
            # on a single provider/route that may be intermittently broken.
            attempt_providers = preferred_providers
            if i == max_retries - 1:
                if preferred_providers:
                    # Rotate preference order (e.g., [a, b] -> [b, a]) to encourage a different route.
                    attempt_providers = (
                        preferred_providers[1:] + preferred_providers[:1]
                        if len(preferred_providers) > 1
                        else None
                    )
                else:
                    # If the caller didn't specify providers, try a last-resort preference order.
                    attempt_providers = ["enso", "aerodrome", "lifi"]

            try:
                success, result = await self.brap_adapter.swap_from_token_ids(
                    from_token_id=from_token_id,
                    to_token_id=to_token_id,
                    from_address=strategy_address,
                    amount=str(amount),
                    slippage=slippage,
                    preferred_providers=attempt_providers,
                )
                if success and result:
                    logger.info(
                        f"Swap succeeded on attempt {i + 1} with slippage {slippage * 100:.1f}%"
                    )
                    if isinstance(result, dict):
                        return result
                    return {"to_amount": result if isinstance(result, int) else 0}

                # Do not retry when the transaction outcome is unknown (pending/dropped).
                # Retrying swaps can create nonce gaps or duplicate fills.
                if isinstance(result, str) and _is_unknown_outcome_message(result):
                    raise SwapOutcomeUnknownError(result)

                last_error = Exception(str(result))
                logger.warning(
                    f"Swap attempt {i + 1}/{max_retries} (providers={attempt_providers}) "
                    f"returned unsuccessful: {result}"
                )
            except SwapOutcomeUnknownError:
                raise
            except Exception as e:
                if _is_unknown_outcome_message(str(e)):
                    raise SwapOutcomeUnknownError(str(e)) from e
                last_error = e
                logger.warning(
                    f"Swap attempt {i + 1}/{max_retries} (providers={attempt_providers}) "
                    f"failed with slippage {slippage * 100:.1f}%: {e}"
                )
            if i < max_retries - 1:
                await asyncio.sleep(2**i)

        logger.error(
            f"All {max_retries} swap attempts failed. Last error: {last_error}"
        )
        return None

    def _parse_balance(self, raw: Any) -> int:
        if raw is None:
            return 0
        if isinstance(raw, dict):
            raw = raw.get("balance", 0)
        try:
            return int(raw)
        except (ValueError, TypeError):
            try:
                return int(float(raw))
            except (ValueError, TypeError):
                return 0

    def _token_address_for_id(self, token_id: str) -> str | None:
        if token_id == ETH_TOKEN_ID:
            return None
        if token_id == USDC_TOKEN_ID:
            return USDC
        if token_id == WETH_TOKEN_ID:
            return WETH
        if token_id == WSTETH_TOKEN_ID:
            return WSTETH
        return None

    async def _get_balance_raw(
        self,
        *,
        token_id: str,
        wallet_address: str,
        block_identifier: int | str | None = None,
    ) -> int:
        if not token_id or not wallet_address:
            return 0

        token_address = self._token_address_for_id(token_id)
        if token_id != ETH_TOKEN_ID and not token_address:
            # Try to resolve address via token metadata (not a balance read).
            if self.token_adapter is not None:
                try:
                    success, info = await self.token_adapter.get_token(token_id)
                    if success and isinstance(info, dict):
                        token_address = info.get("address") or None
                except Exception as exc:
                    logger.warning(
                        f"Failed to resolve token address for {token_id}: {exc}"
                    )

        if token_id != ETH_TOKEN_ID and not token_address:
            # Do not fall back to API balances for execution-critical paths.
            logger.warning(
                f"Unknown token address for {token_id}; skipping balance read"
            )
            return 0

        block_id = block_identifier if block_identifier is not None else "latest"
        max_retries = 3
        last_error: Exception | None = None

        for attempt in range(max_retries):
            try:
                async with web3_from_chain_id(BASE_CHAIN_ID) as w3:
                    if token_id == ETH_TOKEN_ID:
                        bal = await w3.eth.get_balance(
                            to_checksum_address(wallet_address),
                            block_identifier=block_id,
                        )
                        return int(bal)

                    contract = w3.eth.contract(
                        address=to_checksum_address(str(token_address)),
                        abi=ERC20_ABI,
                    )
                    bal = await contract.functions.balanceOf(
                        to_checksum_address(wallet_address)
                    ).call(block_identifier=block_id)
                    return int(bal)
            except Exception as exc:
                last_error = exc if isinstance(exc, Exception) else Exception(str(exc))
                err = str(exc)
                if ("429" in err or "Too Many Requests" in err) and attempt < (
                    max_retries - 1
                ):
                    await asyncio.sleep(2**attempt)
                    continue
                logger.warning(
                    f"On-chain balance read failed for {token_id} at block {block_id}: {exc}"
                )
                return 0

        logger.warning(
            f"On-chain balance read failed after {max_retries} attempts for {token_id} "
            f"at block {block_id}: {last_error}"
        )
        return 0

    def _normalize_usd_value(self, raw: Any) -> float:
        if raw is None:
            return 0.0

        # Preserve int-ness check before coercion: ints are assumed 1e18-scaled.
        is_int = isinstance(raw, int) and not isinstance(raw, bool)
        try:
            val = float(raw)
        except (ValueError, TypeError):
            return 0.0

        if is_int:
            return val / 1e18

        # Defensive: if a float looks like a 1e18-scaled value, de-scale it.
        return val / 1e18 if val > 1e12 else val

    def _mtoken_amount_for_underlying(
        self, withdraw_info: dict[str, Any], underlying_raw: int
    ) -> int:
        if underlying_raw <= 0:
            return 0

        max_ctokens = int(withdraw_info.get("cTokens_raw", 0) or 0)
        if max_ctokens <= 0:
            return 0

        exchange_rate_raw = int(withdraw_info.get("exchangeRate_raw", 0) or 0)
        conversion_factor = withdraw_info.get("conversion_factor", 0) or 0

        if exchange_rate_raw > 0:
            # underlying = cTokens * exchangeRate / 1e18  =>  cTokens = ceil(underlying*1e18 / exchangeRate)
            ctokens_needed = (
                int(underlying_raw) * 10**18 + exchange_rate_raw - 1
            ) // exchange_rate_raw
        else:
            try:
                cf = float(conversion_factor)
            except (TypeError, ValueError):
                cf = 0.0
            ctokens_needed = (
                int(cf * int(underlying_raw)) + 1 if cf > 0 else max_ctokens
            )

        return min(int(ctokens_needed), max_ctokens)

    def _pinned_block(self, tx_result: Any) -> int | None:
        if not isinstance(tx_result, dict):
            return None

        receipt = tx_result.get("receipt") or {}
        receipt_block = (
            receipt.get("blockNumber") if isinstance(receipt, dict) else None
        )

        return (
            tx_result.get("confirmed_block_number")
            or tx_result.get("block_number")
            or receipt_block
        )

    async def _balance_after_tx(
        self,
        *,
        token_id: str,
        wallet: str,
        pinned_block: int | None,
        min_expected: int = 1,
        attempts: int = 5,
    ) -> int:
        bal = 0
        for i in range(int(attempts)):
            bal = await self._get_balance_raw(
                token_id=token_id,
                wallet_address=wallet,
                block_identifier=pinned_block,
            )
            if bal >= int(min_expected):
                return int(bal)
            await asyncio.sleep(1 + i)
        return int(bal)

    async def _get_gas_balance(self) -> int:
        return await self._get_balance_raw(
            token_id=ETH_TOKEN_ID, wallet_address=self._get_strategy_wallet_address()
        )

    async def _get_usdc_balance(self) -> int:
        return await self._get_balance_raw(
            token_id=USDC_TOKEN_ID, wallet_address=self._get_strategy_wallet_address()
        )

    async def _get_wallet_balances_usd(self) -> dict[str, dict]:
        strategy_addr = self._get_strategy_wallet_address()

        weth_raw, usdc_raw, wsteth_raw = await asyncio.gather(
            self._get_balance_raw(token_id=WETH_TOKEN_ID, wallet_address=strategy_addr),
            self._get_balance_raw(token_id=USDC_TOKEN_ID, wallet_address=strategy_addr),
            self._get_balance_raw(
                token_id=WSTETH_TOKEN_ID, wallet_address=strategy_addr
            ),
        )

        # Get token data (prices cached from _aggregate_positions)
        token_data = await asyncio.gather(
            self._get_token_data(WETH_TOKEN_ID),
            self._get_token_data(USDC_TOKEN_ID),
            self._get_token_data(WSTETH_TOKEN_ID),
        )

        def calc(raw: int, decimals: int, price: float) -> dict:
            tokens = raw / 10**decimals if raw > 0 else 0.0
            return {"tokens": tokens, "usd": tokens * price}

        return {
            "WETH": calc(weth_raw, token_data[0][1], token_data[0][0]),
            "USDC": calc(usdc_raw, token_data[1][1], token_data[1][0]),
            "wstETH": calc(wsteth_raw, token_data[2][1], token_data[2][0]),
        }

    async def _validate_gas_balance(self) -> tuple[bool, str]:
        gas_balance = await self._get_gas_balance()
        main_gas = await self._get_balance_raw(
            token_id=ETH_TOKEN_ID, wallet_address=self._get_main_wallet_address()
        )
        total_gas = gas_balance + main_gas

        min_gas_wei = int(self._gas_keep_wei())
        if total_gas < min_gas_wei:
            return (
                False,
                f"Need at least {min_gas_wei / 10**18:.4f} Base ETH for gas. "
                f"You have: {total_gas / 10**18:.6f}",
            )
        return (True, "Gas balance validated")

    async def _validate_usdc_deposit(
        self, usdc_amount: float
    ) -> tuple[bool, str, float]:
        actual_balance = await self._get_balance_raw(
            token_id=USDC_TOKEN_ID, wallet_address=self._get_main_wallet_address()
        )

        token_info = await self._get_token_info(USDC_TOKEN_ID)
        decimals = token_info.get("decimals", 6)
        available_usdc = actual_balance / (10**decimals)

        usdc_amount = min(usdc_amount, available_usdc)

        if usdc_amount < self.MIN_USDC_DEPOSIT:
            return (
                False,
                f"Minimum deposit is {self.MIN_USDC_DEPOSIT} USDC. Available: {available_usdc:.2f}",
                usdc_amount,
            )
        return (True, "USDC deposit amount validated", usdc_amount)

    async def _check_quote_profitability(self) -> tuple[bool, str]:
        quote = await self._quote()
        if quote.get("apy", 0) < 0:
            return (
                False,
                "APYs and ratios are not profitable at the moment, aborting deposit",
            )
        return (True, "Quote is profitable")

    async def _transfer_usdc_to_vault(self, usdc_amount: float) -> tuple[bool, str]:
        (
            success,
            msg,
        ) = await self.balance_adapter.move_from_main_wallet_to_strategy_wallet(
            USDC_TOKEN_ID, usdc_amount
        )
        if not success:
            return (False, f"Depositing USDC into vault wallet failed: {msg}")
        return (True, "USDC transferred to vault")

    async def _transfer_gas_to_vault(self) -> tuple[bool, str]:
        vault_gas = await self._get_gas_balance()
        min_gas_wei = int(self._gas_keep_wei())
        if vault_gas < min_gas_wei:
            needed_gas = (min_gas_wei - vault_gas) / 10**18
            (
                success,
                msg,
            ) = await self.balance_adapter.move_from_main_wallet_to_strategy_wallet(
                ETH_TOKEN_ID, needed_gas
            )
            if not success:
                return (False, f"Depositing gas into strategy wallet failed: {msg}")
        return (True, "Gas transferred to strategy")

    async def _sweep_token_balances(
        self,
        target_token_id: str,
        exclude: set[str] | None = None,
        min_usd_value: float = 1.0,
        *,
        strict: bool = False,
    ) -> tuple[bool, str]:
        if exclude is None:
            exclude = set()

        exclude.add(ETH_TOKEN_ID)
        exclude.add(target_token_id)

        tokens_to_check = [USDC_TOKEN_ID, WETH_TOKEN_ID, WSTETH_TOKEN_ID, WELL_TOKEN_ID]
        total_swept_usd = 0.0
        swept_count = 0

        for token_id in tokens_to_check:
            if token_id in exclude:
                continue

            balance = await self._get_balance_raw(
                token_id=token_id, wallet_address=self._get_strategy_wallet_address()
            )
            if balance <= 0:
                continue

            price, decimals = await self._get_token_data(token_id)
            usd_value = (balance / 10**decimals) * price

            if usd_value < min_usd_value:
                continue

            try:
                swap_result = await self._swap_with_retries(
                    from_token_id=token_id,
                    to_token_id=target_token_id,
                    amount=balance,
                )
                if swap_result:
                    total_swept_usd += usd_value
                    swept_count += 1
                    logger.info(
                        f"Swept {balance / 10**decimals:.6f} {token_id} "
                        f"(${usd_value:.2f}) to {target_token_id}"
                    )
                else:
                    msg = f"Failed to sweep {token_id} to {target_token_id}"
                    logger.warning(msg)
                    if strict:
                        return (False, msg)
            except SwapOutcomeUnknownError:
                raise
            except Exception as e:
                if strict:
                    return (False, f"Failed to sweep {token_id}: {e}")
                logger.warning(f"Failed to sweep {token_id}: {e}")

        if swept_count == 0:
            return (True, "No tokens to sweep")

        return (True, f"Swept {swept_count} tokens totaling ${total_swept_usd:.2f}")

    async def _claim_and_reinvest_rewards(self) -> tuple[bool, str]:
        claimed_ok, claimed = await self.moonwell_adapter.claim_rewards(
            min_rewards_usd=self.MIN_REWARD_CLAIM_USD
        )
        if not claimed_ok:
            logger.warning(f"Failed to claim rewards: {claimed}")
            return (True, "Reward claim failed, skipping reinvestment")

        if not claimed or not isinstance(claimed, dict):
            return (True, "No rewards to reinvest")

        well_balance = await self._get_balance_raw(
            token_id=WELL_TOKEN_ID,
            wallet_address=self._get_strategy_wallet_address(),
        )
        if well_balance <= 0:
            return (True, "No WELL balance to reinvest")

        well_price, well_decimals = await self._get_token_data(WELL_TOKEN_ID)
        well_value_usd = (well_balance / 10**well_decimals) * well_price

        if well_value_usd < self.MIN_REWARD_CLAIM_USD:
            logger.debug(
                f"WELL balance ${well_value_usd:.2f} below threshold, skipping swap"
            )
            return (True, f"WELL value ${well_value_usd:.2f} below threshold")

        logger.info(
            f"Swapping {well_balance / 10**well_decimals:.4f} WELL "
            f"(${well_value_usd:.2f}) to USDC"
        )
        try:
            swap_result = await self._swap_with_retries(
                from_token_id=WELL_TOKEN_ID,
                to_token_id=USDC_TOKEN_ID,
                amount=well_balance,
            )
            if not swap_result:
                logger.warning("Failed to swap WELL to USDC")
                return (True, "WELL swap failed, rewards left in wallet")
        except Exception as e:
            logger.warning(f"WELL swap error: {e}")
            return (True, f"WELL swap error: {e}")

        usdc_balance = await self._get_balance_raw(
            token_id=USDC_TOKEN_ID,
            wallet_address=self._get_strategy_wallet_address(),
        )
        if usdc_balance <= 0:
            return (True, "No USDC from reward swap")

        usdc_decimals = 6
        usdc_amount = usdc_balance / 10**usdc_decimals

        # Lend USDC directly to mUSDC (no leverage loop)
        logger.info(f"Lending {usdc_amount:.2f} USDC from rewards to mUSDC")
        lend_ok, lend_msg = await self.moonwell_adapter.lend(
            mtoken=M_USDC,
            amount=usdc_balance,
        )
        if not lend_ok:
            logger.warning(f"Failed to lend reward USDC: {lend_msg}")
            return (True, f"Reward USDC lend failed: {lend_msg}")

        return (True, f"Reinvested ${usdc_amount:.2f} USDC from WELL rewards")

    async def deposit(
        self, main_token_amount: float = 0.0, gas_token_amount: float = 0.0
    ) -> StatusTuple:
        self._clear_price_cache()

        if main_token_amount <= 0:
            return (False, "Deposit amount must be positive")

        success, message = await self._check_quote_profitability()
        if not success:
            return (False, message)

        success, message, validated_amount = await self._validate_usdc_deposit(
            main_token_amount
        )
        if not success:
            return (False, message)
        usdc_amount = validated_amount

        success, message = await self._validate_gas_balance()
        if not success:
            return (False, message)

        # Transfer gas to vault wallet first (if this fails, USDC stays in main wallet)
        success, message = await self._transfer_gas_to_vault()
        if not success:
            return (False, message)

        success, message = await self._transfer_usdc_to_vault(usdc_amount)
        if not success:
            return (False, message)

        return (
            True,
            f"Deposited {usdc_amount:.2f} USDC to strategy wallet. Call update() to deploy funds to Moonwell.",
        )

    async def _get_collateral_factors(self) -> tuple[float, float]:
        cf_u_result, cf_w_result = await asyncio.gather(
            self.moonwell_adapter.get_collateral_factor(mtoken=M_USDC),
            self.moonwell_adapter.get_collateral_factor(mtoken=M_WSTETH),
        )
        cf_u = cf_u_result[1] if cf_u_result[0] else 0.0
        cf_w = cf_w_result[1] if cf_w_result[0] else 0.0
        return cf_u, cf_w

    async def _get_current_leverage(
        self,
        snap: Optional["MoonwellWstethLoopStrategy.AccountingSnapshot"] = None,
        collateral_factors: tuple[float, float] | None = None,
    ) -> tuple[float, float, float]:
        if snap is None:
            snap, _ = await self._accounting_snapshot(
                collateral_factors=collateral_factors
            )

        usdc_key = f"Base_{M_USDC}"
        wsteth_key = f"Base_{M_WSTETH}"
        usdc_lend_value = float(snap.totals_usd.get(usdc_key, 0.0))
        wsteth_lend_value = float(snap.totals_usd.get(wsteth_key, 0.0))

        initial_leverage = (
            wsteth_lend_value / usdc_lend_value + 1 if usdc_lend_value else 0.0
        )

        return (usdc_lend_value, wsteth_lend_value, initial_leverage)

    async def _get_steth_apy(self) -> float | None:
        url = "https://eth-api.lido.fi/v1/protocol/steth/apr/sma"
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.get(url)
                r.raise_for_status()
                data = r.json()

            apy = data.get("data", {}).get("smaApr", None)
            if apy:
                return apy / 100
        except Exception as e:
            logger.warning(f"Failed to fetch stETH APY: {e}")
        return None

    async def _quote(self) -> dict:
        (
            usdc_apy_result,
            weth_apy_result,
            wsteth_apy,
            cf_u_result,
            cf_w_result,
        ) = await asyncio.gather(
            self.moonwell_adapter.get_apy(mtoken=M_USDC, apy_type="supply"),
            self.moonwell_adapter.get_apy(mtoken=M_WETH, apy_type="borrow"),
            self._get_steth_apy(),
            self.moonwell_adapter.get_collateral_factor(mtoken=M_USDC),
            self.moonwell_adapter.get_collateral_factor(mtoken=M_WSTETH),
        )

        usdc_lend_apy = usdc_apy_result[1] if usdc_apy_result[0] else 0.0
        weth_borrow_apy = weth_apy_result[1] if weth_apy_result[0] else 0.0
        wsteth_lend_apy = wsteth_apy or 0.0

        if not wsteth_lend_apy:
            return {
                "apy": 0,
                "information": "Failed to get Lido wstETH APY",
                "data": {},
            }

        cf_u = cf_u_result[1] if cf_u_result[0] else 0.0
        cf_w = cf_w_result[1] if cf_w_result[0] else 0.0

        if not cf_u or cf_u <= 0:
            return {"apy": 0, "information": "Invalid collateral factor", "data": {}}

        denominator = self.TARGET_HEALTH_FACTOR - cf_w
        if denominator <= 0:
            return {"apy": 0, "information": "Invalid health factor params", "data": {}}
        target_borrow = cf_u / denominator
        total_apy = target_borrow * (wsteth_lend_apy - weth_borrow_apy) + usdc_lend_apy
        total_leverage = target_borrow + 1

        return {
            "apy": total_apy,
            "information": f"Strategy would return {total_apy * 100:.2f}% APY with leverage of {total_leverage:.2f}x",
            "data": {
                "rates": {
                    "usdc_lend": usdc_lend_apy,
                    "wsteth_lend": wsteth_lend_apy,
                    "weth_borrow": weth_borrow_apy,
                },
                "leverage_achievable": total_leverage,
                "apy_achievable": total_apy,
            },
        }

    async def quote(self, deposit_amount: float | None = None) -> dict:
        result = await self._quote()
        apy = result.get("apy", 0.0)
        data = result.get("data", {})
        rates = data.get("rates", {})
        leverage = data.get("leverage_achievable", 1.0)

        return {
            "expected_apy": float(apy),
            "apy_type": "net",
            "confidence": "high" if apy > 0 else "low",
            "methodology": f"Leveraged wstETH loop at {leverage:.2f}x: wstETH yield minus WETH borrow cost, plus USDC supply yield",
            "components": {
                "usdc_supply_apy": rates.get("usdc_lend", 0.0),
                "wsteth_yield_apy": rates.get("wsteth_lend", 0.0),
                "weth_borrow_cost": rates.get("weth_borrow", 0.0),
            },
            "deposit_amount": deposit_amount,
            "as_of": datetime.now(UTC).isoformat(),
            "summary": result.get("information", f"Expected APY: {apy * 100:.2f}%"),
        }

    async def _atomic_deposit_iteration(self, borrow_amt_wei: int) -> int:
        safe_borrow_amt = int(borrow_amt_wei * COLLATERAL_SAFETY_FACTOR)
        strategy_address = self._get_strategy_wallet_address()

        # Snapshot balances so we can detect whether the borrow surfaced as native ETH or WETH.
        # (On Base, some integrations auto-unwrap borrowed WETH to native ETH.)
        eth_before, weth_before = await asyncio.gather(
            self._get_balance_raw(
                token_id=ETH_TOKEN_ID, wallet_address=strategy_address
            ),
            self._get_balance_raw(
                token_id=WETH_TOKEN_ID, wallet_address=strategy_address
            ),
        )

        # Step 1: Borrow (debt is WETH-denominated)
        success, borrow_result = await self.moonwell_adapter.borrow(
            mtoken=M_WETH, amount=safe_borrow_amt
        )
        if not success:
            raise Exception(f"Borrow failed: {borrow_result}")

        # On Base we wait +2 blocks by default; `confirmed_block_number` is safe to pin reads to.
        pinned_block = self._pinned_block(borrow_result)

        logger.info(
            f"Borrowed {safe_borrow_amt / 10**18:.6f} WETH (may arrive as ETH) "
            f"(pinned block {pinned_block})"
        )

        # Use block-pinned reads to check balances at the transaction's block
        # This avoids stale reads from RPC indexing lag on L2s like Base
        eth_after = int(eth_before)
        weth_after = int(weth_before)
        eth_delta = 0
        weth_delta = 0
        for attempt in range(5):
            eth_after, weth_after = await asyncio.gather(
                self._get_balance_raw(
                    token_id=ETH_TOKEN_ID,
                    wallet_address=strategy_address,
                    block_identifier=pinned_block,
                ),
                self._get_balance_raw(
                    token_id=WETH_TOKEN_ID,
                    wallet_address=strategy_address,
                    block_identifier=pinned_block,
                ),
            )
            eth_delta = max(0, int(eth_after) - int(eth_before))
            weth_delta = max(0, int(weth_after) - int(weth_before))
            if eth_delta > 0 or weth_delta > 0:
                break
            await asyncio.sleep(1 + attempt)

        gas_reserve = int(self._gas_keep_wei())
        # Usable ETH is the minimum of what we received (eth_delta) and what's available after gas reserve
        usable_eth = min(eth_delta, max(0, int(eth_after) - gas_reserve))

        logger.debug(
            f"Post-borrow balances: ETH delta={eth_delta / 10**18:.6f}, "
            f"WETH delta={weth_delta / 10**18:.6f}, usable_eth={usable_eth / 10**18:.6f}"
        )

        # Always swap WETH (not ETH directly) - ETH swaps get bad fills.
        # If borrow arrived as native ETH, wrap it first.
        weth_bal = int(weth_after)

        if eth_delta > 0 and usable_eth > 0:
            wrap_amt = min(int(safe_borrow_amt), int(usable_eth))
            logger.info(
                f"Borrow arrived as native ETH, wrapping {wrap_amt / 10**18:.6f} ETH to WETH"
            )
            wrap_success, wrap_msg = await self.moonwell_adapter.wrap_eth(
                amount=wrap_amt
            )
            if not wrap_success:
                raise Exception(f"Wrap ETH→WETH failed: {wrap_msg}")
            # WETH wrapping is 1:1, so we know exactly how much we have now
            # (avoids stale RPC reads after the wrap tx)
            weth_bal = int(weth_after) + wrap_amt
            logger.info(f"Post-wrap WETH balance (calculated): {weth_bal / 10**18:.6f}")
        elif weth_delta > 0:
            logger.info(f"Borrow arrived as WETH: {weth_delta / 10**18:.6f}")
        elif eth_delta == 0 and weth_delta == 0:
            # Borrow succeeded but balance reads are stale - assume it arrived as ETH
            # and try to wrap what we can (this is common on Base L2)
            available_eth = max(0, int(eth_after) - gas_reserve)
            if available_eth > 0:
                wrap_amt = min(int(safe_borrow_amt), available_eth)
                logger.warning(
                    f"Balance delta not detected but borrow succeeded. "
                    f"Assuming ETH arrival, wrapping {wrap_amt / 10**18:.6f} ETH"
                )
                wrap_success, wrap_msg = await self.moonwell_adapter.wrap_eth(
                    amount=wrap_amt
                )
                if not wrap_success:
                    raise Exception(f"Wrap ETH→WETH failed: {wrap_msg}")
                # WETH wrapping is 1:1, so we know exactly how much we have now
                weth_bal = int(weth_after) + wrap_amt
                logger.info(
                    f"Post-wrap WETH balance (calculated): {weth_bal / 10**18:.6f}"
                )

        amount_to_swap = min(int(safe_borrow_amt), int(weth_bal))

        if amount_to_swap <= 0:
            raise Exception(
                f"No WETH available to swap after borrowing (weth_bal={weth_bal})"
            )

        # Step 2: Swap WETH to wstETH with retries
        # Prefer enso/aerodrome for WETH→wstETH - LiFi gets bad fills
        swap_result = await self._swap_with_retries(
            from_token_id=WETH_TOKEN_ID,
            to_token_id=WSTETH_TOKEN_ID,
            amount=amount_to_swap,
            preferred_providers=["aerodrome", "enso"],
        )
        if swap_result is None:
            # Roll back: repay the borrowed amount to remain delta-neutral.
            try:
                # Prefer repaying directly with the borrowed WETH.
                weth_bal = await self._get_balance_raw(
                    token_id=WETH_TOKEN_ID, wallet_address=strategy_address
                )

                wrap_amt = 0
                if weth_bal < safe_borrow_amt:
                    # If the borrow surfaced as native ETH (or WETH was otherwise reduced),
                    # attempt to wrap ETH for the shortfall while preserving gas.
                    eth_bal = await self._get_balance_raw(
                        token_id=ETH_TOKEN_ID, wallet_address=strategy_address
                    )
                    gas_reserve = int(self._gas_keep_wei())
                    available_for_wrap = max(0, eth_bal - gas_reserve)
                    shortfall = safe_borrow_amt - weth_bal
                    wrap_amt = min(shortfall, available_for_wrap)
                    if wrap_amt > 0:
                        wrap_success, wrap_msg = await self.moonwell_adapter.wrap_eth(
                            amount=wrap_amt
                        )
                        if not wrap_success:
                            raise Exception(f"Wrap ETH→WETH failed: {wrap_msg}")

                        weth_bal = await self._get_balance_raw(
                            token_id=WETH_TOKEN_ID, wallet_address=strategy_address
                        )

                repay_amt = min(safe_borrow_amt, weth_bal)
                if repay_amt <= 0:
                    raise Exception("No WETH available to repay the borrow")

                repay_success, repay_msg = await self.moonwell_adapter.repay(
                    mtoken=M_WETH,
                    underlying_token=WETH,
                    amount=repay_amt,
                )
                if not repay_success:
                    raise Exception(f"Repay failed: {repay_msg}")

                if repay_amt < safe_borrow_amt:
                    logger.warning(
                        f"Swap failed; only repaid {repay_amt / 10**18:.6f} of "
                        f"{safe_borrow_amt / 10**18:.6f} WETH. Position may be imbalanced."
                    )
                else:
                    logger.warning("Swap failed after retries. Borrow undone.")
            except Exception as repay_exc:
                raise Exception(
                    f"Swap failed after retries and reverting borrow failed: {repay_exc}. "
                    "Position may no longer be delta-neutral!"
                ) from repay_exc
            raise Exception("Atomic deposit failed at swap step after all retries")

        raw_to_amount = (
            swap_result.get("to_amount", 0) if isinstance(swap_result, dict) else 0
        )
        try:
            to_amount_wei = int(raw_to_amount) if raw_to_amount else 0
        except (ValueError, TypeError):
            to_amount_wei = 0

        wsteth_success, wsteth_bal_raw = await self.balance_adapter.get_balance(
            token_id=WSTETH_TOKEN_ID, wallet_address=self._get_strategy_wallet_address()
        )
        if not wsteth_success:
            raise Exception("Failed to get wstETH balance after swap")
        wsteth_bal = self._parse_balance(wsteth_bal_raw)

        # Use the smaller of balance check and swap result to avoid over-lending
        lend_amt_wei = (
            min(to_amount_wei, wsteth_bal) if wsteth_bal > 0 else to_amount_wei
        )

        if lend_amt_wei <= 0:
            logger.warning("Swap resulted in 0 wstETH. Rolling back borrow...")
            try:
                weth_bal = await self._get_balance_raw(
                    token_id=WETH_TOKEN_ID, wallet_address=strategy_address
                )
                if weth_bal > 0:
                    repay_amt = min(weth_bal, safe_borrow_amt)
                    await self.moonwell_adapter.repay(
                        mtoken=M_WETH,
                        underlying_token=WETH,
                        amount=repay_amt,
                    )
                    logger.info(f"Rolled back: repaid {repay_amt / 10**18:.6f} WETH")
            except Exception as rollback_exc:
                raise Exception(
                    f"Swap produced 0 wstETH and rollback failed: {rollback_exc}. "
                    "Position may have excess WETH debt!"
                ) from rollback_exc
            raise Exception(
                "Swap resulted in 0 wstETH to lend. Borrow was rolled back."
            )

        # Step 3: Lend wstETH
        mwsteth_before = 0
        minted_mwsteth = 0
        try:
            mwsteth_pos_before = await self.moonwell_adapter.get_pos(mtoken=M_WSTETH)
            if mwsteth_pos_before[0] and isinstance(mwsteth_pos_before[1], dict):
                mwsteth_before = int(
                    (mwsteth_pos_before[1] or {}).get("mtoken_balance", 0) or 0
                )
        except Exception:  # noqa: BLE001
            mwsteth_before = 0

        try:
            success, msg = await self.moonwell_adapter.lend(
                mtoken=M_WSTETH,
                underlying_token=WSTETH,
                amount=lend_amt_wei,
            )
            if not success:
                raise Exception(f"Lend failed: {msg}")

            # Track minted mTokens so we can redeem the correct amount on rollback.
            try:
                mwsteth_pos_after = await self.moonwell_adapter.get_pos(mtoken=M_WSTETH)
                if mwsteth_pos_after[0] and isinstance(mwsteth_pos_after[1], dict):
                    mwsteth_after = int(
                        (mwsteth_pos_after[1] or {}).get("mtoken_balance", 0) or 0
                    )
                    minted_mwsteth = max(0, int(mwsteth_after) - int(mwsteth_before))
            except Exception:  # noqa: BLE001
                minted_mwsteth = 0

            set_coll_success, set_coll_msg = await self.moonwell_adapter.set_collateral(
                mtoken=M_WSTETH
            )
            if not set_coll_success:
                # Must redeem mTokens (not underlying) since wstETH is now in protocol, not wallet.
                to_redeem = minted_mwsteth
                if to_redeem <= 0:
                    # Fallback: redeem whatever balance we can see (best-effort).
                    mwsteth_pos = await self.moonwell_adapter.get_pos(mtoken=M_WSTETH)
                    if mwsteth_pos[0] and isinstance(mwsteth_pos[1], dict):
                        to_redeem = int(
                            (mwsteth_pos[1] or {}).get("mtoken_balance", 0) or 0
                        )
                if to_redeem > 0:
                    await self.moonwell_adapter.unlend(
                        mtoken=M_WSTETH, amount=to_redeem
                    )
                raise Exception(
                    f"set_collateral failed: {set_coll_msg}. Lend reversed."
                )
            logger.info(f"Lent {lend_amt_wei / 10**18:.6f} wstETH")

        except Exception as lend_exc:
            # Roll back: swap wstETH back to WETH and repay (only if we have wstETH)
            try:
                # Ensure wstETH is in the wallet (redeem minted mwstETH if needed).
                rollback_wsteth = await self._get_balance_raw(
                    token_id=WSTETH_TOKEN_ID, wallet_address=strategy_address
                )
                if rollback_wsteth <= 0 and minted_mwsteth > 0:
                    await self.moonwell_adapter.unlend(
                        mtoken=M_WSTETH, amount=minted_mwsteth
                    )
                    rollback_wsteth = await self._get_balance_raw(
                        token_id=WSTETH_TOKEN_ID, wallet_address=strategy_address
                    )

                if rollback_wsteth > 0:
                    (
                        revert_success,
                        revert_result,
                    ) = await self.brap_adapter.swap_from_token_ids(
                        from_token_id=WSTETH_TOKEN_ID,
                        to_token_id=WETH_TOKEN_ID,
                        from_address=strategy_address,
                        amount=str(rollback_wsteth),
                    )
                    if revert_success and revert_result:
                        weth_after = await self._get_balance_raw(
                            token_id=WETH_TOKEN_ID, wallet_address=strategy_address
                        )
                        repay_amt = min(weth_after, safe_borrow_amt)
                        if repay_amt > 0:
                            await self.moonwell_adapter.repay(
                                mtoken=M_WETH,
                                underlying_token=WETH,
                                amount=repay_amt,
                            )
                else:
                    logger.warning(
                        f"Lend failed but no wstETH to rollback. Lend error: {lend_exc}"
                    )
            except Exception as revert_exc:
                raise Exception(
                    f"Lend failed: {lend_exc} and revert failed: {revert_exc}"
                ) from revert_exc
            raise Exception(
                f"Deposit to wstETH failed and was reverted: {lend_exc}"
            ) from lend_exc

        return lend_amt_wei

    async def partial_liquidate(self, usd_value: float) -> StatusTuple:
        if usd_value <= 0:
            raise ValueError(f"usd_value must be positive, got {usd_value}")

        strategy_address = self._get_strategy_wallet_address()

        usdc_info = await self._get_token_info(USDC_TOKEN_ID)
        usdc_decimals = usdc_info.get("decimals", 6)

        # (1) Check current USDC in wallet
        usdc_raw = await self._get_usdc_balance()
        current_usdc = usdc_raw / (10**usdc_decimals)
        if current_usdc >= usd_value:
            target_raw = int(usd_value * (10**usdc_decimals))
            available = min(usdc_raw, target_raw) / (10**usdc_decimals)
            return (
                True,
                f"Partial liquidation not needed. Available: {available:.2f} USDC",
            )

        missing_usd = float(usd_value - current_usdc)

        collateral_factors = await self._get_collateral_factors()
        snap, _ = await self._accounting_snapshot(collateral_factors=collateral_factors)

        key_wsteth = f"Base_{M_WSTETH}"
        key_usdc = f"Base_{M_USDC}"

        wsteth_usd = float(snap.totals_usd.get(key_wsteth, 0.0))
        weth_debt_usd = float(snap.debt_usd)

        # (2a) Prefer withdrawing wstETH first if we're meaningfully long (collateral > debt).
        if missing_usd > 0 and wsteth_usd > weth_debt_usd:
            max_delta_unwind = max(0.0, wsteth_usd - weth_debt_usd)
            desired_unlend_usd = min(float(missing_usd), float(max_delta_unwind))

            safe_unlend_usd = self._max_safe_withdraw_usd(
                totals_usd=snap.totals_usd,
                withdraw_key=key_wsteth,
                collateral_factors=collateral_factors,
                hf_floor=float(self.MIN_HEALTH_FACTOR),
            )
            unlend_usd = min(float(desired_unlend_usd), float(safe_unlend_usd))

            if unlend_usd >= float(self.min_withdraw_usd):
                if not snap.wsteth_price or snap.wsteth_price <= 0:
                    return (False, "Invalid wstETH price")

                unlend_underlying_raw = (
                    int(unlend_usd / snap.wsteth_price * 10**snap.wsteth_dec) + 1
                )

                mwsteth_res = await self.moonwell_adapter.max_withdrawable_mtoken(
                    mtoken=M_WSTETH
                )
                if not mwsteth_res[0]:
                    return (
                        False,
                        f"Failed to compute withdrawable mwstETH: {mwsteth_res[1]}",
                    )
                withdraw_info = mwsteth_res[1]
                if not isinstance(withdraw_info, dict):
                    return (False, f"Bad withdraw info for mwstETH: {withdraw_info}")

                mtoken_amt = self._mtoken_amount_for_underlying(
                    withdraw_info, unlend_underlying_raw
                )
                if mtoken_amt > 0:
                    success, msg = await self.moonwell_adapter.unlend(
                        mtoken=M_WSTETH, amount=mtoken_amt
                    )
                    if not success:
                        return (
                            False,
                            f"Failed to redeem mwstETH for partial liquidation: {msg}",
                        )

                    pinned_block = self._pinned_block(msg)
                    wsteth_wallet_raw = await self._balance_after_tx(
                        token_id=WSTETH_TOKEN_ID,
                        wallet=strategy_address,
                        pinned_block=pinned_block,
                        min_expected=1,
                        attempts=5,
                    )
                    amount_to_swap = min(
                        int(wsteth_wallet_raw), int(unlend_underlying_raw)
                    )
                    if amount_to_swap > 0:
                        swap_res = await self._swap_with_retries(
                            from_token_id=WSTETH_TOKEN_ID,
                            to_token_id=USDC_TOKEN_ID,
                            amount=amount_to_swap,
                        )
                        if swap_res is None:
                            restore_amt = min(
                                int(wsteth_wallet_raw), int(amount_to_swap)
                            )
                            if restore_amt > 0:
                                await self.moonwell_adapter.lend(
                                    mtoken=M_WSTETH,
                                    underlying_token=WSTETH,
                                    amount=restore_amt,
                                )
                                await self.moonwell_adapter.set_collateral(
                                    mtoken=M_WSTETH
                                )

        # (3) Re-check wallet USDC balance
        usdc_raw = await self._get_balance_raw(
            token_id=USDC_TOKEN_ID, wallet_address=strategy_address
        )
        current_usdc = usdc_raw / (10**usdc_decimals)

        # (4) If still short, redeem USDC collateral directly
        if current_usdc < usd_value:
            snap, _ = await self._accounting_snapshot(
                collateral_factors=collateral_factors
            )

            missing_usdc = float(usd_value - current_usdc)
            available_usdc_usd = float(snap.totals_usd.get(key_usdc, 0.0))

            if missing_usdc > 0 and available_usdc_usd > 0:
                safe_unlend_usd = self._max_safe_withdraw_usd(
                    totals_usd=snap.totals_usd,
                    withdraw_key=key_usdc,
                    collateral_factors=collateral_factors,
                    hf_floor=float(self.MIN_HEALTH_FACTOR),
                )
                desired_unlend_usd = min(
                    float(missing_usdc),
                    float(available_usdc_usd),
                    float(safe_unlend_usd),
                )
                if desired_unlend_usd >= float(self.min_withdraw_usd):
                    if snap.usdc_price and snap.usdc_price > 0:
                        unlend_underlying_raw = (
                            int(
                                desired_unlend_usd / snap.usdc_price * 10**snap.usdc_dec
                            )
                            + 1
                        )
                    else:
                        unlend_underlying_raw = int(
                            desired_unlend_usd * (10**usdc_decimals)
                        )

                    musdc_res = await self.moonwell_adapter.max_withdrawable_mtoken(
                        mtoken=M_USDC
                    )
                    if not musdc_res[0]:
                        return (
                            False,
                            f"Failed to compute withdrawable mUSDC: {musdc_res[1]}",
                        )
                    withdraw_info = musdc_res[1]
                    if not isinstance(withdraw_info, dict):
                        return (False, f"Bad withdraw info for mUSDC: {withdraw_info}")

                    mtoken_amt = self._mtoken_amount_for_underlying(
                        withdraw_info, unlend_underlying_raw
                    )
                    if mtoken_amt > 0:
                        success, msg = await self.moonwell_adapter.unlend(
                            mtoken=M_USDC, amount=mtoken_amt
                        )
                        if not success:
                            return (
                                False,
                                f"Failed to redeem mUSDC for partial liquidation: {msg}",
                            )

        # (5) Final available USDC (capped to target)
        usdc_raw = await self._get_balance_raw(
            token_id=USDC_TOKEN_ID, wallet_address=strategy_address
        )
        target_raw = int(usd_value * (10**usdc_decimals))
        final_raw = min(usdc_raw, target_raw)

        if final_raw <= 0:
            return (False, "Partial liquidation produced no USDC")

        final_usdc = final_raw / (10**usdc_decimals)
        if final_raw < target_raw:
            return (
                True,
                f"Partial liquidation completed. Available: {final_usdc:.2f} USDC (requested {usd_value:.2f})",
            )
        return (
            True,
            f"Partial liquidation completed. Available: {final_usdc:.2f} USDC",
        )

    async def _execute_deposit_loop(self, usdc_amount: float) -> tuple[bool, Any, int]:
        token_info = await self._get_token_info(USDC_TOKEN_ID)
        decimals = token_info.get("decimals", 6)
        initial_deposit = int(usdc_amount * 10**decimals)

        wsteth_price, weth_price, collateral_factors = await asyncio.gather(
            self._get_token_price(WSTETH_TOKEN_ID),
            self._get_token_price(WETH_TOKEN_ID),
            self._get_collateral_factors(),
        )

        weth_pos = await self.moonwell_adapter.get_pos(mtoken=M_WETH)

        current_borrowed_value = 0.0
        if weth_pos[0]:
            borrow_bal = weth_pos[1].get("borrow_balance", 0)
            current_borrowed_value = (borrow_bal / 10**18) * weth_price

        success, msg = await self.moonwell_adapter.lend(
            mtoken=M_USDC,
            underlying_token=USDC,
            amount=initial_deposit,
        )
        if not success:
            return (False, f"Initial USDC lend failed: {msg}", 0)

        await self.moonwell_adapter.set_collateral(mtoken=M_USDC)
        logger.info(f"Deposited {usdc_amount:.2f} USDC as initial collateral")

        # Get current leverage (positions changed after lend, must re-fetch)
        (
            usdc_lend_value,
            wsteth_lend_value,
            initial_leverage,
        ) = await self._get_current_leverage()

        return await self._loop_wsteth(
            wsteth_price=wsteth_price,
            weth_price=weth_price,
            current_borrowed_value=current_borrowed_value,
            initial_leverage=initial_leverage,
            usdc_lend_value=usdc_lend_value,
            wsteth_lend_value=wsteth_lend_value,
            collateral_factors=collateral_factors,
        )

    async def _loop_wsteth(
        self,
        wsteth_price: float,
        weth_price: float,
        current_borrowed_value: float,
        initial_leverage: float,
        usdc_lend_value: float,
        wsteth_lend_value: float,
        collateral_factors: tuple[float, float] | None = None,
    ) -> tuple[bool, Any, int]:
        # Ensure USDC and wstETH markets are entered as collateral before borrowing
        # This is idempotent - if already entered, Moonwell just returns success
        if usdc_lend_value > 0:
            set_coll_result = await self.moonwell_adapter.set_collateral(mtoken=M_USDC)
            if not set_coll_result[0]:
                logger.warning(
                    f"Failed to ensure USDC collateral: {set_coll_result[1]}"
                )
                return (
                    False,
                    f"Failed to enable USDC as collateral: {set_coll_result[1]}",
                    0,
                )

        if wsteth_lend_value > 0:
            set_coll_result = await self.moonwell_adapter.set_collateral(
                mtoken=M_WSTETH
            )
            if not set_coll_result[0]:
                logger.warning(
                    f"Failed to ensure wstETH collateral: {set_coll_result[1]}"
                )
                # This is less critical - we can continue if wstETH collateral fails

        # Enter M_WETH market to allow borrowing from it
        # In Compound v2/Moonwell, you must be in a market to borrow from it
        # (enterMarkets enables both collateral usage AND borrowing)
        set_weth_result = await self.moonwell_adapter.set_collateral(mtoken=M_WETH)
        if not set_weth_result[0]:
            logger.warning(f"Failed to enter M_WETH market: {set_weth_result[1]}")
            return (
                False,
                f"Failed to enter M_WETH market for borrowing: {set_weth_result[1]}",
                0,
            )
        logger.info("Entered M_WETH market to enable borrowing")

        if collateral_factors is not None:
            cf_u, cf_w = collateral_factors
        else:
            cf_u, cf_w = await self._get_collateral_factors()

        max_safe_f = self._max_safe_F(cf_w)

        denominator = self.TARGET_HEALTH_FACTOR + 0.001 - cf_w
        if denominator <= 0:
            logger.warning(
                f"Cannot calculate target borrow: cf_w ({cf_w:.3f}) >= TARGET_HF ({self.TARGET_HEALTH_FACTOR})"
            )
            return (False, initial_leverage, -1)

        target_borrow_value = (
            usdc_lend_value * cf_u / denominator - current_borrowed_value
        )

        if target_borrow_value < 0:
            return (False, initial_leverage, -1)

        # Track wstETH added THIS session (starts at 0), not total position
        session_wsteth_lend_value = 0.0
        total_wsteth_lend_value = wsteth_lend_value
        raw_leverage_limit = (
            (current_borrowed_value + target_borrow_value) / usdc_lend_value + 1
            if usdc_lend_value
            else 0
        )

        # Apply depeg-aware leverage cap
        max_safe_leverage = max_safe_f * usdc_lend_value + 1 if usdc_lend_value else 0
        leverage_limit = min(raw_leverage_limit, max_safe_leverage, self.leverage_limit)

        leverage_tracker: list[float] = [initial_leverage]

        for i in range(self._MAX_LOOP_LIMIT):
            borrowable_result = await self.moonwell_adapter.get_borrowable_amount()
            if not borrowable_result[0]:
                logger.warning("Failed to get borrowable amount")
                break

            if not weth_price or weth_price <= 0:
                logger.warning("Invalid WETH price; breaking loop")
                break

            borrowable_usd = self._normalize_usd_value(borrowable_result[1])
            if borrowable_usd <= self.min_withdraw_usd:
                logger.info("No additional borrowing possible; breaking loop")
                break

            weth_info = await self._get_token_info(WETH_TOKEN_ID)
            weth_decimals = weth_info.get("decimals", 18)
            max_borrow_wei = int(borrowable_usd / weth_price * 10**weth_decimals)

            # remaining_value is how much more we need to borrow/lend THIS session
            remaining_value = target_borrow_value - session_wsteth_lend_value
            remaining_wei = int(remaining_value / weth_price * 10**weth_decimals) + 1

            if remaining_value < 2:
                logger.info(
                    f"Target reached: borrowed/lent ${session_wsteth_lend_value:.2f} of ${target_borrow_value:.2f} target"
                )
                break

            # Scale up for swap slippage
            optimal_this_iter = int(remaining_wei / (1 - 0.005))
            borrow_amt_wei = min(optimal_this_iter, max_borrow_wei)

            current_leverage = leverage_tracker[-1]
            logger.info(
                f"Current leverage {current_leverage:.2f}x. "
                f"Borrowing {borrow_amt_wei / 10**weth_decimals:.6f} WETH"
            )

            try:
                lend_amt_wei = await self._atomic_deposit_iteration(borrow_amt_wei)
            except Exception as e:
                logger.error(f"Deposit iteration aborted: {e}")
                return (False, f"deposit iteration {i + 1} failed: {e}", i)

            wsteth_info = await self._get_token_info(WSTETH_TOKEN_ID)
            wsteth_decimals = wsteth_info.get("decimals", 18)

            lend_value_this_iter = wsteth_price * lend_amt_wei / 10**wsteth_decimals
            session_wsteth_lend_value += lend_value_this_iter
            total_wsteth_lend_value += lend_value_this_iter
            leverage_tracker.append(total_wsteth_lend_value / usdc_lend_value + 1)

            # Stop if max leverage or marginal gain < threshold (diminishing returns vs gas cost)
            if (leverage_tracker[-1] > leverage_limit) or (
                len(leverage_tracker) > 1
                and leverage_tracker[-1] / leverage_tracker[-2] - 1
                < self._MIN_LEVERAGE_GAIN_BPS
            ):
                logger.info(
                    f"Finished loop, final leverage: {leverage_tracker[-1]:.2f}"
                )
                break

        if len(leverage_tracker) == 1:
            return (False, leverage_tracker[-1], 0)

        return (True, leverage_tracker[-1], len(leverage_tracker) - 1)

    async def update(self) -> StatusTuple:
        logger.info("")
        logger.info("*" * 60)
        logger.info("* MOONWELL STRATEGY UPDATE CALLED")
        logger.info("*" * 60)
        self._clear_price_cache()

        status: StatusTuple = (False, "Unknown")
        err: Exception | None = None

        try:
            status = await self._update_impl()
        except Exception as exc:
            err = exc
            if isinstance(exc, SwapOutcomeUnknownError):
                status = (False, f"Swap outcome unknown: {exc}")
            else:
                status = (False, f"Update failed: {exc}")

        guard_ok, guard_msg = await self._post_run_guard(
            mode="operate", prior_error=err
        )
        if not guard_ok:
            return (
                False,
                f"{status[1]} | finalizer FAILED: {guard_msg}",
            )
        return (
            status[0],
            f"{status[1]} | finalizer: {guard_msg}",
        )

    async def _update_impl(self) -> StatusTuple:
        logger.info("=" * 60)
        logger.info("UPDATE START")
        logger.info("=" * 60)

        gas_amt = await self._get_gas_balance()
        logger.info(
            f"Gas balance: {gas_amt / 10**18:.6f} ETH (min: {self.MAINTENANCE_GAS} ETH)"
        )
        if gas_amt < int(self.MAINTENANCE_GAS * 10**18):
            logger.warning(
                f"Low gas: {gas_amt / 10**18:.6f} < {self.MAINTENANCE_GAS} ETH. "
                f"Transactions may fail. Call deposit() with gas to top up."
            )

        # Pre-fetch collateral factors once (saves RPC + makes decisions consistent)
        collateral_factors = await self._get_collateral_factors()

        snap, _ = await self._accounting_snapshot(collateral_factors=collateral_factors)

        logger.info("-" * 40)
        logger.info("CURRENT STATE:")
        logger.info(f"  Health Factor: {snap.hf:.3f}")
        logger.info(
            f"  Wallet: ETH={snap.wallet_eth / 10**18:.4f}, WETH={snap.wallet_weth / 10**18:.4f}, USDC={snap.wallet_usdc / 10**6:.2f}"
        )
        logger.info(
            f"  Supplied: USDC=${snap.usdc_supplied / 10**6 * snap.usdc_price:.2f}, wstETH=${snap.wsteth_supplied / 10**18 * snap.wsteth_price:.2f}"
        )
        logger.info(f"  Debt: WETH=${snap.weth_debt / 10**18 * snap.weth_price:.2f}")
        logger.info(f"  Net equity: ${snap.net_equity_usd:.2f}")
        logger.info(f"  Borrow capacity: ${snap.capacity_usd:.2f}")
        logger.info("-" * 40)

        ok, msg = await self._ensure_markets_for_state(snap)
        if not ok:
            return (False, f"Failed ensuring markets: {msg}")

        # 1) Reconcile wallet leftovers into the intended position
        logger.info("STEP 1: Reconciling wallet leftovers into position...")
        try:
            ok, msg = await self._reconcile_wallet_into_position(
                collateral_factors=collateral_factors,
                max_batch_usd=8000.0,
            )
            if not ok:
                return (False, msg)
        except SwapOutcomeUnknownError as exc:
            return (False, f"Swap outcome unknown during wallet reconciliation: {exc}")
        except Exception as exc:
            return (False, f"Failed during wallet reconciliation: {exc}")

        # 2) Refresh snapshot and keep HF near TARGET_HEALTH_FACTOR (deleverage if too low)
        snap, _ = await self._accounting_snapshot(collateral_factors=collateral_factors)
        logger.info(f"STEP 2: Check HF thresholds (current HF={snap.hf:.3f})")

        emergency_hf_floor = float(self.LEVERAGE_DELEVER_HF_FLOOR)
        if snap.hf < emergency_hf_floor:
            logger.warning(
                f"EMERGENCY: HF {snap.hf:.3f} < floor {emergency_hf_floor:.2f} - depositing USDC to raise HF"
            )
            # Emergency: raise collateral without touching debt/collateral withdrawals.
            # Sweep whatever wallet assets we can into USDC, then lend it as collateral.
            try:
                ok, sweep_msg = await self._sweep_token_balances(
                    target_token_id=USDC_TOKEN_ID,
                    exclude={ETH_TOKEN_ID},
                    min_usd_value=float(self.sweep_min_usd),
                )
            except SwapOutcomeUnknownError as exc:
                return (False, f"Swap outcome unknown during emergency sweep: {exc}")

            if not ok:
                return (False, f"Emergency sweep failed: {sweep_msg}")

            addr = self._get_strategy_wallet_address()
            usdc_bal = await self._get_balance_raw(
                token_id=USDC_TOKEN_ID, wallet_address=addr
            )
            if usdc_bal <= 0:
                return (
                    False,
                    f"HF={snap.hf:.3f} below emergency floor ({emergency_hf_floor:.2f}); "
                    "no USDC available to deposit after sweep",
                )

            lend_ok, lend_res = await self.moonwell_adapter.lend(
                mtoken=M_USDC, underlying_token=USDC, amount=int(usdc_bal)
            )
            if not lend_ok:
                return (False, f"Emergency USDC deposit failed: {lend_res}")

            # Ensure USDC market is entered as collateral (idempotent).
            await self.moonwell_adapter.set_collateral(mtoken=M_USDC)

            return (
                True,
                f"HF={snap.hf:.3f} below emergency floor ({emergency_hf_floor:.2f}); "
                f"swept+deposited {usdc_bal / 10**snap.usdc_dec:.2f} USDC to improve HF "
                f"({sweep_msg})",
            )

        target_hf = float(self.TARGET_HEALTH_FACTOR)
        deleverage_threshold = max(
            float(self.MIN_HEALTH_FACTOR),
            float(target_hf) - float(self.HF_DELEVERAGE_BUFFER),
        )
        logger.info(
            f"  Target HF={target_hf:.2f}, Deleverage threshold={deleverage_threshold:.2f}"
        )
        if snap.hf < deleverage_threshold:
            logger.info(
                f"DELEVERAGE: HF {snap.hf:.3f} < threshold {deleverage_threshold:.2f} - reducing debt"
            )
            if snap.capacity_usd <= 0:
                return (
                    False,
                    "No borrow capacity found; cannot compute deleverage target",
                )
            try:
                ok, msg = await self._settle_weth_debt_to_target_usd(
                    target_debt_usd=0.0,
                    target_hf=float(target_hf),
                    collateral_factors=collateral_factors,
                    mode="operate",
                    max_batch_usd=3000.0,
                )
            except SwapOutcomeUnknownError as exc:
                return (False, f"Swap outcome unknown during deleverage: {exc}")
            if not ok:
                return (False, f"Deleverage failed: {msg}")
            snap, _ = await self._accounting_snapshot(
                collateral_factors=collateral_factors
            )

        # If leverage drifted above target (by > LEVERAGE_DELEVERAGE_BUFFER), reduce it by
        # withdrawing wstETH collateral and applying it to the WETH borrow.
        target_leverage = self._target_leverage(
            collateral_factors=collateral_factors,
            target_hf=float(target_hf),
        )
        usdc_key = f"Base_{M_USDC}"
        wsteth_key = f"Base_{M_WSTETH}"
        usdc_lend_value = float(snap.totals_usd.get(usdc_key, 0.0))
        wsteth_lend_value = float(snap.totals_usd.get(wsteth_key, 0.0))
        current_leverage = (
            wsteth_lend_value / usdc_lend_value + 1.0 if usdc_lend_value > 0 else 0.0
        )
        logger.info(
            f"STEP 3: Check leverage (current={current_leverage:.2f}x, target={target_leverage:.2f}x)"
        )
        if target_leverage > 0 and current_leverage > target_leverage + float(
            self.LEVERAGE_DELEVERAGE_BUFFER
        ):
            logger.info(
                f"DELEVER: Leverage {current_leverage:.2f}x > target+buffer {target_leverage + self.LEVERAGE_DELEVERAGE_BUFFER:.2f}x"
            )
            try:
                ok, msg = await self._delever_wsteth_to_target_leverage(
                    target_leverage=float(target_leverage),
                    collateral_factors=collateral_factors,
                    max_over_leverage=float(self.LEVERAGE_DELEVERAGE_BUFFER),
                    max_batch_usd=3000.0,
                    max_steps=10,
                )
            except SwapOutcomeUnknownError as exc:
                return (False, f"Swap outcome unknown during leverage delever: {exc}")
            if not ok:
                return (False, msg)

            snap, _ = await self._accounting_snapshot(
                collateral_factors=collateral_factors
            )

        totals_usd = snap.totals_usd
        hf = snap.hf

        # Claim WELL rewards, swap to USDC, and lend directly (no leverage)
        logger.info("STEP 4: Claim and reinvest WELL rewards...")
        reward_ok, reward_msg = await self._claim_and_reinvest_rewards()
        logger.info(f"  Rewards: {reward_msg}")

        success, msg = await self._check_quote_profitability()
        if not success:
            return (False, msg)

        usdc_balance_wei = await self._get_usdc_balance()
        token_info = await self._get_token_info(USDC_TOKEN_ID)
        decimals = token_info.get("decimals", 6)
        usdc_balance = usdc_balance_wei / 10**decimals

        usdc_key = f"Base_{M_USDC}"
        wsteth_key = f"Base_{M_WSTETH}"
        usdc_lend_value = totals_usd.get(usdc_key, 0)
        wsteth_lend_value = totals_usd.get(wsteth_key, 0)
        initial_leverage = (
            wsteth_lend_value / usdc_lend_value + 1 if usdc_lend_value else 0
        )

        logger.info("STEP 5: Check for USDC to deploy...")
        logger.info(
            f"  Wallet USDC: {usdc_balance:.2f}, Min deposit: {self.MIN_USDC_DEPOSIT}"
        )
        logger.info(f"  Current leverage: {initial_leverage:.2f}x, HF: {hf:.3f}")

        # If we have meaningful USDC in-wallet, redeploy it regardless of current HF.
        if usdc_balance >= self.MIN_USDC_DEPOSIT:
            logger.info(
                f"REDEPLOY: Deploying {usdc_balance:.2f} USDC into leverage loop"
            )
            success, final_leverage, n_loops = await self._execute_deposit_loop(
                usdc_balance
            )
            if not success:
                return (
                    False,
                    f"Redeploy loop failed: {final_leverage} after {n_loops} successful loops",
                )
            return (
                True,
                f"Redeployed {usdc_balance:.2f} USDC to {final_leverage:.2f}x with {n_loops} loops",
            )

        # Lever-up when HF is significantly above target (TARGET_HEALTH_FACTOR).
        lever_up_threshold = float(target_hf) + float(self.HF_LEVER_UP_BUFFER)
        logger.info(
            f"STEP 6: Check lever-up (HF={hf:.3f}, threshold={lever_up_threshold:.2f})"
        )
        if hf <= lever_up_threshold:
            logger.info(
                f"NO ACTION: HF {hf:.3f} <= lever-up threshold {lever_up_threshold:.2f}"
            )
            logger.info("=" * 60)
            return (
                True,
                f"HF={hf:.3f} <= lever-up threshold({lever_up_threshold:.2f}); no action needed.",
            )

        # Use 95% threshold to handle rounding/slippage from deposit
        min_lend_threshold = self.MIN_USDC_DEPOSIT * 0.95
        if (
            usdc_balance < self.MIN_USDC_DEPOSIT
            and usdc_lend_value < min_lend_threshold
        ):
            return (
                False,
                f"No USDC lent ({usdc_lend_value:.2f}) and not enough in wallet ({usdc_balance:.2f}). Deposit funds.",
            )

        if usdc_balance < self.MIN_USDC_DEPOSIT:
            # Lever-up path - use pre-fetched data
            wsteth_price = await self._get_token_price(WSTETH_TOKEN_ID)
            weth_price = await self._get_token_price(WETH_TOKEN_ID)

            weth_key = f"Base_{WETH}"
            current_borrowed_value = abs(totals_usd.get(weth_key, 0))

            success, final_leverage, n_loops = await self._loop_wsteth(
                wsteth_price=wsteth_price,
                weth_price=weth_price,
                current_borrowed_value=current_borrowed_value,
                initial_leverage=initial_leverage,
                usdc_lend_value=usdc_lend_value,
                wsteth_lend_value=wsteth_lend_value,
                collateral_factors=collateral_factors,
            )
            if not success:
                return (
                    False,
                    f"Leverage was {initial_leverage:.2f}x; adjustment failed. "
                    f"Final: {final_leverage} after {n_loops} loops",
                )
            return (
                True,
                f"Adjusted leverage from {initial_leverage:.2f}x to {final_leverage:.2f}x "
                f"via {n_loops} loops",
            )

        # Full redeposit loop
        success, final_leverage, n_loops = await self._execute_deposit_loop(
            usdc_balance
        )
        if not success:
            return (
                False,
                f"Loop failed: {final_leverage} after {n_loops} successful loops",
            )
        return (
            True,
            f"Executed redeposit loop to {final_leverage:.2f}x with {n_loops} loops",
        )

    async def _repay_weth(self, amount: int, remaining_debt: int) -> int:
        if amount <= 0 or remaining_debt <= 0:
            return 0

        amount = int(amount)
        remaining_debt = int(remaining_debt)

        # Only use repay_full when we have a small buffer above the observed debt.
        # This avoids cases where debt accrues between snapshot and execution and leaves dust.
        full_repay_buffer_wei = max(10_000, remaining_debt // 10_000)
        can_repay_full = amount >= (remaining_debt + full_repay_buffer_wei)

        if can_repay_full:
            # Approve the full amount we have available so repayBorrow(MAX_UINT256)
            # can clear debt even if it drifted slightly.
            success, _ = await self.moonwell_adapter.repay(
                mtoken=M_WETH,
                underlying_token=WETH,
                amount=amount,
                repay_full=True,
            )
            return remaining_debt if success else 0

        repay_amt = min(amount, remaining_debt)
        success, _ = await self.moonwell_adapter.repay(
            mtoken=M_WETH,
            underlying_token=WETH,
            amount=repay_amt,
            repay_full=False,
        )
        return repay_amt if success else 0

    async def _swap_to_weth_and_repay(
        self, token_id: str, amount: int, remaining_debt: int
    ) -> int:
        swap_result = await self._swap_with_retries(
            from_token_id=token_id,
            to_token_id=WETH_TOKEN_ID,
            amount=amount,
            preferred_providers=["aerodrome", "enso"],
        )
        if not swap_result:
            return 0

        pinned_block = self._pinned_block(swap_result)

        # Use swap quote amount as minimum expected, retry balance read until we see it
        expected_weth = (
            int(swap_result.get("to_amount") or 0)
            if isinstance(swap_result, dict)
            else 0
        )
        addr = self._get_strategy_wallet_address()
        min_expected = max(1, int(expected_weth * 0.95)) if expected_weth > 0 else 1
        weth_bal = await self._balance_after_tx(
            token_id=WETH_TOKEN_ID,
            wallet=addr,
            pinned_block=pinned_block,
            min_expected=min_expected,
            attempts=5,
        )

        if weth_bal <= 0:
            logger.warning(
                f"WETH balance still 0 after swap, using estimate {expected_weth}"
            )
            weth_bal = expected_weth

        return await self._repay_weth(weth_bal, remaining_debt)

    async def withdraw(self, amount: float | None = None, **kwargs) -> StatusTuple:
        logger.info("")
        logger.info("*" * 60)
        logger.info("* MOONWELL STRATEGY WITHDRAW CALLED")
        logger.info(
            f"* Amount requested: {amount if amount else 'ALL (full withdrawal)'}"
        )
        logger.info("*" * 60)
        self._clear_price_cache()

        status: StatusTuple = (False, "Unknown")
        err: Exception | None = None

        try:
            status = await self._withdraw_impl()
        except Exception as exc:
            err = exc
            if isinstance(exc, SwapOutcomeUnknownError):
                status = (False, f"Swap outcome unknown: {exc}")
            else:
                status = (False, f"Withdraw failed: {exc}")

        # Only run the guard if withdraw failed; a successful withdraw should not touch positions.
        if status[0]:
            return status

        guard_ok, guard_msg = await self._post_run_guard(mode="exit", prior_error=err)
        suffix = (
            f"finalizer: {guard_msg}" if guard_ok else f"finalizer FAILED: {guard_msg}"
        )
        return (False, f"{status[1]} | {suffix}")

    async def _withdraw_impl(self) -> StatusTuple:
        logger.info("=" * 60)
        logger.info("WITHDRAW START - Full position unwind")
        logger.info("=" * 60)

        collateral_factors = await self._get_collateral_factors()

        snap, _ = await self._accounting_snapshot(collateral_factors=collateral_factors)
        logger.info("INITIAL STATE:")
        logger.info(f"  USDC supplied: ${snap.usdc_supplied / 10**6:.2f}")
        logger.info(
            f"  wstETH supplied: {snap.wsteth_supplied / 10**18:.6f} (${snap.wsteth_supplied / 10**18 * snap.wsteth_price:.2f})"
        )
        logger.info(
            f"  WETH debt: {snap.weth_debt / 10**18:.6f} (${snap.weth_debt / 10**18 * snap.weth_price:.2f})"
        )
        logger.info(f"  Wallet USDC: {snap.wallet_usdc / 10**6:.2f}")
        logger.info(f"  Health Factor: {snap.hf:.3f}")
        logger.info("-" * 40)

        # Best-effort: convert reward/odd-lot tokens to WETH to improve repayment.
        logger.info("STEP 1: Sweeping wallet tokens to WETH for debt repayment...")
        await self._sweep_token_balances(target_token_id=WETH_TOKEN_ID)

        # 1) Settle debt to zero (batchy + safe)
        logger.info("STEP 2: Settling WETH debt to zero...")
        logger.info("  Source: Withdraw wstETH collateral → swap to WETH → repay")
        try:
            ok, msg = await self._settle_weth_debt_to_target_usd(
                target_debt_usd=0.0,
                collateral_factors=collateral_factors,
                mode="exit",
                max_batch_usd=4000.0,
                max_steps=30,
            )
        except SwapOutcomeUnknownError as exc:
            return (False, f"Swap outcome unknown while unwinding debt: {exc}")

        if not ok:
            logger.error(f"Failed to settle debt: {msg}")
            return (False, f"Failed to unwind debt: {msg}")
        logger.info("  Debt settled successfully")

        # 2) Unlend everything and convert to USDC
        logger.info("STEP 3: Unlending remaining positions and converting to USDC...")
        logger.info("  Source: Redeem mUSDC → USDC, Redeem mwstETH → swap to USDC")
        try:
            (
                ok,
                msg,
            ) = await self._unlend_remaining_positions()
        except SwapOutcomeUnknownError as exc:
            return (False, f"Swap outcome unknown while unlending positions: {exc}")
        except Exception as exc:  # noqa: BLE001
            return (False, f"Failed while unlending positions: {exc}")

        if not ok:
            logger.error(f"Failed to unlend positions: {msg}")
            return (False, msg)
        logger.info("  Positions unlent successfully")

        # 3) Sweep any wallet leftovers to USDC (keeping ETH)
        logger.info("STEP 4: Sweeping remaining wallet tokens to USDC...")
        try:
            ok, msg = await self._sweep_token_balances(
                target_token_id=USDC_TOKEN_ID,
                exclude={ETH_TOKEN_ID, WELL_TOKEN_ID},
                min_usd_value=float(self.sweep_min_usd),
                strict=True,
            )
        except SwapOutcomeUnknownError as exc:
            return (False, f"Swap outcome unknown while sweeping wallet: {exc}")

        if not ok:
            logger.error(f"Failed to sweep wallet: {msg}")
            return (False, msg)
        logger.info("  Wallet swept successfully")

        # Step 5: Report final balances in strategy wallet
        logger.info("STEP 5: Checking final balances in strategy wallet...")
        usdc_balance = await self._get_balance_raw(
            token_id=USDC_TOKEN_ID, wallet_address=self._get_strategy_wallet_address()
        )
        gas_balance = await self._get_balance_raw(
            token_id=ETH_TOKEN_ID, wallet_address=self._get_strategy_wallet_address()
        )

        token_info = await self._get_token_info(USDC_TOKEN_ID)
        decimals = token_info.get("decimals", 6)
        usdc_amount = usdc_balance / 10**decimals if usdc_balance > 0 else 0.0
        gas_amount = gas_balance / 10**18 if gas_balance > 0 else 0.0

        logger.info(f"  Strategy wallet USDC: {usdc_amount:.2f}")
        logger.info(f"  Strategy wallet ETH: {gas_amount:.6f}")

        return (
            True,
            f"Positions liquidated. Strategy wallet contains {usdc_amount:.2f} USDC and {gas_amount:.6f} ETH. Call exit() to transfer to main wallet.",
        )

    async def exit(self, **kwargs) -> StatusTuple:
        logger.info("")
        logger.info("*" * 60)
        logger.info("* MOONWELL STRATEGY EXIT CALLED")
        logger.info("*" * 60)

        transferred_items = []

        usdc_balance = await self._get_balance_raw(
            token_id=USDC_TOKEN_ID, wallet_address=self._get_strategy_wallet_address()
        )
        if usdc_balance > 0:
            token_info = await self._get_token_info(USDC_TOKEN_ID)
            decimals = token_info.get("decimals", 6)
            usdc_amount = usdc_balance / 10**decimals
            logger.info(f"Transferring {usdc_amount:.2f} USDC to main wallet")

            (
                success,
                msg,
            ) = await self.balance_adapter.move_from_strategy_wallet_to_main_wallet(
                USDC_TOKEN_ID, usdc_amount
            )
            if not success:
                logger.error(f"USDC transfer failed: {msg}")
                return (False, f"USDC transfer failed: {msg}")
            transferred_items.append(f"{usdc_amount:.2f} USDC")
            logger.info(f"USDC transfer successful: {usdc_amount:.2f} USDC")

        gas_balance = await self._get_balance_raw(
            token_id=ETH_TOKEN_ID, wallet_address=self._get_strategy_wallet_address()
        )
        tx_fee_reserve = int(0.0002 * 10**18)
        transferable_gas = gas_balance - tx_fee_reserve
        if transferable_gas > 0:
            gas_amount = transferable_gas / 10**18
            logger.info(f"Transferring {gas_amount:.6f} ETH to main wallet")
            (
                gas_success,
                gas_msg,
            ) = await self.balance_adapter.move_from_strategy_wallet_to_main_wallet(
                ETH_TOKEN_ID, gas_amount
            )
            if gas_success:
                transferred_items.append(f"{gas_amount:.6f} ETH")
                logger.info(f"ETH transfer successful: {gas_amount:.6f} ETH")
            else:
                logger.warning(f"ETH transfer failed (non-critical): {gas_msg}")

        if not transferred_items:
            return (True, "No funds to transfer to main wallet")

        return (True, f"Transferred to main wallet: {', '.join(transferred_items)}")

    async def _unlend_remaining_positions(self) -> tuple[bool, str]:
        logger.info("UNLEND: Redeeming remaining Moonwell positions...")

        wsteth_pos = await self.moonwell_adapter.get_pos(mtoken=M_WSTETH)
        if wsteth_pos[0]:
            mtoken_bal = wsteth_pos[1].get("mtoken_balance", 0)
            underlying = wsteth_pos[1].get("underlying_balance", 0)
            if mtoken_bal > 0:
                logger.info(f"  Unlending wstETH: {underlying / 10**18:.6f} wstETH")
                ok, msg = await self.moonwell_adapter.unlend(
                    mtoken=M_WSTETH, amount=mtoken_bal
                )
                if not ok:
                    return (False, f"Failed to unlend wstETH: {msg}")
                wsteth_bal = await self._get_balance_raw(
                    token_id=WSTETH_TOKEN_ID,
                    wallet_address=self._get_strategy_wallet_address(),
                )
                if wsteth_bal > 0:
                    swap_result = await self._swap_with_retries(
                        from_token_id=WSTETH_TOKEN_ID,
                        to_token_id=USDC_TOKEN_ID,
                        amount=wsteth_bal,
                    )
                    if swap_result is None:
                        return (False, "Failed to swap wstETH to USDC after retries")

        usdc_pos = await self.moonwell_adapter.get_pos(mtoken=M_USDC)
        if usdc_pos[0]:
            mtoken_bal = usdc_pos[1].get("mtoken_balance", 0)
            underlying = usdc_pos[1].get("underlying_balance", 0)
            if mtoken_bal > 0:
                logger.info(f"  Unlending USDC: {underlying / 10**6:.2f} USDC")
                ok, msg = await self.moonwell_adapter.unlend(
                    mtoken=M_USDC, amount=mtoken_bal
                )
                if not ok:
                    return (False, f"Failed to unlend USDC: {msg}")

        await self.moonwell_adapter.claim_rewards(min_rewards_usd=0)

        ok, msg = await self._sweep_token_balances(
            target_token_id=USDC_TOKEN_ID,
            exclude={ETH_TOKEN_ID, WELL_TOKEN_ID},
            min_usd_value=float(self.sweep_min_usd),
            strict=True,
        )
        if not ok:
            return (False, msg)
        return (True, "Unlent remaining positions")

    async def get_peg_diff(self) -> float | dict:
        steth_price = await self._get_token_price(STETH_TOKEN_ID)
        weth_price = await self._get_token_price(WETH_TOKEN_ID)

        if not steth_price or not weth_price or weth_price <= 0:
            return {
                "ok": False,
                "error": f"Bad price data stETH={steth_price}, WETH={weth_price}",
            }

        peg_ratio = steth_price / weth_price
        peg_diff = abs(peg_ratio - 1)

        return peg_diff

    async def _status(self) -> StatusDict:
        snap, _ = await self._accounting_snapshot()
        totals_usd = dict(snap.totals_usd)
        _, net_deposit = await self.ledger_adapter.get_strategy_net_deposit(
            wallet_address=self._get_strategy_wallet_address()
        )

        ltv = float(snap.ltv)

        wallet_balances: dict[str, dict[str, float]] = {}
        wallet_tokens = {
            "WETH": ("wallet_weth", TOKEN_ID_WETH_BASE),
            "USDC": ("wallet_usdc", TOKEN_ID_USDC_BASE),
            "wstETH": ("wallet_wsteth", TOKEN_ID_WSTETH_BASE),
        }
        # Assume snapshot exposes token prices and decimals keyed by token id
        prices_usd = getattr(snap, "prices_usd", {}) or {}
        token_decimals = getattr(snap, "token_decimals", {}) or {}
        for symbol, (attr_name, token_id) in wallet_tokens.items():
            raw_balance = getattr(snap, attr_name, 0) or 0
            if raw_balance:
                decimals = token_decimals.get(token_id, 18)
                price = prices_usd.get(token_id, 0.0) or 0.0
                tokens = float(raw_balance) / float(10**decimals)
                usd_value = tokens * float(price)
                wallet_balances[symbol] = {
                    "tokens": tokens,
                    "usd": usd_value,
                }
        hf = (1 / ltv) if ltv and ltv > 0 and not (ltv != ltv) else None

        gas_balance = await self._get_gas_balance()

        borrowable_result = await self.moonwell_adapter.get_borrowable_amount()
        borrowable_amt_raw = borrowable_result[1] if borrowable_result[0] else 0
        borrowable_amt = self._normalize_usd_value(borrowable_amt_raw)

        total_borrowed = float(snap.debt_usd)
        credit_remaining = 1.0
        if (borrowable_amt + total_borrowed) > 0:
            credit_remaining = round(
                borrowable_amt / (borrowable_amt + total_borrowed), 4
            )

        peg_diff = await self.get_peg_diff()

        # Calculate displayed portfolio value.
        # Note: snap.net_equity_usd represents net equity (wallet + supplies - debt),
        # so wallet_value is added here only for this aggregate display metric.
        net_equity_value = float(snap.net_equity_usd)
        wallet_value = sum(wb["usd"] for wb in wallet_balances.values())
        portfolio_value = net_equity_value + wallet_value

        quote = await self._quote()

        strategy_status = {
            "current_positions_usd_value": totals_usd,
            "wallet_balances": {
                k: v for k, v in wallet_balances.items() if v["tokens"] > 0
            },
            "wallet_balances_total_usd": wallet_value,
            "credit_remaining": f"{credit_remaining * 100:.2f}%",
            "LTV": ltv,
            "health_factor": hf,
            "projected_earnings": quote.get("data", {}),
            "steth_eth_peg_difference": peg_diff,
        }

        return StatusDict(
            portfolio_value=portfolio_value,
            net_deposit=net_deposit,
            strategy_status=strategy_status,
            gas_available=gas_balance / 10**18,
            gassed_up=gas_balance >= int(self.MAINTENANCE_GAS * 10**18),
        )

    @staticmethod
    async def policies() -> list[str]:
        return [
            # Moonwell operations
            await musdc_mint_or_approve_or_redeem(),
            await mweth_approve_or_borrow_or_repay(),
            await mwsteth_approve_or_mint_or_redeem(),
            await moonwell_comptroller_enter_markets_or_claim_rewards(),
            await weth_deposit(),
            # Swaps
            erc20_spender_for_any_token(ENSO_ROUTER),
            await enso_swap(),
        ]
