from __future__ import annotations

import asyncio
import math
import random
import time
import traceback
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal, getcontext
from pathlib import Path
from statistics import fmean
from typing import Any

from wayfinder_paths.adapters.balance_adapter.adapter import BalanceAdapter
from wayfinder_paths.adapters.hyperliquid_adapter.adapter import HyperliquidAdapter
from wayfinder_paths.adapters.hyperliquid_adapter.paired_filler import (
    MIN_NOTIONAL_USD,
    FillConfig,
    PairedFiller,
)
from wayfinder_paths.adapters.hyperliquid_adapter.utils import (
    normalize_l2_book as hl_normalize_l2_book,
)
from wayfinder_paths.adapters.hyperliquid_adapter.utils import (
    round_size_for_asset as hl_round_size_for_asset,
)
from wayfinder_paths.adapters.hyperliquid_adapter.utils import (
    size_step as hl_size_step,
)
from wayfinder_paths.adapters.hyperliquid_adapter.utils import (
    spot_index_from_asset_id as hl_spot_index_from_asset_id,
)
from wayfinder_paths.adapters.hyperliquid_adapter.utils import (
    usd_depth_in_band as hl_usd_depth_in_band,
)
from wayfinder_paths.adapters.token_adapter.adapter import TokenAdapter
from wayfinder_paths.core.analytics import (
    block_bootstrap_paths as analytics_block_bootstrap_paths,
)
from wayfinder_paths.core.analytics import (
    percentile as analytics_percentile,
)
from wayfinder_paths.core.analytics import (
    rolling_min_sum as analytics_rolling_min_sum,
)
from wayfinder_paths.core.analytics import (
    z_from_conf as analytics_z_from_conf,
)
from wayfinder_paths.core.constants.contracts import HYPE_FEE_WALLET, HYPERLIQUID_BRIDGE
from wayfinder_paths.core.strategies.descriptors import (
    Complexity,
    Directionality,
    Frequency,
    StratDescriptor,
    TokenExposure,
    Volatility,
)
from wayfinder_paths.core.strategies.Strategy import StatusDict, StatusTuple, Strategy
from wayfinder_paths.policies.erc20 import any_erc20_function
from wayfinder_paths.policies.hyperliquid import (
    any_hyperliquid_l1_payload,
    any_hyperliquid_user_payload,
)
from wayfinder_paths.strategies.basis_trading_strategy.constants import (
    USDC_ARBITRUM_TOKEN_ID,
)
from wayfinder_paths.strategies.basis_trading_strategy.snapshot_mixin import (
    BasisSnapshotMixin,
)
from wayfinder_paths.strategies.basis_trading_strategy.types import (
    BasisCandidate,
    BasisPosition,
)

getcontext().prec = 28


def _d(x: float | Decimal | str) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


class BasisTradingStrategy(BasisSnapshotMixin, Strategy):
    name = "Basis Trading Strategy"

    # Strategy parameters
    MIN_DEPOSIT_USDC = 25
    DEFAULT_LOOKBACK_DAYS = 30
    DEFAULT_FEE_EPS = 0.003
    DEFAULT_OI_FLOOR = 100_000.0
    DEFAULT_DAY_VLM_FLOOR = 100_000
    DEFAULT_MAX_LEVERAGE = 2
    GAS_MAXIMUM = 0.01
    DEFAULT_BOOTSTRAP_SIMS = 50
    DEFAULT_BOOTSTRAP_BLOCK_HOURS = 48

    # Liquidation and rebalance thresholds
    # Trigger rebalance at 75% to liquidation
    LIQUIDATION_REBALANCE_THRESHOLD = 0.75
    # Stop-loss at 90% to liquidation (closer)
    LIQUIDATION_STOP_LOSS_THRESHOLD = 0.90
    FUNDING_REBALANCE_THRESHOLD = 0.02

    # Position tolerances
    SPOT_POSITION_DUST_TOLERANCE = 0.04
    MIN_UNUSED_USD = 5.0
    UNUSED_REL_EPS = 0.01

    TOKEN_DECIMALS = 1e6
    GAS_DECIMALS = 1e18

    # Rotation cooldown
    ROTATION_MIN_INTERVAL_DAYS = 14

    HYPE_PRO_FEE: int = 30
    DEFAULT_BUILDER_FEE: dict[str, Any] = {"b": HYPE_FEE_WALLET, "f": HYPE_PRO_FEE}

    INFO = StratDescriptor(
        description="""Delta-neutral basis trading on Hyperliquid that captures funding rate payments.
            **What it does:** Analyzes historical funding rates, price volatility, and liquidity across
            Hyperliquid markets to identify optimal basis trading opportunities. Opens matched spot long
            and perpetual short positions to capture positive funding while remaining market neutral.
            **Exposure type:** Delta-neutral - equal long spot and short perp exposure cancels price risk.
            **Chains:** Hyperliquid (Arbitrum for deposits).
            **Deposit/Withdrawal:** Deposits USDC which is used to open basis positions.
            Withdrawals close all positions and return USDC to main wallet.
            **Risk:** Funding rates can flip negative; liquidation risk if leverage too high.
            """,
        summary=(
            "Automated delta-neutral basis trading on Hyperliquid, capturing funding rate payments "
            "through matched spot long / perp short positions with intelligent leverage sizing."
        ),
        risk_description="Protocol risk is always present when engaging with DeFi strategies, this includes underlying DeFi protocols and Wayfinder itself. Additional risks include funding rate reversals, liquidity constraints on Hyperliquid, smart contract risk, and temporary capital lock-up during volatile market conditions. During extreme price movements, high volatility can stop out the short side of positions, breaking delta-neutrality and leaving unhedged long exposure that suffers losses when prices revert downward. This can cause significant damage beyond normal funding rate fluctuations.",
        fee_description="Wayfinder takes a 2 basis point (0.02%) builder fee on all orders placed on Hyperliquid through this strategy. If fees remain unpaid, Wayfinder may pause automated management of this vault.",
        gas_token_symbol="ETH",
        gas_token_id="ethereum-arbitrum",
        deposit_token_id="usd-coin-arbitrum",
        minimum_net_deposit=MIN_DEPOSIT_USDC,
        gas_maximum=GAS_MAXIMUM,
        gas_threshold=GAS_MAXIMUM / 3,
        volatility=Volatility.MEDIUM,
        volatility_description="Delta-neutral but funding can flip negative.",
        directionality=Directionality.DELTA_NEUTRAL,
        directionality_description="Matched spot long and perp short cancels directional exposure.",
        complexity=Complexity.MEDIUM,
        complexity_description="Requires understanding of funding rates and leverage.",
        token_exposure=TokenExposure.STABLECOINS,
        token_exposure_description="Capital in USDC, exposed to crypto through hedged positions.",
        frequency=Frequency.LOW,
        frequency_description="Positions held for days/weeks to accumulate funding.",
        return_drivers=["funding rate", "basis spread"],
        config={
            "deposit": {
                "description": "Deposit USDC to fund basis trading positions.",
                "parameters": {
                    "main_token_amount": {
                        "type": "float",
                        "unit": "USDC",
                        "description": "Amount of USDC to allocate.",
                        "minimum": MIN_DEPOSIT_USDC,
                    },
                    "gas_token_amount": {
                        "type": "float",
                        "unit": "ETH",
                        "description": "Amount of ETH for gas.",
                        "minimum": 0.0,
                        "maximum": GAS_MAXIMUM,
                    },
                },
            },
            "update": {
                "description": "Analyze markets and open/monitor positions.",
            },
            "withdraw": {
                "description": "Close all positions and return USDC to main wallet.",
            },
        },
    )

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        main_wallet: dict[str, Any] | None = None,
        strategy_wallet: dict[str, Any] | None = None,
        strategy_sign_typed_data: Callable[[dict], Awaitable[str]] | None = None,
        main_wallet_signing_callback: Callable[[dict], Awaitable[str]] | None = None,
        strategy_wallet_signing_callback: Callable[[dict], Awaitable[str]]
        | None = None,
    ) -> None:
        super().__init__(
            main_wallet_signing_callback=main_wallet_signing_callback,
            strategy_wallet_signing_callback=strategy_wallet_signing_callback,
            strategy_sign_typed_data=strategy_sign_typed_data,
        )

        merged_config = dict(config or {})
        if main_wallet:
            merged_config["main_wallet"] = main_wallet
        if strategy_wallet:
            merged_config["strategy_wallet"] = strategy_wallet
        self.config: dict[str, Any] = merged_config

        self.current_position: BasisPosition | None = None
        self.deposit_amount: float = 0.0

        self.builder_fee: dict[str, Any] | None = self.config.get(
            "builder_fee", self.DEFAULT_BUILDER_FEE
        )

        self._margin_table_cache: dict[int, list[dict[str, float]]] = {}

        try:
            adapter_config = {
                "main_wallet": self.config.get("main_wallet"),
                "strategy_wallet": self.config.get("strategy_wallet"),
                "strategy": self.config,
            }

            self.hyperliquid_adapter = HyperliquidAdapter(
                config=adapter_config,
                sign_callback=strategy_sign_typed_data,
            )
            strat_addr = (self.config.get("strategy_wallet") or {}).get("address")
            main_addr = (self.config.get("main_wallet") or {}).get("address")

            self.balance_adapter = BalanceAdapter(
                adapter_config,
                main_sign_callback=self.main_wallet_signing_callback,
                sign_callback=self.strategy_wallet_signing_callback,
                main_wallet_address=main_addr,
                wallet_address=strat_addr,
            )
            self.token_adapter = TokenAdapter()
        except Exception as e:
            self.logger.error(f"Failed to initialize strategy adapters: {e}")
            raise

    async def setup(self) -> None:
        self.logger.info("Starting BasisTradingStrategy setup")
        start_time = time.time()

        await super().setup()

        try:
            success, deposit_data = await self.ledger_adapter.get_strategy_net_deposit(
                wallet_address=self._get_strategy_wallet_address()
            )
            if success and deposit_data is not None:
                self.deposit_amount = float(deposit_data)
        except Exception as e:
            self.logger.warning(f"Could not fetch deposit data: {e}")

        # Discover existing positions from Hyperliquid (critical for restart recovery)
        try:
            await self._discover_existing_position()
        except Exception as e:
            self.logger.warning(f"Could not discover existing positions: {e}")

        elapsed = time.time() - start_time
        self.logger.info(f"BasisTradingStrategy setup completed in {elapsed:.2f}s")

    async def _discover_existing_position(self) -> None:
        address = self._get_strategy_wallet_address()

        user_state_result = await self.hyperliquid_adapter.get_user_state(address)
        if not user_state_result[0]:
            self.logger.warning("Could not fetch user state for position discovery")
            return

        asset_positions = user_state_result[1].get("assetPositions", [])
        if not asset_positions:
            self.logger.info("No existing perp positions found")
            return

        # Find SHORT perp position (basis trading uses short perp)
        perp_position = None
        for pos_wrapper in asset_positions:
            pos = pos_wrapper.get("position", {})
            szi = float(pos.get("szi", 0))
            if szi < 0:
                perp_position = pos
                break

        if not perp_position:
            self.logger.info("No short perp position found")
            return

        coin = perp_position.get("coin")
        perp_size = abs(float(perp_position.get("szi", 0)))
        entry_px = float(perp_position.get("entryPx", 0))

        spot_state_result = await self.hyperliquid_adapter.get_spot_user_state(address)
        if not spot_state_result[0]:
            self.logger.warning(
                f"Found perp position on {coin} but could not fetch spot state"
            )
            return

        spot_position = None
        spot_balances = spot_state_result[1].get("balances", [])
        for bal in spot_balances:
            bal_coin = bal.get("coin", "")
            # Match coin name (spot might have different naming)
            if (
                bal_coin == coin
                or bal_coin.startswith(coin)
                or coin.startswith(bal_coin.replace("U", ""))
            ):
                total = float(bal.get("total", 0))
                if total > 0:
                    spot_position = bal
                    break

        if not spot_position:
            self.logger.warning(
                f"Found perp position on {coin} but no matching spot position - "
                "may have partial exposure"
            )
            # Still track it so we don't open another position
            spot_size = 0.0
        else:
            spot_size = float(spot_position.get("total", 0))

        perp_asset_id = self.hyperliquid_adapter.coin_to_asset.get(coin)
        # Spot asset ID: look up from spot meta or estimate
        spot_asset_id = None
        success, spot_meta = await self.hyperliquid_adapter.get_spot_meta()
        if success:
            tokens = spot_meta.get("tokens", [])
            universe = spot_meta.get("universe", [])
            for pair in universe:
                base_idx = pair["tokens"][0]
                for t in tokens:
                    if t["index"] == base_idx:
                        if (
                            t["name"] == coin
                            or t["name"] == f"U{coin}"
                            or t["name"].replace("U", "") == coin
                        ):
                            spot_asset_id = pair["index"] + 10000
                            break
                if spot_asset_id:
                    break

        # Reconstruct position state
        self.current_position = BasisPosition(
            coin=coin,
            spot_asset_id=spot_asset_id or 0,
            perp_asset_id=perp_asset_id or 0,
            spot_amount=spot_size,
            perp_amount=perp_size,
            entry_price=entry_px,
            leverage=2,
            entry_timestamp=int(time.time() * 1000),
            funding_collected=abs(
                float(perp_position.get("cumFunding", {}).get("sinceOpen", 0))
            ),
        )

        if self.deposit_amount <= 0:
            margin_summary = user_state_result[1].get("marginSummary", {})
            self.deposit_amount = float(margin_summary.get("accountValue", 0))

        self.logger.info(
            f"Discovered existing position: {coin} "
            f"(perp={perp_size:.4f}, spot={spot_size:.4f}, entry=${entry_px:.2f})"
        )

    async def quote(self, deposit_amount: float | None = None) -> dict:
        deposit_usdc = deposit_amount or 1000.0

        best = await self.find_best_trade_with_backtest(
            deposit_usdc=deposit_usdc,
            stop_frac=self.LIQUIDATION_REBALANCE_THRESHOLD,
            lookback_days=self.DEFAULT_LOOKBACK_DAYS,
            max_leverage=self.DEFAULT_MAX_LEVERAGE,
            bootstrap_sims=50,  # Reduced from default 200 for speed
        )

        if not best:
            return {
                "expected_apy": 0.0,
                "apy_type": "net",
                "confidence": "low",
                "methodology": "No qualifying basis trades found",
                "components": {},
                "deposit_amount": deposit_usdc,
                "as_of": datetime.now(UTC).isoformat(),
                "summary": "No qualifying basis trades found meeting liquidity/risk criteria",
            }

        net_apy = float(best.get("net_apy", 0.0))
        gross_apy = float(best.get("gross_funding_apy", 0.0))
        coin = best.get("coin", "")
        leverage = int(best.get("best_L", 1))

        return {
            "expected_apy": net_apy,
            "apy_type": "net",
            "confidence": "medium",
            "methodology": f"Basis trade on {coin} at {leverage}x leverage over {self.DEFAULT_LOOKBACK_DAYS}-day backtest",
            "components": {
                "gross_funding_apy": gross_apy,
                "net_apy": net_apy,
                "coin": coin,
                "leverage": leverage,
                "entry_cost_usd": best.get("entry_cost_usd", 0.0),
                "exit_cost_usd": best.get("exit_cost_usd", 0.0),
            },
            "deposit_amount": deposit_usdc,
            "as_of": datetime.now(UTC).isoformat(),
            "summary": f"Expected net APY: {net_apy * 100:.2f}% on {coin} basis trade at {leverage}x",
        }

    async def deposit(
        self,
        main_token_amount: float = 0.0,
        gas_token_amount: float = 0.0,
    ) -> StatusTuple:
        if main_token_amount < self.MIN_DEPOSIT_USDC:
            return (False, f"Minimum deposit is {self.MIN_DEPOSIT_USDC} USDC")

        if gas_token_amount > self.GAS_MAXIMUM:
            return (False, f"Gas amount exceeds maximum {self.GAS_MAXIMUM} ETH")

        self.logger.info(f"Depositing {main_token_amount} USDC to Hyperliquid L1")

        # Transfer ETH for gas if requested
        if gas_token_amount > 0:
            main_address = self._get_main_wallet_address()
            strategy_address = self._get_strategy_wallet_address()
            self.logger.info(
                f"Transferring {gas_token_amount} ETH for gas from main wallet "
                f"({main_address}) to strategy wallet ({strategy_address})"
            )
            (
                gas_ok,
                gas_res,
            ) = await self.balance_adapter.move_from_main_wallet_to_strategy_wallet(
                token_id="ethereum-arbitrum",
                amount=gas_token_amount,
                strategy_name=self.name or "basis_trading_strategy",
            )
            if not gas_ok:
                self.logger.error(f"Failed to transfer ETH for gas: {gas_res}")
                return (False, f"Failed to transfer ETH for gas: {gas_res}")
            self.logger.info(f"Gas transfer successful: {gas_res}")

        try:
            main_address = self._get_main_wallet_address()
            strategy_address = self._get_strategy_wallet_address()

            (
                strategy_balance_ok,
                strategy_balance,
            ) = await self.balance_adapter.get_balance(
                token_id=USDC_ARBITRUM_TOKEN_ID,
                wallet_address=strategy_address,
            )
            strategy_usdc = 0.0
            if strategy_balance_ok and strategy_balance:
                # Balance is returned in raw units, USDC has 6 decimals
                strategy_usdc = float(strategy_balance) / self.TOKEN_DECIMALS

            need_to_move = main_token_amount - strategy_usdc
            if main_address.lower() != strategy_address.lower() and need_to_move > 0.01:
                self.logger.info(
                    f"Moving {need_to_move:.2f} USDC from main wallet ({main_address}) "
                    f"to strategy wallet ({strategy_address}) [existing: {strategy_usdc:.2f}]"
                )
                (
                    move_ok,
                    move_res,
                ) = await self.balance_adapter.move_from_main_wallet_to_strategy_wallet(
                    token_id=USDC_ARBITRUM_TOKEN_ID,
                    amount=need_to_move,
                    strategy_name=self.name or "basis_trading_strategy",
                )
                if not move_ok:
                    self.logger.error(
                        f"Failed to move USDC into strategy wallet: {move_res}"
                    )
                    return (
                        False,
                        f"Failed to move USDC into strategy wallet: {move_res}",
                    )
            elif strategy_usdc >= main_token_amount:
                self.logger.info(
                    f"Strategy wallet already has {strategy_usdc:.2f} USDC, skipping transfer from main"
                )

            self.deposit_amount += main_token_amount

            return (
                True,
                f"Transferred {main_token_amount} USDC to strategy wallet ({strategy_address}). "
                f"Total deposits: ${self.deposit_amount:.2f}. "
                f"Call update() to bridge to Hyperliquid and open positions.",
            )

        except Exception as e:
            self.logger.error(f"Deposit failed: {e}")
            return (False, f"Deposit failed: {e}")

    async def update(self) -> StatusTuple:
        strategy_address = self._get_strategy_wallet_address()
        strategy_wallet = self.config.get("strategy_wallet")

        strategy_usdc = 0.0
        try:
            (
                strategy_balance_ok,
                strategy_balance,
            ) = await self.balance_adapter.get_balance(
                token_id=USDC_ARBITRUM_TOKEN_ID,
                wallet_address=strategy_address,
            )
            if strategy_balance_ok and strategy_balance:
                strategy_usdc = float(strategy_balance) / self.TOKEN_DECIMALS
        except Exception as e:
            self.logger.warning(f"Could not check strategy wallet balance: {e}")

        hl_usdc = 0.0
        try:
            perp_margin, spot_usdc = await self._get_undeployed_capital()
            hl_usdc = perp_margin + spot_usdc
        except Exception as e:
            self.logger.warning(f"Could not check Hyperliquid balance: {e}")

        total_available = strategy_usdc + hl_usdc
        if total_available > 1.0:
            self.deposit_amount = max(self.deposit_amount, total_available)

        if total_available < 1.0 and self.current_position is None:
            return (False, "No funds to manage. Call deposit() first.")

        if strategy_usdc > 10.0:
            try:
                self.logger.info(
                    f"Found ${strategy_usdc:.2f} USDC in strategy wallet, bridging to Hyperliquid"
                )

                # Convert USDC to raw units (6 decimals)
                usdc_raw = int(strategy_usdc * self.TOKEN_DECIMALS)
                success, result = await self.balance_adapter.send_to_address(
                    token_id=USDC_ARBITRUM_TOKEN_ID,
                    amount=usdc_raw,
                    from_wallet=strategy_wallet,
                    to_address=HYPERLIQUID_BRIDGE,
                    signing_callback=self.strategy_wallet_signing_callback,
                )

                if not success:
                    self.logger.error(f"Failed to send USDC to bridge: {result}")
                    return (False, f"Failed to bridge USDC to Hyperliquid: {result}")

                self.logger.info(f"USDC sent to bridge, tx: {result}")

                self.logger.info("Waiting for Hyperliquid to credit the deposit...")

                (
                    deposit_confirmed,
                    final_balance,
                ) = await self.hyperliquid_adapter.wait_for_deposit(
                    address=strategy_address,
                    expected_increase=strategy_usdc,
                    timeout_s=180,
                    poll_interval_s=10,
                )

                if not deposit_confirmed:
                    self.logger.warning(
                        f"Deposit not confirmed within timeout. "
                        f"Current HL balance: ${final_balance:.2f}. "
                        f"Deposit may still be processing."
                    )
                    return (
                        True,
                        f"Sent ${strategy_usdc:.2f} USDC to bridge. Deposit still processing. "
                        f"Current HL balance: ${final_balance:.2f}. Call update() again.",
                    )

                self.logger.info(
                    f"Successfully bridged ${strategy_usdc:.2f} USDC to Hyperliquid"
                )
            except Exception as e:
                self.logger.warning(f"Failed to bridge USDC to Hyperliquid: {e}")

        if self.current_position is None:
            return await self._find_and_open_position(rotation_reason="initial_open")

        # Monitor existing position (handles idle capital, leg balance, stop-loss)
        return await self._monitor_position()

    async def analyze(
        self, deposit_usdc: float = 1000.0, verbose: bool = True
    ) -> dict[str, Any]:
        self.logger.info(
            f"Analyzing basis opportunities for ${deposit_usdc} deposit..."
        )

        debug_info: dict[str, Any] = {}

        try:
            snapshot = self._snapshot_from_config()
            if snapshot is not None:
                try:
                    opportunities = self.opportunities_from_snapshot(
                        snapshot=snapshot, deposit_usdc=deposit_usdc
                    )
                    return {
                        "success": True,
                        "source": "snapshot",
                        "snapshot_path": None,
                        "snapshot_hour_bucket_utc": snapshot.get("hour_bucket_utc"),
                        "deposit_usdc": deposit_usdc,
                        "opportunities_count": len(opportunities),
                        "opportunities": opportunities,
                        "debug": debug_info if verbose else None,
                    }
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning(
                        f"Failed to use in-memory snapshot: {exc}. Falling back to live analysis."
                    )

            snapshot_path = self._snapshot_path_from_config()
            if snapshot_path and Path(snapshot_path).exists():
                try:
                    snapshot = self.load_snapshot_from_path(snapshot_path)
                    opportunities = self.opportunities_from_snapshot(
                        snapshot=snapshot, deposit_usdc=deposit_usdc
                    )
                    return {
                        "success": True,
                        "source": "snapshot",
                        "snapshot_path": snapshot_path,
                        "snapshot_hour_bucket_utc": snapshot.get("hour_bucket_utc"),
                        "deposit_usdc": deposit_usdc,
                        "opportunities_count": len(opportunities),
                        "opportunities": opportunities,
                        "debug": debug_info if verbose else None,
                    }
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning(
                        f"Failed to load/use snapshot from {snapshot_path}: {exc}. "
                        "Falling back to live analysis."
                    )

            (
                success,
                perps_ctx_pack,
            ) = await self.hyperliquid_adapter.get_meta_and_asset_ctxs()
            if success:
                perps_meta_list = perps_ctx_pack[0]["universe"]
                debug_info["perp_count"] = len(perps_meta_list)

            success, spot_meta = await self.hyperliquid_adapter.get_spot_meta()
            if success:
                spot_pairs = spot_meta.get("universe", [])
                debug_info["spot_pair_count"] = len(spot_pairs)

            bootstrap_sims = int(
                self._cfg_get("bootstrap_sims", self.DEFAULT_BOOTSTRAP_SIMS) or 0
            )
            bootstrap_block_hours = int(
                self._cfg_get(
                    "bootstrap_block_hours", self.DEFAULT_BOOTSTRAP_BLOCK_HOURS
                )
                or 0
            )
            bootstrap_seed = self._cfg_get("bootstrap_seed")
            bootstrap_seed = int(bootstrap_seed) if bootstrap_seed is not None else None
            self.logger.info(
                f"Bootstrap settings: sims={bootstrap_sims}, block_hours={bootstrap_block_hours}, "
                f"seed={'random' if bootstrap_seed is None else bootstrap_seed}"
            )

            opportunities = await self.solve_candidates_max_net_apy_with_stop(
                deposit_usdc=deposit_usdc,
                stop_frac=self.LIQUIDATION_REBALANCE_THRESHOLD,
                lookback_days=self.DEFAULT_LOOKBACK_DAYS,
                fee_eps=self.DEFAULT_FEE_EPS,
                oi_floor=self.DEFAULT_OI_FLOOR,
                day_vlm_floor=self.DEFAULT_DAY_VLM_FLOOR,
                max_leverage=self.DEFAULT_MAX_LEVERAGE,
                bootstrap_sims=bootstrap_sims,
                bootstrap_block_hours=bootstrap_block_hours,
                bootstrap_seed=bootstrap_seed,
            )

            if verbose:
                self.logger.info(
                    f"Found {len(opportunities)} opportunities after all filters"
                )

            return {
                "success": True,
                "source": "live",
                "deposit_usdc": deposit_usdc,
                "bootstrap": {
                    "sims": bootstrap_sims,
                    "block_hours": bootstrap_block_hours,
                    "seed": bootstrap_seed,
                },
                "opportunities_count": len(opportunities),
                "opportunities": opportunities,
                "debug": debug_info if verbose else None,
            }

        except Exception as e:
            self.logger.error(f"Analysis failed: {e}")
            traceback.print_exc()
            return {
                "success": False,
                "error": str(e),
                "opportunities": [],
                "debug": debug_info if verbose else None,
            }

    def _cfg_get(self, key: str, default: Any | None = None) -> Any:
        if key in self.config:
            return self.config.get(key, default)
        nested = self.config.get("strategy")
        if isinstance(nested, dict) and key in nested:
            return nested.get(key, default)
        return default

    def _resolve_mid_price(self, coin: str, mid_prices: dict[str, float]) -> float:
        # Direct match
        if coin in mid_prices:
            return mid_prices[coin]

        # Case variations
        for key in [coin.upper(), coin.lower()]:
            if key in mid_prices:
                return mid_prices[key]

        # Strip U-prefix (UXPL -> XPL)
        if coin.startswith("U") and len(coin) > 1:
            stripped = coin[1:]
            for key in [stripped, stripped.upper(), stripped.lower()]:
                if key in mid_prices:
                    return mid_prices[key]

        prefixed = f"U{coin}"
        for key in [prefixed, prefixed.upper(), prefixed.lower()]:
            if key in mid_prices:
                return mid_prices[key]

        return 0.0

    def _coins_match(self, coin1: str, coin2: str) -> bool:
        if coin1 == coin2:
            return True
        # Strip U-prefix from either and compare
        c1 = coin1[1:] if coin1.startswith("U") and len(coin1) > 1 else coin1
        c2 = coin2[1:] if coin2.startswith("U") and len(coin2) > 1 else coin2
        return c1 == c2

    async def withdraw(self, amount: float | None = None, **kwargs) -> StatusTuple:
        address = self._get_strategy_wallet_address()
        usdc_token_id = "usd-coin-arbitrum"

        strategy_usdc = 0.0
        try:
            success, balance_data = await self.balance_adapter.get_balance(
                token_id=usdc_token_id,
                wallet_address=address,
            )
            if success:
                strategy_usdc = float(balance_data) / self.TOKEN_DECIMALS
        except Exception as e:
            self.logger.warning(f"Could not get strategy wallet balance: {e}")

        hl_perp_value = 0.0
        hl_spot_usdc = 0.0
        user_state_result = await self.hyperliquid_adapter.get_user_state(address)
        if user_state_result[0]:
            margin_summary = user_state_result[1].get("marginSummary", {})
            hl_perp_value = float(margin_summary.get("accountValue", 0))

        spot_state_result = await self.hyperliquid_adapter.get_spot_user_state(address)
        if spot_state_result[0]:
            for bal in spot_state_result[1].get("balances", []):
                if bal.get("coin") == "USDC":
                    hl_spot_usdc = float(bal.get("total", 0))
                    break

        hl_value = hl_perp_value + hl_spot_usdc

        if strategy_usdc < 1.0 and hl_value < 1.0 and self.current_position is None:
            return (False, "Nothing to withdraw")

        # If nothing on Hyperliquid, we're done - funds already in strategy wallet
        if hl_value < 1.0 and self.current_position is None:
            self.deposit_amount = 0
            return (
                True,
                f"${strategy_usdc:.2f} USDC in strategy wallet ({address}). "
                f"Call exit() to transfer to main wallet.",
            )

        if self.current_position is not None:
            close_success, close_msg = await self._close_position()
            if not close_success:
                return (False, f"Failed to close position: {close_msg}")

        # Wait for spot sale to settle before checking balance
        await asyncio.sleep(5)

        for _transfer_attempt in range(3):
            spot_state_result = await self.hyperliquid_adapter.get_spot_user_state(
                address
            )
            if not spot_state_result[0]:
                continue

            spot_balances = spot_state_result[1].get("balances", [])
            for bal in spot_balances:
                if bal.get("coin") == "USDC":
                    # Use available balance (total - hold), not total
                    total = float(bal.get("total", 0))
                    hold = float(bal.get("hold", 0))
                    available = total - hold

                    # Floor to 2 decimal places to avoid precision issues
                    spot_usdc = math.floor(available * 100) / 100

                    if spot_usdc > 1.0:
                        self.logger.info(
                            f"Transferring ${spot_usdc:.2f} from spot to perp "
                            f"(available={available:.8f}, floored={spot_usdc:.2f})"
                        )
                        (
                            transfer_ok,
                            transfer_result,
                        ) = await self.hyperliquid_adapter.transfer_spot_to_perp(
                            amount=spot_usdc,
                            address=address,
                        )
                        if transfer_ok:
                            self.logger.info("Spot to perp transfer successful")
                        else:
                            self.logger.warning(
                                f"Spot to perp transfer failed: {transfer_result}. "
                                f"Retrying after delay..."
                            )
                            await asyncio.sleep(5)
                            continue
                    break
            break

        # Wait a moment for transfers to settle
        await asyncio.sleep(2)

        withdrawable = 0.0
        for attempt in range(3):
            user_state_result = await self.hyperliquid_adapter.get_user_state(address)
            if not user_state_result[0]:
                continue

            # withdrawable is at top level of user_state, not in marginSummary
            withdrawable = float(user_state_result[1].get("withdrawable", 0))

            if withdrawable > 1.0:
                break

            self.logger.info(
                f"Waiting for funds to be withdrawable (attempt {attempt + 1}/3)..."
            )
            await asyncio.sleep(3)

        if withdrawable <= 0:
            return (False, "No withdrawable funds available")

        self.logger.info(
            f"Withdrawing ${withdrawable:.2f} from Hyperliquid to Arbitrum"
        )
        success, withdraw_result = await self.hyperliquid_adapter.withdraw(
            amount=withdrawable,
            address=address,
        )

        if not success:
            return (False, f"Hyperliquid withdrawal failed: {withdraw_result}")

        self.logger.info(f"Withdrawal initiated: {withdraw_result}")

        # Hyperliquid withdrawals typically take 5-15 minutes
        self.logger.info("Waiting for withdrawal to appear on-chain...")

        (
            withdrawal_success,
            withdrawals,
        ) = await self.hyperliquid_adapter.wait_for_withdrawal(
            address=address,
            lookback_s=5,
            max_poll_time_s=20 * 60,
            poll_interval_s=10,
        )

        if not withdrawal_success or not withdrawals:
            return (
                False,
                f"Withdrawal of ${withdrawable:.2f} initiated but not confirmed on-chain. "
                "Check Hyperliquid explorer for status.",
            )

        tx_hash = list(withdrawals.keys())[-1]
        withdrawn_amount = withdrawals[tx_hash]
        self.logger.info(
            f"Withdrawal confirmed: tx={tx_hash}, amount=${withdrawn_amount:.2f}"
        )

        await asyncio.sleep(10)

        final_balance = 0.0
        try:
            success, balance_data = await self.balance_adapter.get_balance(
                token_id=usdc_token_id,
                wallet_address=address,
            )
            if success:
                final_balance = float(balance_data) / self.TOKEN_DECIMALS
        except Exception as e:
            self.logger.warning(f"Could not get final balance: {e}")

        self.deposit_amount = 0
        self.current_position = None

        return (
            True,
            f"Withdrew ${withdrawn_amount:.2f} from Hyperliquid to strategy wallet ({address}). "
            f"Current balance: ${final_balance:.2f}. Call exit() to transfer to main wallet.",
        )

    async def exit(self, **kwargs) -> StatusTuple:
        self.logger.info("EXIT: Transferring remaining funds to main wallet")

        strategy_address = self._get_strategy_wallet_address()
        main_address = self._get_main_wallet_address()

        if strategy_address.lower() == main_address.lower():
            return (True, "Main wallet is strategy wallet, no transfer needed")

        transferred_items = []

        usdc_ok, usdc_raw = await self.balance_adapter.get_balance(
            token_id=USDC_ARBITRUM_TOKEN_ID,
            wallet_address=strategy_address,
        )
        if usdc_ok and usdc_raw:
            usdc_balance = float(usdc_raw) / self.TOKEN_DECIMALS
            if usdc_balance > 1.0:
                self.logger.info(f"Transferring {usdc_balance:.2f} USDC to main wallet")
                (
                    success,
                    msg,
                ) = await self.balance_adapter.move_from_strategy_wallet_to_main_wallet(
                    token_id=USDC_ARBITRUM_TOKEN_ID,
                    amount=usdc_balance,
                    strategy_name=self.name,
                    skip_ledger=False,
                )
                if success:
                    transferred_items.append(f"{usdc_balance:.2f} USDC")
                else:
                    self.logger.warning(f"USDC transfer failed: {msg}")

        # Transfer ETH (minus reserve for tx fees) to main wallet
        eth_ok, eth_raw = await self.balance_adapter.get_balance(
            token_id="ethereum-arbitrum",
            wallet_address=strategy_address,
        )
        if eth_ok and eth_raw:
            eth_balance = float(eth_raw) / self.GAS_DECIMALS
            tx_fee_reserve = 0.0002
            transferable_eth = eth_balance - tx_fee_reserve
            if transferable_eth > 0.0001:
                self.logger.info(
                    f"Transferring {transferable_eth:.6f} ETH to main wallet"
                )
                (
                    success,
                    msg,
                ) = await self.balance_adapter.move_from_strategy_wallet_to_main_wallet(
                    token_id="ethereum-arbitrum",
                    amount=transferable_eth,
                    strategy_name=self.name,
                    skip_ledger=False,
                )
                if success:
                    transferred_items.append(f"{transferable_eth:.6f} ETH")
                else:
                    self.logger.warning(f"ETH transfer failed: {msg}")

        if not transferred_items:
            return (True, "No funds to transfer to main wallet")

        return (True, f"Transferred to main wallet: {', '.join(transferred_items)}")

    async def _status(self) -> StatusDict:
        total_value, hl_value, vault_value = await self._get_total_portfolio_value()

        status_payload: dict[str, Any] = {
            "has_position": self.current_position is not None,
            "hyperliquid_value": hl_value,
            "vault_wallet_value": vault_value,
        }

        if self.current_position is not None:
            status_payload.update(
                {
                    "coin": self.current_position.coin,
                    "spot_amount": self.current_position.spot_amount,
                    "perp_amount": self.current_position.perp_amount,
                    "entry_price": self.current_position.entry_price,
                    "leverage": self.current_position.leverage,
                    "funding_collected": self.current_position.funding_collected,
                }
            )

        try:
            success, deposit_data = await self.ledger_adapter.get_strategy_net_deposit(
                wallet_address=self._get_strategy_wallet_address()
            )
            net_deposit = (
                float(deposit_data)
                if success and deposit_data is not None
                else self.deposit_amount
            )
        except Exception:
            net_deposit = self.deposit_amount

        (
            success,
            gas_balance_wei,
        ) = await self.balance_adapter.get_balance(
            token_id=self.INFO.gas_token_id,
            wallet_address=self._get_strategy_wallet_address(),
        )

        gas_balance = gas_balance_wei / 1e18

        return StatusDict(
            portfolio_value=total_value,
            net_deposit=float(net_deposit),
            strategy_status=status_payload,
            gas_available=gas_balance,
            gassed_up=self.INFO.gas_threshold <= gas_balance,
        )

    async def ensure_builder_fee_approved(self) -> StatusTuple:
        if not self.hyperliquid_adapter:
            return False, "Hyperliquid adapter not configured"
        address = self._get_strategy_wallet_address()
        if not address:
            return False, "No strategy wallet address"
        return await self.hyperliquid_adapter.ensure_builder_fee_approved(
            address=address,
            builder_fee=self.builder_fee,
        )

    # ------------------------------------------------------------------ #
    # Position Management                                                 #
    # ------------------------------------------------------------------ #

    async def _find_and_open_position(
        self, *, rotation_reason: str | None = None
    ) -> StatusTuple:
        self.logger.info("Analyzing basis trading opportunities...")

        try:
            # Use actual on-exchange USDC (spot + perp) for sizing when opening a fresh position.
            # This handles liquidation scenarios where most USDC ends up in spot.
            perp_margin, spot_usdc = await self._get_undeployed_capital()
            total_usdc = perp_margin + spot_usdc
            if total_usdc > 1.0:
                self.deposit_amount = total_usdc

            best: dict[str, Any] | None = None

            snapshot = self._snapshot_from_config()
            if snapshot is not None:
                try:
                    opps = self.opportunities_from_snapshot(
                        snapshot=snapshot, deposit_usdc=self.deposit_amount
                    )
                    if opps:
                        self.logger.info(
                            "Selecting best opportunity from in-memory batch snapshot"
                        )
                        best = await self.score_opportunity_from_snapshot(
                            opportunity=opps[0],
                            deposit_usdc=self.deposit_amount,
                            horizons_days=[1, 7],
                            stop_frac=self.LIQUIDATION_REBALANCE_THRESHOLD,
                            lookback_days=self.DEFAULT_LOOKBACK_DAYS,
                            fee_eps=self.DEFAULT_FEE_EPS,
                            perp_slippage_bps=1.0,
                            cooloff_hours=0,
                            bootstrap_sims=int(
                                self._cfg_get(
                                    "bootstrap_sims", self.DEFAULT_BOOTSTRAP_SIMS
                                )
                                or self.DEFAULT_BOOTSTRAP_SIMS
                            ),
                            bootstrap_block_hours=int(
                                self._cfg_get(
                                    "bootstrap_block_hours",
                                    self.DEFAULT_BOOTSTRAP_BLOCK_HOURS,
                                )
                                or 0
                            ),
                            bootstrap_seed=self._cfg_get("bootstrap_seed"),
                        )
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning(
                        f"Snapshot selection failed (in-memory): {exc}. Falling back to live solver."
                    )

            snapshot_path = self._snapshot_path_from_config()
            if best is None and snapshot_path and Path(snapshot_path).exists():
                try:
                    snapshot = self.load_snapshot_from_path(snapshot_path)
                    opps = self.opportunities_from_snapshot(
                        snapshot=snapshot, deposit_usdc=self.deposit_amount
                    )
                    if opps:
                        self.logger.info(
                            f"Selecting best opportunity from snapshot {snapshot_path}"
                        )
                        best = await self.score_opportunity_from_snapshot(
                            opportunity=opps[0],
                            deposit_usdc=self.deposit_amount,
                            horizons_days=[1, 7],
                            stop_frac=self.LIQUIDATION_REBALANCE_THRESHOLD,
                            lookback_days=self.DEFAULT_LOOKBACK_DAYS,
                            fee_eps=self.DEFAULT_FEE_EPS,
                            perp_slippage_bps=1.0,
                            cooloff_hours=0,
                            bootstrap_sims=int(
                                self._cfg_get(
                                    "bootstrap_sims", self.DEFAULT_BOOTSTRAP_SIMS
                                )
                                or self.DEFAULT_BOOTSTRAP_SIMS
                            ),
                            bootstrap_block_hours=int(
                                self._cfg_get(
                                    "bootstrap_block_hours",
                                    self.DEFAULT_BOOTSTRAP_BLOCK_HOURS,
                                )
                                or 0
                            ),
                            bootstrap_seed=self._cfg_get("bootstrap_seed"),
                        )
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning(
                        f"Snapshot selection failed ({snapshot_path}): {exc}. "
                        "Falling back to live solver."
                    )

            if best is None:
                best = await self.find_best_trade_with_backtest(
                    deposit_usdc=self.deposit_amount,
                    stop_frac=self.LIQUIDATION_REBALANCE_THRESHOLD,
                    lookback_days=self.DEFAULT_LOOKBACK_DAYS,
                    fee_eps=self.DEFAULT_FEE_EPS,
                    oi_floor=self.DEFAULT_OI_FLOOR,
                    day_vlm_floor=self.DEFAULT_DAY_VLM_FLOOR,
                    horizons_days=[1, 7],
                    max_leverage=self.DEFAULT_MAX_LEVERAGE,
                )

            if not best:
                return (True, "No suitable basis opportunities found at this time.")

            coin = best.get("coin", "unknown")
            safe = best.get("safe", {})

            safe_7 = safe.get("7") or {}
            if safe_7.get("spot_usdc", 0) <= 0 or safe_7.get("perp_amount", 0) <= 0:
                return (True, f"Best opportunity ({coin}) returned zero sizing.")

            leverage = int(safe_7.get("safe_leverage", best.get("best_L", 1)) or 1)
            expected_net_apy_pct = float(best.get("net_apy", 0.0) or 0.0) * 100.0
            target_qty = min(
                float(safe_7.get("spot_amount", 0.0) or 0.0),
                float(safe_7.get("perp_amount", 0.0) or 0.0),
            )
            spot_asset_id = best.get("spot_asset_id", 0)
            perp_asset_id = best.get("perp_asset_id", 0)

            self.logger.info(
                f"Best opportunity: {coin} at {leverage}x leverage, "
                f"expected net APY: {expected_net_apy_pct:.2f}%, target qty: {target_qty}"
            )

            address = self._get_strategy_wallet_address()
            order_usd = float(safe_7.get("spot_usdc", 0.0) or 0.0)
            order_usd = float(
                Decimal(str(order_usd)).quantize(Decimal("0.01"), rounding=ROUND_UP)
            )

            fee_success, fee_msg = await self.ensure_builder_fee_approved()
            if not fee_success:
                return (False, f"Builder fee approval failed: {fee_msg}")

            self.logger.info(f"Setting leverage to {leverage}x for {coin}")
            success, lev_result = await self.hyperliquid_adapter.update_leverage(
                asset_id=perp_asset_id,
                leverage=leverage,
                is_cross=True,
                address=address,
            )
            if not success:
                self.logger.warning(f"Failed to set leverage: {lev_result}")
                # Continue anyway - leverage might already be set

            # Target: spot has order_usd for the spot buy, perp holds the remainder as margin.
            split_ok, split_msg = await self._rebalance_usdc_between_perp_and_spot(
                target_spot_usdc=order_usd,
                address=address,
            )
            if not split_ok:
                self.logger.warning(f"USDC rebalance failed: {split_msg}")

            filler = PairedFiller(
                adapter=self.hyperliquid_adapter,
                address=address,
                cfg=FillConfig(max_slip_bps=35, max_chunk_usd=7500.0),
            )

            (
                spot_filled,
                perp_filled,
                spot_notional,
                perp_notional,
                _spot_pointers,
                _perp_pointers,
            ) = await filler.fill_pair_units(
                coin=coin,
                spot_asset_id=spot_asset_id,
                perp_asset_id=perp_asset_id,
                total_units=target_qty,
                direction="long_spot_short_perp",
                builder_fee=self.builder_fee,
            )

            if spot_filled <= 0 or perp_filled <= 0:
                return (False, f"Failed to fill basis position on {coin}")

            self.logger.info(
                f"Filled basis position: spot={spot_filled:.6f}, perp={perp_filled:.6f}, "
                f"notional=${spot_notional:.2f}/${perp_notional:.2f}"
            )

            mids_result = await self.hyperliquid_adapter.get_all_mid_prices()
            entry_price = (
                self._resolve_mid_price(coin, mids_result[1]) if mids_result[0] else 0.0
            )

            user_state_result = await self.hyperliquid_adapter.get_user_state(address)
            liquidation_price = None
            if user_state_result[0]:
                for pos_wrapper in user_state_result[1].get("assetPositions", []):
                    pos = pos_wrapper.get("position", {})
                    if pos.get("coin") == coin:
                        liquidation_price = float(pos.get("liquidationPx", 0))
                        break

            if liquidation_price and liquidation_price > 0:
                sl_success, sl_msg = await self._place_stop_loss_orders(
                    coin=coin,
                    perp_asset_id=perp_asset_id,
                    position_size=perp_filled,
                    entry_price=entry_price,
                    liquidation_price=liquidation_price,
                    spot_asset_id=spot_asset_id,
                    spot_position_size=spot_filled,
                )
                if not sl_success:
                    self.logger.warning(f"Stop-loss placement failed: {sl_msg}")
            else:
                self.logger.warning("Could not get liquidation price for stop-loss")

            self.current_position = BasisPosition(
                coin=coin,
                spot_asset_id=spot_asset_id,
                perp_asset_id=perp_asset_id,
                spot_amount=spot_filled,
                perp_amount=perp_filled,
                entry_price=entry_price,
                leverage=leverage,
                entry_timestamp=int(time.time() * 1000),
            )

            try:
                await self._record_rotation(
                    coin=coin,
                    spot_asset_id=int(spot_asset_id),
                    perp_asset_id=int(perp_asset_id),
                    spot_units=float(spot_filled),
                    perp_units=float(perp_filled),
                    leverage=int(leverage),
                    reason=rotation_reason,
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.debug(f"Failed to record rotation: {exc}")

            return (
                True,
                f"Opened basis position on {coin}: {spot_filled:.4f} units at {leverage}x, expected net APY: {expected_net_apy_pct:.1f}%",
            )

        except Exception as e:
            self.logger.error(f"Error finding basis opportunities: {e}")
            return (False, f"Analysis failed: {e}")

    # ------------------------------------------------------------------ #
    # Position Scaling                                                    #
    # ------------------------------------------------------------------ #

    async def _unused_usd_now(
        self,
        state: dict[str, Any],
        *,
        mid_prices: dict[str, Any] | None = None,
    ) -> tuple[float, float]:
        """Estimate deployable idle USDC without mistaking perp PnL for new cash.

        `marginSummary.withdrawable` can increase just because the perp leg moves in our favor
        (unrealized PnL), which frees margin but does not represent a fresh cash deposit.
        This helper estimates idle capital by comparing current bankroll to what's allocated
        to the delta-neutral position.

        Returns:
        - unused_usd: estimated deployable idle USDC.
        - bankroll_now: estimated bankroll backing the position (perp equity + spot value + spot USDC).
        """
        if self.current_position is None:
            return 0.0, 0.0

        coin = self.current_position.coin

        perp_position = self._get_perp_position(state)
        if perp_position is None:
            return 0.0, 0.0

        if mid_prices is None:
            success, mids = await self.hyperliquid_adapter.get_all_mid_prices()
            if not success:
                return 0.0, 0.0
            mid_prices = mids

        current_price = float(self._resolve_mid_price(coin, mid_prices) or 0.0)
        if current_price <= 0:
            return 0.0, 0.0

        # Spot side: only track the strategy coin + USDC. (Other balances are ignored for safety.)
        address = self._get_strategy_wallet_address()
        spot_units = 0.0
        spot_usdc = 0.0
        spot_state_result = await self.hyperliquid_adapter.get_spot_user_state(address)
        if spot_state_result[0]:
            for bal in spot_state_result[1].get("balances", []):
                bal_coin = str(bal.get("coin", ""))
                try:
                    total = float(bal.get("total", 0) or 0.0)
                except (TypeError, ValueError):
                    continue

                if bal_coin == "USDC":
                    spot_usdc = total
                elif self._coins_match(bal_coin, coin):
                    spot_units = abs(total)

        spot_value = spot_units * current_price

        # Perp side (account value includes margin + unrealized PnL + funding).
        margin_summary = state.get("marginSummary") or {}
        try:
            perp_equity = float(margin_summary.get("accountValue", 0) or 0.0)
        except (TypeError, ValueError):
            perp_equity = 0.0

        bankroll_now = perp_equity + spot_value + spot_usdc

        # Position sizing inputs.
        try:
            perp_units = abs(float(perp_position.get("szi", 0) or 0.0))
        except (TypeError, ValueError):
            perp_units = 0.0

        if perp_units <= 0 or spot_units <= 0:
            return 0.0, bankroll_now

        entry_px_raw = perp_position.get("entryPx") or self.current_position.entry_price
        try:
            perp_entry_price = float(entry_px_raw or 0.0)
        except (TypeError, ValueError):
            perp_entry_price = 0.0

        if perp_entry_price <= 0:
            return 0.0, bankroll_now

        leverage = float(
            self.current_position.leverage or self.DEFAULT_MAX_LEVERAGE or 1
        )
        lev_raw = perp_position.get("leverage")
        if isinstance(lev_raw, dict):
            try:
                leverage = float(lev_raw.get("value") or leverage)
            except (TypeError, ValueError):
                pass
        elif lev_raw is not None:
            try:
                leverage = float(lev_raw)
            except (TypeError, ValueError):
                pass
        if leverage <= 0:
            leverage = 1.0

        funding_since_open = 0.0
        cum_funding = perp_position.get("cumFunding") or {}
        if isinstance(cum_funding, dict):
            try:
                funding_since_open = abs(float(cum_funding.get("sinceOpen", 0) or 0.0))
            except (TypeError, ValueError):
                funding_since_open = 0.0

        # Margin+PnL contribution of the perp leg for a short at leverage L.
        current_perp_contrib = (
            perp_entry_price * (1.0 + 1.0 / leverage) - current_price
        ) * perp_units
        # Conservative guard: don't let extreme loss create fake "unused" capital.
        current_perp_contrib = max(0.0, current_perp_contrib)

        allocated_now = current_perp_contrib + spot_value + funding_since_open
        unused_usd = max(0.0, bankroll_now - allocated_now)

        return unused_usd, bankroll_now

    async def _get_undeployed_capital(self) -> tuple[float, float]:
        address = self._get_strategy_wallet_address()

        user_state_result = await self.hyperliquid_adapter.get_user_state(address)
        if not user_state_result[0]:
            return 0.0, 0.0

        # Hyperliquid userState commonly nests withdrawable under marginSummary, but keep
        # compatibility with any top-level "withdrawable" shape.
        withdrawable_val = user_state_result[1].get("withdrawable")
        if withdrawable_val is None:
            margin_summary = user_state_result[1].get("marginSummary") or {}
            if isinstance(margin_summary, dict):
                withdrawable_val = margin_summary.get("withdrawable")

        withdrawable = float(withdrawable_val or 0.0)

        spot_state_result = await self.hyperliquid_adapter.get_spot_user_state(address)
        spot_usdc = 0.0
        if spot_state_result[0]:
            for bal in spot_state_result[1].get("balances", []):
                if bal.get("coin") == "USDC":
                    spot_usdc = float(bal.get("total", 0))
                    break

        return withdrawable, spot_usdc

    async def _rebalance_usdc_between_perp_and_spot(
        self,
        *,
        target_spot_usdc: float,
        address: str,
    ) -> tuple[bool, str]:
        if target_spot_usdc <= 0:
            return False, "Target spot USDC must be positive"

        perp_margin, spot_usdc = await self._get_undeployed_capital()
        total_usdc = perp_margin + spot_usdc
        if total_usdc <= 0:
            return False, "No deployable USDC on Hyperliquid"

        # Operate at cent precision to avoid dust churn.
        eps = 0.01
        target = float(
            Decimal(str(target_spot_usdc)).quantize(Decimal("0.01"), rounding=ROUND_UP)
        )

        if target > total_usdc + eps:
            return (
                False,
                f"Target spot ${target:.2f} exceeds total deployable ${total_usdc:.2f}",
            )

        delta = target - spot_usdc
        if abs(delta) < eps:
            return True, "Spot/perp USDC already balanced"

        if delta > 0:
            # Need more spot USDC: move from perp to spot.
            amount = float(
                Decimal(str(min(delta, perp_margin))).quantize(
                    Decimal("0.01"), rounding=ROUND_UP
                )
            )
            if amount < eps:
                return True, "No meaningful perp->spot transfer needed"
            success, result = await self.hyperliquid_adapter.transfer_perp_to_spot(
                amount=amount,
                address=address,
            )
            if not success:
                return False, f"Perp->spot transfer failed: {result}"
            return True, f"Transferred ${amount:.2f} perp->spot"

        # Need more perp USDC: move from spot to perp.
        amount = float(
            Decimal(str(min(-delta, spot_usdc))).quantize(
                Decimal("0.01"), rounding=ROUND_DOWN
            )
        )
        if amount < eps:
            return True, "No meaningful spot->perp transfer needed"
        success, result = await self.hyperliquid_adapter.transfer_spot_to_perp(
            amount=amount,
            address=address,
        )
        if not success:
            return False, f"Spot->perp transfer failed: {result}"
        return True, f"Transferred ${amount:.2f} spot->perp"

    async def _scale_up_position(self, additional_capital: float) -> StatusTuple:
        if self.current_position is None:
            return False, "No position to scale up"

        pos = self.current_position
        address = self._get_strategy_wallet_address()

        leverage = pos.leverage or 2

        # order_usd = capital * (L / (L + 1)) for leveraged position
        order_usd = additional_capital * (leverage / (leverage + 1))

        success, mids = await self.hyperliquid_adapter.get_all_mid_prices()
        if not success:
            return False, "Failed to get mid prices"

        price = self._resolve_mid_price(pos.coin, mids)
        if price <= 0:
            return False, f"Invalid price for {pos.coin}"

        if order_usd < MIN_NOTIONAL_USD:
            return (
                True,
                f"Additional capital ${order_usd:.2f} below minimum notional ${MIN_NOTIONAL_USD}",
            )

        units_to_add = order_usd / price

        spot_valid = self.hyperliquid_adapter.get_valid_order_size(
            pos.spot_asset_id, units_to_add
        )
        perp_valid = self.hyperliquid_adapter.get_valid_order_size(
            pos.perp_asset_id, units_to_add
        )
        units_to_add = min(spot_valid, perp_valid)

        if units_to_add <= 0:
            return (
                True,
                "Additional capital rounds to zero units after decimal adjustment",
            )

        self.logger.info(
            f"Scaling up {pos.coin} position: adding {units_to_add:.4f} units "
            f"(${order_usd:.2f}) at {leverage}x leverage"
        )

        # Target: spot has order_usd for the spot buy, perp holds the remainder as margin.
        split_ok, split_msg = await self._rebalance_usdc_between_perp_and_spot(
            target_spot_usdc=order_usd,
            address=address,
        )
        if not split_ok:
            self.logger.warning(f"USDC rebalance failed: {split_msg}")

        filler = PairedFiller(
            adapter=self.hyperliquid_adapter,
            address=address,
            cfg=FillConfig(max_slip_bps=35, max_chunk_usd=7500.0),
        )

        try:
            (
                spot_filled,
                perp_filled,
                spot_notional,
                perp_notional,
                _,
                _,
            ) = await filler.fill_pair_units(
                coin=pos.coin,
                spot_asset_id=pos.spot_asset_id,
                perp_asset_id=pos.perp_asset_id,
                total_units=units_to_add,
                direction="long_spot_short_perp",
                builder_fee=self.builder_fee,
            )
        except Exception as e:
            self.logger.error(f"PairedFiller failed: {e}")
            return False, f"Failed to scale position: {e}"

        if spot_filled <= 0 or perp_filled <= 0:
            return False, f"Failed to add to position on {pos.coin}"

        self.current_position = BasisPosition(
            coin=pos.coin,
            spot_asset_id=pos.spot_asset_id,
            perp_asset_id=pos.perp_asset_id,
            spot_amount=pos.spot_amount + spot_filled,
            perp_amount=pos.perp_amount + perp_filled,
            entry_price=price,
            leverage=leverage,
            entry_timestamp=pos.entry_timestamp,
            funding_collected=pos.funding_collected,
        )

        self.logger.info(
            f"Scaled up position: +{spot_filled:.4f} spot, +{perp_filled:.4f} perp. "
            f"Total now: {self.current_position.spot_amount:.4f} / {self.current_position.perp_amount:.4f}"
        )

        return (
            True,
            f"Added {spot_filled:.4f} {pos.coin} to position (${spot_notional:.2f})",
        )

    async def _monitor_position(self) -> StatusTuple:
        if self.current_position is None:
            return (True, "No position to monitor")

        pos = self.current_position
        coin = pos.coin
        address = self._get_strategy_wallet_address()
        actions_taken: list[str] = []

        success, state = await self.hyperliquid_adapter.get_user_state(address)
        if not success:
            return (False, f"Failed to fetch user state: {state}")

        total_value, hl_value, _ = await self._get_total_portfolio_value()

        # ------------------------------------------------------------------ #
        # Emergency: Near-liquidation risk management                         #
        # ------------------------------------------------------------------ #
        near_liq, near_msg = await self._is_near_liquidation(state)
        if near_liq:
            self.logger.warning(f"Near liquidation on {coin}: {near_msg}")
            # Close both legs (sell spot, buy perp) and redeploy into a fresh position.
            # This bypasses rotation cooldown because it's an emergency safety action.
            close_success, close_msg = await self._close_position()
            if not close_success:
                return (
                    False,
                    f"Emergency rebalance failed - could not close: {close_msg}",
                )
            return await self._find_and_open_position(
                rotation_reason="near_liquidation"
            )

        needs_rebalance, reason = await self._needs_new_position(state, hl_value)

        if needs_rebalance:
            rotation_allowed, cooldown_reason = await self._is_rotation_allowed()

            if not rotation_allowed:
                self.logger.info(f"Rebalance needed ({reason}) but {cooldown_reason}")
                return (
                    True,
                    f"Position needs attention but in cooldown: {cooldown_reason}",
                )

            self.logger.info(f"Rebalancing position: {reason}")
            close_success, close_msg = await self._close_position()
            if not close_success:
                return (False, f"Rebalance failed - could not close: {close_msg}")

            return await self._find_and_open_position(rotation_reason=str(reason))

        leg_ok, leg_msg = await self._verify_leg_balance(state)
        if not leg_ok:
            self.logger.warning(f"Leg imbalance detected: {leg_msg}")
            repair_ok, repair_msg = await self._repair_leg_imbalance(state)
            if repair_ok:
                actions_taken.append(f"Repaired leg imbalance: {repair_msg}")
            else:
                actions_taken.append(f"Leg imbalance repair failed: {repair_msg}")

        unused_usd, bankroll_now = await self._unused_usd_now(state)
        min_deploy = max(self.MIN_UNUSED_USD, self.UNUSED_REL_EPS * bankroll_now)

        if unused_usd > min_deploy:
            self.logger.info(
                f"Found ${unused_usd:.2f} deployable idle USDC, scaling up position"
            )
            scale_ok, scale_msg = await self._scale_up_position(unused_usd)
            if scale_ok:
                actions_taken.append(f"Scaled up: {scale_msg}")
                # Refresh state after scale-up so stop-loss uses new position size/liq price
                success, state = await self.hyperliquid_adapter.get_user_state(address)
                if not success:
                    self.logger.warning("Could not refresh state after scale-up")
            else:
                actions_taken.append(f"Scale-up failed: {scale_msg}")

        sl_ok, sl_msg = await self._ensure_stop_loss_valid(state)
        if not sl_ok:
            actions_taken.append(f"Stop-loss issue: {sl_msg}")
        elif "placed" in sl_msg.lower() or "updated" in sl_msg.lower():
            actions_taken.append(sl_msg)

        position_age_hours = (time.time() * 1000 - pos.entry_timestamp) / (1000 * 3600)
        rotation_hint = ""
        try:
            rotation_hint = await self._rotation_cooldown_hint()
        except Exception as exc:  # noqa: BLE001
            self.logger.debug(f"Could not compute rotation cooldown hint: {exc}")

        if actions_taken:
            suffix = f". Rotation: {rotation_hint}" if rotation_hint else ""
            return (
                True,
                f"Position on {coin} monitored, age: {position_age_hours:.1f}h. Actions: {'; '.join(actions_taken)}{suffix}",
            )

        suffix = f". Rotation: {rotation_hint}" if rotation_hint else ""
        return (
            True,
            f"Position on {coin} healthy, age: {position_age_hours:.1f}h{suffix}",
        )

    async def _is_near_liquidation(self, state: dict[str, Any]) -> tuple[bool, str]:
        if self.current_position is None:
            return False, "No position"

        coin = self.current_position.coin

        perp_pos = None
        for pos_wrapper in state.get("assetPositions", []):
            pos = pos_wrapper.get("position", {})
            if pos.get("coin") == coin:
                perp_pos = pos
                break

        if not perp_pos:
            return False, "No perp position found"

        try:
            szi = float(perp_pos.get("szi", 0) or 0)
        except (TypeError, ValueError):
            szi = 0.0

        # Only applies to short perps (basis trade is short perp).
        if szi >= 0:
            return False, "Perp is not short"

        entry_px_raw = perp_pos.get("entryPx") or self.current_position.entry_price
        liq_px_raw = perp_pos.get("liquidationPx")
        try:
            entry_px = float(entry_px_raw or 0)
            liq_px = float(liq_px_raw or 0)
        except (TypeError, ValueError):
            return False, "Missing entry/liquidation price"

        if entry_px <= 0 or liq_px <= 0 or liq_px <= entry_px:
            return False, "Invalid entry/liquidation prices"

        success, mids = await self.hyperliquid_adapter.get_all_mid_prices()
        if not success:
            return False, "Failed to fetch mid prices"

        mid_px = float(self._resolve_mid_price(coin, mids) or 0.0)
        if mid_px <= 0:
            return False, "Missing mid price"

        denom = liq_px - entry_px
        frac = (mid_px - entry_px) / denom if denom != 0 else 0.0

        if frac >= self.LIQUIDATION_REBALANCE_THRESHOLD:
            return (
                True,
                f"mid=${mid_px:.4f} is {frac:.2%} of the way from entry=${entry_px:.4f} to liq=${liq_px:.4f}",
            )

        return (
            False,
            f"mid=${mid_px:.4f} is {frac:.2%} of the way from entry=${entry_px:.4f} to liq=${liq_px:.4f}",
        )

    async def _verify_leg_balance(self, state: dict[str, Any]) -> tuple[bool, str]:
        if self.current_position is None:
            return True, "No position"

        pos = self.current_position
        coin = pos.coin

        perp_size = 0.0
        for pos_wrapper in state.get("assetPositions", []):
            position = pos_wrapper.get("position", {})
            if position.get("coin") == coin:
                perp_size = abs(float(position.get("szi", 0)))
                break

        address = self._get_strategy_wallet_address()
        spot_state_result = await self.hyperliquid_adapter.get_spot_user_state(address)
        spot_size = 0.0
        if spot_state_result[0]:
            for bal in spot_state_result[1].get("balances", []):
                if self._coins_match(bal.get("coin", ""), coin):
                    spot_size = float(bal.get("total", 0))
                    break

        if spot_size <= 0 and perp_size <= 0:
            return False, "Both legs are zero"

        max_size = max(spot_size, perp_size)
        if max_size > 0:
            imbalance_pct = abs(spot_size - perp_size) / max_size
            if imbalance_pct > 0.02:
                return (
                    False,
                    f"Imbalance: spot={spot_size:.6f}, perp={perp_size:.6f} ({imbalance_pct * 100:.1f}%)",
                )

        self.current_position = BasisPosition(
            coin=pos.coin,
            spot_asset_id=pos.spot_asset_id,
            perp_asset_id=pos.perp_asset_id,
            spot_amount=spot_size,
            perp_amount=perp_size,
            entry_price=pos.entry_price,
            leverage=pos.leverage,
            entry_timestamp=pos.entry_timestamp,
            funding_collected=pos.funding_collected,
        )

        return True, f"Balanced: spot={spot_size:.6f}, perp={perp_size:.6f}"

    async def _repair_leg_imbalance(self, state: dict[str, Any]) -> tuple[bool, str]:
        if self.current_position is None:
            return True, "No position"

        pos = self.current_position
        coin = pos.coin
        address = self._get_strategy_wallet_address()

        perp_size = 0.0
        for pos_wrapper in state.get("assetPositions", []):
            position = pos_wrapper.get("position", {})
            if position.get("coin") == coin:
                perp_size = abs(float(position.get("szi", 0)))
                break

        spot_state_result = await self.hyperliquid_adapter.get_spot_user_state(address)
        spot_size = 0.0
        if spot_state_result[0]:
            for bal in spot_state_result[1].get("balances", []):
                if self._coins_match(bal.get("coin", ""), coin):
                    spot_size = float(bal.get("total", 0))
                    break

        diff = abs(spot_size - perp_size)
        if diff < 0.001:
            return True, "Legs already balanced"

        mids_result = await self.hyperliquid_adapter.get_all_mid_prices()
        if not mids_result[0]:
            return False, "Failed to get mid prices"
        price = self._resolve_mid_price(coin, mids_result[1])
        if price <= 0:
            return False, f"Invalid price for {coin}"

        diff_usd = diff * price
        if diff_usd < 10:
            return True, f"Imbalance ${diff_usd:.2f} below minimum notional"

        try:
            if spot_size > perp_size:
                # Need more perp (short more)
                self.logger.info(
                    f"Repairing imbalance: shorting {diff:.6f} {coin} perp"
                )
                success, result = await self.hyperliquid_adapter.place_market_order(
                    asset_id=pos.perp_asset_id,
                    is_buy=False,
                    slippage=0.01,
                    size=self.hyperliquid_adapter.get_valid_order_size(
                        pos.perp_asset_id, diff
                    ),
                    address=address,
                    builder=self.builder_fee,
                )
                if not success:
                    return False, f"Failed to add perp: {result}"
                return True, f"Added {diff:.6f} perp short"
            else:
                # Need more spot (buy more)
                self.logger.info(f"Repairing imbalance: buying {diff:.6f} {coin} spot")
                success, result = await self.hyperliquid_adapter.place_market_order(
                    asset_id=pos.spot_asset_id,
                    is_buy=True,
                    slippage=0.01,
                    size=self.hyperliquid_adapter.get_valid_order_size(
                        pos.spot_asset_id, diff
                    ),
                    address=address,
                    builder=self.builder_fee,
                )
                if not success:
                    return False, f"Failed to add spot: {result}"
                return True, f"Added {diff:.6f} spot"
        except Exception as e:
            return False, f"Repair failed: {e}"

    async def _ensure_stop_loss_valid(self, state: dict[str, Any]) -> tuple[bool, str]:
        if self.current_position is None:
            return True, "No position"

        pos = self.current_position
        coin = pos.coin

        perp_size = 0.0
        liquidation_price = None
        entry_price = pos.entry_price

        for pos_wrapper in state.get("assetPositions", []):
            position = pos_wrapper.get("position", {})
            if position.get("coin") == coin:
                perp_size = abs(float(position.get("szi", 0)))
                liquidation_price = float(position.get("liquidationPx", 0))
                entry_px = position.get("entryPx")
                if entry_px:
                    entry_price = float(entry_px)
                break

        if perp_size <= 0:
            return True, "No perp position to protect"

        if not liquidation_price or liquidation_price <= 0:
            return False, "Could not determine liquidation price"

        # to ensure stop-loss covers the actual spot holdings
        spot_position = await self._get_spot_position()
        if spot_position:
            spot_size = float(spot_position.get("total", 0))
        else:
            spot_size = pos.spot_amount

        return await self._place_stop_loss_orders(
            coin=coin,
            perp_asset_id=pos.perp_asset_id,
            position_size=perp_size,
            entry_price=entry_price,
            liquidation_price=liquidation_price,
            spot_asset_id=pos.spot_asset_id,
            spot_position_size=spot_size,
        )

    async def _cancel_all_position_orders(self) -> None:
        if self.current_position is None:
            return

        pos = self.current_position
        address = self._get_strategy_wallet_address()
        spot_coin = (
            f"@{pos.spot_asset_id - 10000}" if pos.spot_asset_id >= 10000 else None
        )

        open_orders_result = await self.hyperliquid_adapter.get_frontend_open_orders(
            address
        )
        if not open_orders_result[0]:
            self.logger.warning("Could not fetch open orders to cancel")
            return

        for order in open_orders_result[1]:
            order_coin = order.get("coin", "")
            order_id = order.get("oid")

            if order_coin == pos.coin and order_id:
                self.logger.info(f"Canceling perp order {order_id} for {pos.coin}")
                await self.hyperliquid_adapter.cancel_order(
                    asset_id=pos.perp_asset_id,
                    order_id=order_id,
                    address=address,
                )

            if spot_coin and order_coin == spot_coin and order_id:
                self.logger.info(f"Canceling spot order {order_id} for {spot_coin}")
                await self.hyperliquid_adapter.cancel_order(
                    asset_id=pos.spot_asset_id,
                    order_id=order_id,
                    address=address,
                )

    async def _close_position(self) -> StatusTuple:
        if self.current_position is None:
            return (True, "No position to close")

        pos = self.current_position
        self.logger.info(f"Closing position on {pos.coin}")

        await self._cancel_all_position_orders()

        try:
            address = self._get_strategy_wallet_address()
            filler = PairedFiller(
                adapter=self.hyperliquid_adapter,
                address=address,
                cfg=FillConfig(max_slip_bps=50, max_chunk_usd=7500.0),
            )

            # Close by going opposite direction: sell spot, buy perp
            close_units = max(pos.spot_amount, pos.perp_amount)
            (
                spot_closed,
                perp_closed,
                spot_notional,
                perp_notional,
                _,
                _,
            ) = await filler.fill_pair_units(
                coin=pos.coin,
                spot_asset_id=pos.spot_asset_id,
                perp_asset_id=pos.perp_asset_id,
                total_units=close_units,
                direction="short_spot_long_perp",
                builder_fee=self.builder_fee,
            )

            if spot_closed <= 0 and perp_closed <= 0:
                self.logger.warning(
                    f"Position close may be incomplete: spot={spot_closed}, perp={perp_closed}"
                )

            self.logger.info(
                f"Closed position: spot={spot_closed:.6f}, perp={perp_closed:.6f}"
            )
            self.current_position = None
            return (True, f"Closed position on {pos.coin}")

        except Exception as e:
            self.logger.error(f"Error closing position: {e}")
            return (False, f"Failed to close position: {e}")

    # ------------------------------------------------------------------ #
    # Position Health Checks                                              #
    # ------------------------------------------------------------------ #

    async def _needs_new_position(
        self,
        state: dict[str, Any],
        deposited_amount: float,
        best: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        perp_position = self._get_perp_position(state)
        spot_position = await self._get_spot_position()

        if perp_position is None or spot_position is None:
            return True, "Missing perp or spot position"

        if best:
            if perp_position.get("asset_id") != best.get("perp_asset_id"):
                return True, "Perp asset mismatch"
            if spot_position.get("asset_id") != best.get("spot_asset_id"):
                return True, "Spot asset mismatch"

        funding_earned = self._get_funding_earned(state)
        if funding_earned > deposited_amount * self.FUNDING_REBALANCE_THRESHOLD:
            return True, f"Funding earned {funding_earned:.2f} exceeds threshold"

        perp_size = float(perp_position.get("szi", 0))
        if perp_size >= 0:
            return True, "Perp position is not short"

        spot_size = abs(float(spot_position.get("total", 0)))
        perp_size_abs = abs(perp_size)
        lower = spot_size * (1 - self.SPOT_POSITION_DUST_TOLERANCE)
        upper = spot_size * (1 + self.SPOT_POSITION_DUST_TOLERANCE)
        if not (lower <= perp_size_abs <= upper):
            return True, f"Position imbalance: spot={spot_size}, perp={perp_size_abs}"

        # Note: Unused capital is handled by _scale_up_position() in _monitor_position's
        # Check 3, NOT here. We should never trigger a full rebalance just because
        # there's idle capital - that should be added to the existing position.

        # Note: Stop-loss validation is handled separately in _monitor_position's
        # without triggering a full rebalance.

        return False, "Position healthy"

    def _get_perp_position(self, state: dict[str, Any]) -> dict[str, Any] | None:
        if self.current_position is None:
            return None

        asset_positions = state.get("assetPositions", [])
        for pos_wrapper in asset_positions:
            pos = pos_wrapper.get("position", {})
            coin = pos.get("coin")
            if coin == self.current_position.coin:
                pos["asset_id"] = self.current_position.perp_asset_id
                return pos

        return None

    async def _get_spot_position(self) -> dict[str, Any] | None:
        if self.current_position is None:
            return None

        address = self._get_strategy_wallet_address()
        spot_state_result = await self.hyperliquid_adapter.get_spot_user_state(address)
        if not spot_state_result[0]:
            return None

        balances = spot_state_result[1].get("balances", [])
        for bal in balances:
            coin = bal.get("coin", "")
            if self._coins_match(coin, self.current_position.coin):
                bal["asset_id"] = self.current_position.spot_asset_id
                return bal

        return None

    def _get_funding_earned(self, state: dict[str, Any]) -> float:
        if self.current_position is None:
            return 0.0

        asset_positions = state.get("assetPositions", [])
        for pos_wrapper in asset_positions:
            pos = pos_wrapper.get("position", {})
            if pos.get("coin") == self.current_position.coin:
                return abs(float(pos.get("cumFunding", {}).get("sinceOpen", 0)))

        return 0.0

    async def _place_stop_loss_orders(
        self,
        coin: str,
        perp_asset_id: int,
        position_size: float,
        entry_price: float,
        liquidation_price: float,
        spot_asset_id: int | None = None,
        spot_position_size: float | None = None,
    ) -> tuple[bool, str]:
        address = self._get_strategy_wallet_address()

        if spot_asset_id is None or spot_position_size is None:
            if self.current_position:
                spot_asset_id = self.current_position.spot_asset_id
                spot_position_size = self.current_position.spot_amount
            else:
                spot_asset_id = None
                spot_position_size = 0.0

        # For short perp, liquidation is ABOVE entry price
        stop_loss_price = (
            entry_price
            + (liquidation_price - entry_price) * self.LIQUIDATION_STOP_LOSS_THRESHOLD
        )
        # Round to 5 significant figures to avoid SDK float_to_wire precision errors
        stop_loss_price = float(f"{stop_loss_price:.5g}")

        open_orders_result = await self.hyperliquid_adapter.get_frontend_open_orders(
            address
        )

        has_valid_perp_stop = False
        has_valid_spot_limit = False
        orders_to_cancel = []

        # Spot coin name for matching (e.g., "@4" for HYPE spot)
        spot_coin = (
            f"@{spot_asset_id - 10000}"
            if spot_asset_id and spot_asset_id >= 10000
            else None
        )

        if open_orders_result[0]:
            for order in open_orders_result[1]:
                order_coin = order.get("coin", "")
                order_id = order.get("oid")
                is_trigger = order.get("isTrigger", False)
                order_type = str(order.get("orderType", "")).lower()
                is_sell = order.get("side", "").upper() == "A"

                if order_coin == coin:
                    is_trigger_order = (
                        is_trigger or "stop" in order_type or "trigger" in order_type
                    )

                    if is_trigger_order:
                        existing_trigger = float(order.get("triggerPx", 0))
                        existing_size = float(order.get("sz", 0))

                        if (
                            existing_trigger < liquidation_price
                            and existing_size >= position_size * 0.95
                            and not has_valid_perp_stop
                        ):
                            # First valid perp stop-loss found
                            has_valid_perp_stop = True
                            self.logger.info(
                                f"Valid perp stop-loss exists for {coin} at {existing_trigger} "
                                f"(size: {existing_size})"
                            )
                        else:
                            # Invalid or duplicate - mark for cancellation
                            if order_id:
                                orders_to_cancel.append(
                                    (perp_asset_id, order_id, "perp stop-loss")
                                )

                if spot_coin and order_coin == spot_coin and is_sell:
                    # This is a spot sell order (could be our stop-loss limit)
                    existing_price = float(order.get("limitPx", 0))
                    existing_size = float(order.get("sz", 0))

                    price_match = (
                        abs(existing_price - stop_loss_price) / stop_loss_price < 0.05
                    )
                    # Spot limit must cover at least 99% of spot holdings
                    size_valid = existing_size >= (spot_position_size or 0) * 0.99

                    if price_match and size_valid and not has_valid_spot_limit:
                        # First valid spot limit sell found
                        has_valid_spot_limit = True
                        self.logger.info(
                            f"Valid spot limit sell exists for {spot_coin} at {existing_price} "
                            f"(size: {existing_size})"
                        )
                    elif not is_trigger:
                        # Invalid or duplicate spot limit - mark for cancellation
                        # But only cancel if it's a limit order (not trigger)
                        if order_id:
                            orders_to_cancel.append(
                                (spot_asset_id, order_id, "spot limit")
                            )

        for asset_id, order_id, order_desc in orders_to_cancel:
            self.logger.info(f"Canceling {order_desc} order {order_id}")
            await self.hyperliquid_adapter.cancel_order(
                asset_id=asset_id,
                order_id=order_id,
                address=address,
            )

        if not has_valid_perp_stop:
            success, result = await self.hyperliquid_adapter.place_stop_loss(
                asset_id=perp_asset_id,
                is_buy=True,
                trigger_price=stop_loss_price,
                size=position_size,
                address=address,
            )
            if not success:
                return False, f"Failed to place perp stop-loss: {result}"
            self.logger.info(f"Placed perp stop-loss at {stop_loss_price} for {coin}")

        if (
            spot_asset_id
            and spot_position_size
            and spot_position_size > 0
            and not has_valid_spot_limit
        ):
            spot_sell_size = self.hyperliquid_adapter.get_valid_order_size(
                spot_asset_id, spot_position_size
            )
            if spot_sell_size > 0:
                success, result = await self.hyperliquid_adapter.place_limit_order(
                    asset_id=spot_asset_id,
                    is_buy=False,
                    price=stop_loss_price,
                    size=spot_sell_size,
                    address=address,
                    reduce_only=False,
                )
                if not success:
                    self.logger.warning(f"Failed to place spot limit sell: {result}")
                else:
                    self.logger.info(
                        f"Placed spot limit sell at {stop_loss_price} for {spot_coin} "
                        f"(size: {spot_sell_size})"
                    )

        return True, "Stop-loss orders verified/placed"

    # ------------------------------------------------------------------ #
    # Rotation Cooldown                                                   #
    # ------------------------------------------------------------------ #

    async def _record_rotation(
        self,
        *,
        coin: str,
        spot_asset_id: int,
        perp_asset_id: int,
        spot_units: float,
        perp_units: float,
        leverage: int,
        reason: str | None = None,
    ) -> None:
        """Write a rotation marker to the local ledger so cooldown survives restarts."""
        if not self.ledger_adapter:
            return

        wallet_address = self._get_strategy_wallet_address()
        op_data: dict[str, Any] = {
            "type": "BASIS_ROTATION",
            "coin": str(coin),
            "spot_asset_id": int(spot_asset_id),
            "perp_asset_id": int(perp_asset_id),
            "spot_units": float(spot_units),
            "perp_units": float(perp_units),
            "leverage": int(leverage),
        }
        if reason:
            op_data["reason"] = str(reason)

        try:
            await self.ledger_adapter.ledger_client.add_strategy_operation(
                wallet_address=wallet_address,
                operation_data=op_data,
                usd_value="0",
                strategy_name=self.name or "basis_trading_strategy",
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.debug(f"Failed to write BASIS_ROTATION to ledger: {exc}")

    async def _get_last_rotation_time(self) -> datetime | None:
        wallet_address = self._get_strategy_wallet_address()

        try:
            success, transactions = await self.ledger_adapter.get_strategy_transactions(
                wallet_address=wallet_address,
                limit=50,
            )
            if not success or not transactions:
                return None

            tx_list = (
                transactions.get("transactions", [])
                if isinstance(transactions, dict)
                else []
            )
            for txn in tx_list:
                op_data = txn.get("op_data") or {}
                op_type = str(
                    txn.get("operation")
                    or (op_data.get("type") if isinstance(op_data, dict) else "")
                    or ""
                )
                if op_type != "BASIS_ROTATION":
                    continue

                created_str = txn.get("created") or txn.get("timestamp")
                if created_str:
                    return datetime.fromisoformat(
                        str(created_str).replace("Z", "+00:00")
                    )

            return None
        except Exception as e:
            self.logger.warning(f"Could not get last rotation time: {e}")
            return None

    async def _is_rotation_allowed(self) -> tuple[bool, str]:
        if self.current_position is None:
            return True, "No existing position"

        last_rotation = await self._get_last_rotation_time()
        if last_rotation is None:
            return True, "No prior rotation found"

        now = datetime.now(UTC)
        # Ensure last_rotation is timezone-aware
        if last_rotation.tzinfo is None:
            last_rotation = last_rotation.replace(tzinfo=UTC)

        cooldown = timedelta(days=self.ROTATION_MIN_INTERVAL_DAYS)

        unlock_at = last_rotation + cooldown
        remaining = unlock_at - now

        if remaining <= timedelta(0):
            return True, "Cooldown passed"

        days = remaining.days
        hours = remaining.seconds // 3600
        unlock_str = unlock_at.strftime("%Y-%m-%d %H:%M UTC")
        return (
            False,
            f"Rotation cooldown: {days}d {hours}h remaining (unlocks {unlock_str})",
        )

    async def _rotation_cooldown_hint(self) -> str:
        """Human-readable rotation cooldown status for update() output."""
        if self.current_position is None:
            return ""

        last_rotation = await self._get_last_rotation_time()
        if last_rotation is None:
            return "unlocked (cooldown inactive; no rotation recorded)"

        now = datetime.now(UTC)
        if last_rotation.tzinfo is None:
            last_rotation = last_rotation.replace(tzinfo=UTC)

        cooldown = timedelta(days=self.ROTATION_MIN_INTERVAL_DAYS)
        unlock_at = last_rotation + cooldown
        remaining = unlock_at - now
        if remaining <= timedelta(0):
            return "unlocked (cooldown passed)"

        days = remaining.days
        hours = remaining.seconds // 3600
        unlock_str = unlock_at.strftime("%Y-%m-%d %H:%M UTC")
        return f"{days}d {hours}h remaining (unlocks {unlock_str})"

    # ------------------------------------------------------------------ #
    # Live Portfolio Value                                                #
    # ------------------------------------------------------------------ #

    async def _get_total_portfolio_value(self) -> tuple[float, float, float]:
        address = self._get_strategy_wallet_address()

        hl_value = 0.0
        user_state_result = await self.hyperliquid_adapter.get_user_state(address)
        if user_state_result[0]:
            margin_summary = user_state_result[1].get("marginSummary", {})
            hl_value = float(margin_summary.get("accountValue", 0))

            spot_state_result = await self.hyperliquid_adapter.get_spot_user_state(
                address
            )
            if spot_state_result[0]:
                spot_balances = spot_state_result[1].get("balances", [])
                mid_prices: dict[str, float] = {}
                if any(bal.get("coin") != "USDC" for bal in spot_balances):
                    mids_result = await self.hyperliquid_adapter.get_all_mid_prices()
                    if mids_result[0]:
                        mid_prices = mids_result[1]

                for bal in spot_balances:
                    coin = bal.get("coin", "")
                    total = float(bal.get("total", 0))
                    if total <= 0:
                        continue

                    if coin == "USDC":
                        # USDC is 1:1
                        hl_value += total
                    else:
                        mid_price = self._resolve_mid_price(coin, mid_prices)
                        if mid_price > 0:
                            hl_value += total * mid_price
                        else:
                            self.logger.debug(
                                f"No mid price found for spot {coin}, skipping"
                            )

        strategy_wallet_value = 0.0
        try:
            strategy_address = self._get_strategy_wallet_address()
            success, balance = await self.balance_adapter.get_balance(
                token_id=USDC_ARBITRUM_TOKEN_ID,
                wallet_address=strategy_address,
            )
            if success and balance:
                strategy_wallet_value = float(balance) / self.TOKEN_DECIMALS
        except Exception as e:
            self.logger.debug(f"Could not fetch strategy wallet balance: {e}")

        total_value = hl_value + strategy_wallet_value
        return total_value, hl_value, strategy_wallet_value

    # ------------------------------------------------------------------ #
    # Analysis Methods                                                    #
    # ------------------------------------------------------------------ #

    def _find_basis_candidates(
        self,
        spot_pairs: list[dict],
        idx_to_token: dict[int, str],
        perps_set: set[str],
    ) -> list[tuple[str, str, int]]:
        candidates: list[tuple[str, str, int]] = []

        for pe in spot_pairs:
            base_idx = pe["tokens"][0]
            quote_idx = pe["tokens"][1]
            base = idx_to_token.get(base_idx)
            quote = idx_to_token.get(quote_idx)

            if quote != "USDC":
                continue

            if not base or not quote:
                continue

            spot_pair_name = f"{base}/{quote}"
            spot_asset_id = pe["index"] + 10000

            base_norm = (
                base[1:] if (base.startswith("U") and base[1:] in perps_set) else base
            )
            if base_norm in perps_set:
                candidates.append((spot_pair_name, base_norm, spot_asset_id))

        return candidates

    async def _filter_by_liquidity(
        self,
        candidates: list[tuple[str, str, int]],
        coin_to_ctx: dict[str, Any],
        coin_to_maxlev: dict[str, int],
        coin_to_margin_table: dict[str, int | None],
        deposit_usdc: float,
        max_leverage: int,
        oi_floor: float,
        day_vlm_floor: float,
        perp_coin_to_asset_id: dict[str, int],
        depth_params: dict[str, Any] | None = None,
    ) -> list[BasisCandidate]:
        liquid: list[BasisCandidate] = []

        if deposit_usdc <= 0:
            return liquid

        for spot_sym, coin, spot_asset_id in candidates:
            ctx = coin_to_ctx.get(coin, {})
            oi_base = float(ctx.get("openInterest") or 0.0)
            mark_px = float(ctx.get("markPx") or 0.0)

            if mark_px <= 0:
                continue

            perp_asset_id = perp_coin_to_asset_id.get(coin)
            if perp_asset_id is None:
                continue

            margin_table_id = coin_to_margin_table.get(coin)
            oi_usd = oi_base * mark_px
            day_ntl_usd = float(ctx.get("dayNtlVlm") or 0.0)

            if oi_usd < oi_floor or day_ntl_usd < day_vlm_floor:
                continue

            raw_max_lev = coin_to_maxlev.get(coin, max_leverage)
            coin_max_lev = int(raw_max_lev) if raw_max_lev else max_leverage
            target_leverage = max(1, min(max_leverage, coin_max_lev))
            order_usd = deposit_usdc * (target_leverage / (target_leverage + 1))

            if order_usd <= 0:
                continue

            try:
                book_snapshot = await self._l2_book_spot(
                    spot_asset_id,
                    fallback_mid=mark_px,
                    spot_symbol=spot_sym,
                )
            except Exception as exc:
                self.logger.warning(f"Skipping {spot_sym}: L2 fetch error: {exc}")
                continue

            buy_check = await self.check_spot_depth_ok(
                spot_asset_id,
                order_usd,
                "buy",
                day_ntl_usd=day_ntl_usd,
                params=depth_params,
                book=book_snapshot,
            )
            sell_check = await self.check_spot_depth_ok(
                spot_asset_id,
                order_usd,
                "sell",
                day_ntl_usd=day_ntl_usd,
                params=depth_params,
                book=book_snapshot,
            )

            if not (buy_check.get("pass") and sell_check.get("pass")):
                continue

            depth_checks = {"buy": buy_check, "sell": sell_check}

            liquid.append(
                BasisCandidate(
                    coin=coin,
                    spot_pair=spot_sym,
                    spot_asset_id=spot_asset_id,
                    perp_asset_id=perp_asset_id,
                    mark_price=mark_px,
                    target_leverage=target_leverage,
                    ctx=ctx,
                    spot_book=book_snapshot,
                    open_interest_base=oi_base,
                    open_interest_usd=oi_usd,
                    day_notional_usd=day_ntl_usd,
                    order_usd=order_usd,
                    depth_checks=depth_checks,
                    margin_table_id=margin_table_id,
                )
            )

        return liquid

    # ------------------------------------------------------------------ #
    # Chunked Data Fetching                                               #
    # ------------------------------------------------------------------ #
    # Net APY Solver + Bootstrap                                          #
    # ------------------------------------------------------------------ #

    def _spot_index_from_asset_id(self, spot_asset_id: int) -> int:
        return hl_spot_index_from_asset_id(spot_asset_id)

    def _normalize_l2_book(
        self,
        raw: dict[str, Any],
        *,
        fallback_mid: float | None = None,
    ) -> dict[str, Any]:
        return hl_normalize_l2_book(raw, fallback_mid=fallback_mid)

    async def _l2_book_spot(
        self,
        spot_asset_id: int,
        *,
        fallback_mid: float | None = None,
        spot_symbol: str | None = None,
    ) -> dict[str, Any]:
        last_exc: Exception | None = None

        try:
            success, raw = await self.hyperliquid_adapter.get_spot_l2_book(
                spot_asset_id
            )
            if success and isinstance(raw, dict):
                return self._normalize_l2_book(raw, fallback_mid=fallback_mid)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc

        # Fallback: try spot pair naming conventions
        # - Index 0: use "PURR/USDC"
        # - Other indices: use "@{index}"
        spot_index = self._spot_index_from_asset_id(spot_asset_id)
        if spot_index == 0:
            candidates = ["PURR/USDC"]
        else:
            candidates = [f"@{spot_index}"]

        # Also try the spot_symbol if provided (e.g., "HYPE/USDC")
        if spot_symbol:
            candidates.append(spot_symbol)

        seen: set[str] = set()
        for coin in candidates:
            if not coin or coin in seen:
                continue
            seen.add(coin)
            try:
                # Use get_l2_book which accepts spot pair names like "PURR/USDC" or "@107"
                success, raw = await self.hyperliquid_adapter.get_l2_book(coin)
                if success and isinstance(raw, dict):
                    return self._normalize_l2_book(raw, fallback_mid=fallback_mid)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                continue

        if last_exc is not None:
            raise last_exc
        raise ValueError(f"Unable to fetch L2 book for spot asset {spot_asset_id}")

    def _usd_depth_in_band(
        self, book: dict[str, Any], band_bps: int, side: str
    ) -> tuple[float, float]:
        return hl_usd_depth_in_band(book, band_bps, side)

    def _depth_band_for_size(
        self,
        order_usd: float,
        *,
        base_bps: int = 20,
        max_bps: int = 100,
        gamma: int = 20,
    ) -> int:
        if order_usd <= 0:
            return base_bps

        band = base_bps + int(gamma * max(0.0, math.log10(order_usd / 1e4)))
        band = max(base_bps, band)
        return min(band, max_bps)

    async def check_spot_depth_ok(
        self,
        spot_asset_id: int,
        order_usd: float,
        side: str,
        *,
        day_ntl_usd: float | None = None,
        params: dict[str, Any] | None = None,
        book: dict[str, Any] | None = None,
        fallback_mid: float | None = None,
        spot_symbol: str | None = None,
    ) -> dict[str, Any]:
        config: dict[str, Any] = {
            "base_band_bps": 50,
            "max_band_bps": 100,
            "band_gamma": 20,
            "max_fill_ratio": 0.10,
            "depth_multiple": 2.0,
            "min_depth_floor_usd": 10_000.0,
            "day_frac_cap": 0.005,
        }
        if params:
            config.update(params)

        try:
            book_snapshot = (
                book
                if book is not None
                else await self._l2_book_spot(
                    spot_asset_id, fallback_mid=fallback_mid, spot_symbol=spot_symbol
                )
            )
        except Exception as exc:  # noqa: BLE001
            dyn_min_depth = max(
                float(config["min_depth_floor_usd"]),
                float(config["depth_multiple"]) * float(order_usd),
            )
            return {
                "pass": False,
                "side": side,
                "order_usd": float(order_usd),
                "mid_px": 0.0,
                "band_bps": int(config["base_band_bps"]),
                "depth_side_usd": 0.0,
                "max_fill_ratio": float(config["max_fill_ratio"]),
                "depth_multiple": float(config["depth_multiple"]),
                "min_depth_floor_usd": float(config["min_depth_floor_usd"]),
                "dyn_min_depth_usd": float(dyn_min_depth),
                "max_allowed_by_depth": 0.0,
                "day_ntl_usd": day_ntl_usd,
                "day_frac_cap": float(config["day_frac_cap"]),
                "max_allowed_by_turnover": None,
                "reasons": [
                    f"failed to fetch L2 book for spot_asset_id {spot_asset_id}: {exc}"
                ],
            }

        band_bps = self._depth_band_for_size(
            order_usd,
            base_bps=int(config["base_band_bps"]),
            max_bps=int(config["max_band_bps"]),
            gamma=int(config["band_gamma"]),
        )

        depth_side_usd, mid = self._usd_depth_in_band(book_snapshot, band_bps, side)

        dyn_min_depth = max(
            float(config["min_depth_floor_usd"]),
            float(config["depth_multiple"]) * float(order_usd),
        )

        max_allowed_by_depth = float(config["max_fill_ratio"]) * float(depth_side_usd)
        depth_ok = (
            float(depth_side_usd) >= dyn_min_depth
            and float(order_usd) <= max_allowed_by_depth
            and float(depth_side_usd) > 0.0
        )

        turnover_ok = True
        max_allowed_by_turnover: float | None = None
        if day_ntl_usd is not None and day_ntl_usd > 0:
            max_allowed_by_turnover = float(config["day_frac_cap"]) * float(day_ntl_usd)
            turnover_ok = float(order_usd) <= max_allowed_by_turnover

        reasons: list[str] = []
        if float(depth_side_usd) < dyn_min_depth:
            reasons.append(
                f"insufficient book depth in band (need ≥ {dyn_min_depth:,.2f})"
            )
        if float(order_usd) > max_allowed_by_depth:
            reasons.append("order size exceeds depth-based cap")
        if not turnover_ok:
            reasons.append("exceeds daily turnover cap")

        return {
            "pass": bool(depth_ok and turnover_ok),
            "side": side,
            "order_usd": float(order_usd),
            "mid_px": float(mid),
            "band_bps": int(band_bps),
            "depth_side_usd": float(depth_side_usd),
            "depth_multiple": float(config["depth_multiple"]),
            "min_depth_floor_usd": float(config["min_depth_floor_usd"]),
            "dyn_min_depth_usd": float(dyn_min_depth),
            "max_fill_ratio": float(config["max_fill_ratio"]),
            "max_allowed_by_depth": float(max_allowed_by_depth),
            "day_ntl_usd": day_ntl_usd,
            "day_frac_cap": float(config["day_frac_cap"]),
            "max_allowed_by_turnover": max_allowed_by_turnover,
            "reasons": reasons,
        }

    def _estimate_spot_slippage_usd(
        self,
        book: dict[str, Any],
        order_usd: float,
        side: str,
        band_bps: int,
    ) -> float:
        depth_usd, _mid = self._usd_depth_in_band(book, band_bps, side)
        if order_usd <= 0 or depth_usd <= 0:
            return 0.0
        fill_fraction = min(1.0, order_usd / depth_usd)
        return fill_fraction * (band_bps * 0.5 / 1e4) * order_usd

    async def _estimate_cycle_costs(
        self,
        *,
        N_leg_usd: float,
        spot_asset_id: int,
        spot_book: dict[str, Any],
        fee_model: dict[str, float] | None = None,
        depth_params: dict[str, Any] | None = None,
        perp_slippage_bps: float = 1.0,
        day_ntl_usd: float | None = None,
        spot_symbol: str | None = None,
    ) -> tuple[float, float, dict[str, float], dict[str, dict[str, Any]]]:
        cfg_fees = {"spot_bps": 9.0, "perp_bps": 6.0}
        if fee_model:
            cfg_fees.update(fee_model)

        buy_chk = await self.check_spot_depth_ok(
            spot_asset_id,
            N_leg_usd,
            "buy",
            day_ntl_usd=day_ntl_usd,
            params=depth_params,
            book=spot_book,
            spot_symbol=spot_symbol,
        )
        sell_chk = await self.check_spot_depth_ok(
            spot_asset_id,
            N_leg_usd,
            "sell",
            day_ntl_usd=day_ntl_usd,
            params=depth_params,
            book=spot_book,
            spot_symbol=spot_symbol,
        )

        band_buy = int(buy_chk.get("band_bps", 50))
        band_sell = int(sell_chk.get("band_bps", 50))

        spot_slip_entry = 0.5 * (
            self._estimate_spot_slippage_usd(spot_book, N_leg_usd, "buy", band_buy)
            + self._estimate_spot_slippage_usd(spot_book, N_leg_usd, "sell", band_sell)
        )
        spot_slip_exit = spot_slip_entry

        spot_fee_entry = (cfg_fees["spot_bps"] / 1e4) * N_leg_usd
        spot_fee_exit = (cfg_fees["spot_bps"] / 1e4) * N_leg_usd
        perp_fee_entry = (cfg_fees["perp_bps"] / 1e4) * N_leg_usd
        perp_fee_exit = (cfg_fees["perp_bps"] / 1e4) * N_leg_usd

        perp_slip_entry = (perp_slippage_bps / 1e4) * N_leg_usd
        perp_slip_exit = (perp_slippage_bps / 1e4) * N_leg_usd

        entry_cost = spot_slip_entry + spot_fee_entry + perp_slip_entry + perp_fee_entry
        exit_cost = spot_slip_exit + spot_fee_exit + perp_slip_exit + perp_fee_exit

        breakdown = {
            "spot_slip_entry": spot_slip_entry,
            "spot_slip_exit": spot_slip_exit,
            "spot_fee_entry": spot_fee_entry,
            "spot_fee_exit": spot_fee_exit,
            "perp_slip_entry": perp_slip_entry,
            "perp_slip_exit": perp_slip_exit,
            "perp_fee_entry": perp_fee_entry,
            "perp_fee_exit": perp_fee_exit,
            "band_bps_buy": float(band_buy),
            "band_bps_sell": float(band_sell),
            "depth_usd_buy": float(buy_chk.get("depth_side_usd", 0.0)),
            "depth_usd_sell": float(sell_chk.get("depth_side_usd", 0.0)),
        }
        return entry_cost, exit_cost, breakdown, {"buy": buy_chk, "sell": sell_chk}

    async def _get_margin_table_tiers(self, table_id: int) -> list[dict[str, float]]:
        if table_id in self._margin_table_cache:
            return [dict(t) for t in self._margin_table_cache[table_id]]

        if not hasattr(self.hyperliquid_adapter, "get_margin_table"):
            self._margin_table_cache[table_id] = []
            return []

        try:
            success, response = await self.hyperliquid_adapter.get_margin_table(
                int(table_id)
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"Failed to fetch margin table {table_id}: {exc}")
            self._margin_table_cache[table_id] = []
            return []

        if not success or not isinstance(response, dict):
            self._margin_table_cache[table_id] = []
            return []

        tiers_raw = response.get("marginTiers") or []
        tiers_sorted = sorted(
            (
                {
                    "lowerBound": float(tier.get("lowerBound", 0.0) or 0.0),
                    "maxLeverage": float(tier.get("maxLeverage", 0.0) or 0.0),
                }
                for tier in tiers_raw
                if isinstance(tier, dict)
            ),
            key=lambda t: t["lowerBound"],
        )

        processed: list[dict[str, float]] = []
        deduction = 0.0
        prev_rate: float | None = None

        for tier in tiers_sorted:
            lower = max(0.0, tier["lowerBound"])
            max_lev = tier["maxLeverage"]
            if max_lev <= 0.0:
                continue

            maint_rate = 1.0 / (2.0 * max_lev)
            if prev_rate is not None:
                deduction += lower * (maint_rate - prev_rate)
            processed.append(
                {
                    "lower_bound": float(lower),
                    "maint_rate": float(maint_rate),
                    "deduction": float(deduction),
                }
            )
            prev_rate = maint_rate

        self._margin_table_cache[table_id] = [dict(t) for t in processed]
        return [dict(t) for t in processed]

    def maintenance_fraction_for_notional(
        self,
        margin_table_id: int | None,
        notional_usd: float,
        fallback_max_leverage: int,
    ) -> float:
        fallback_mmr = self.maintenance_rate_from_max_leverage(
            max(1, int(fallback_max_leverage))
        )
        notional = float(notional_usd)
        if notional <= 0 or not margin_table_id:
            return fallback_mmr

        tiers = self._margin_table_cache.get(int(margin_table_id)) or []
        if not tiers:
            return fallback_mmr

        chosen = tiers[0]
        for tier in tiers:
            if notional >= float(tier["lower_bound"]):
                chosen = tier
            else:
                break

        maint_rate = float(chosen["maint_rate"])
        deduction = float(chosen["deduction"])
        maintenance_margin = maint_rate * notional - deduction
        if maintenance_margin <= 0:
            return max(maint_rate, fallback_mmr)

        fraction = maintenance_margin / notional
        return max(min(float(fraction), 1.0), 0.0)

    def _first_stop_horizon(
        self,
        *,
        start_idx: int,
        closes: list[float],
        highs: list[float],
        hourly_funding: list[float],
        leverage: int,
        stop_frac: float,
        fee_eps: float,
        maintenance_fn,
        base_notional: float,
    ) -> int:
        n = min(len(closes), len(highs), len(hourly_funding)) - 1
        if start_idx >= n:
            return 0

        entry = closes[start_idx]
        if entry <= 0:
            return 1

        peak = entry
        cum_neg_f = 0.0
        max_j = n - start_idx

        if not (0.0 < stop_frac <= 1.0):
            raise ValueError(f"stop_frac must be in (0, 1], got {stop_frac}")

        L = max(1, int(leverage))
        threshold = stop_frac * (1.0 / float(L))

        for j in range(1, max_j + 1):
            idx = start_idx + j
            h = highs[idx]
            if h > peak:
                peak = h

            runup = (peak / entry) - 1.0
            r = hourly_funding[idx]
            if r < 0.0:
                cum_neg_f += (-r) * (1.0 + runup)

            notional = base_notional * (1.0 + runup)
            maintenance_fraction = float(maintenance_fn(notional))
            req = maintenance_fraction * (1.0 + runup) + runup + cum_neg_f + fee_eps
            if req >= threshold:
                return j

        return max_j

    def _simulate_barrier_backtest(
        self,
        *,
        funding: list[float],
        closes: list[float],
        highs: list[float],
        leverage: int,
        stop_frac: float,
        fee_eps: float,
        N_leg_usd: float,
        entry_cost_usd: float,
        exit_cost_usd: float,
        margin_table_id: int | None,
        fallback_max_leverage: int,
        cooloff_hours: int = 0,
    ) -> dict[str, float]:
        n = min(len(funding), len(closes), len(highs)) - 1
        if n <= 0:
            return {
                "net_pnl_usd": 0.0,
                "gross_funding_usd": 0.0,
                "cycles": 0,
                "hours": 0,
                "hours_in_market": 0,
            }

        pnl = 0.0
        gross_funding = 0.0
        cycles = 0
        t = 0
        hours_in_market = 0

        def maintenance_fn(notional: float) -> float:
            return self.maintenance_fraction_for_notional(
                margin_table_id,
                notional,
                fallback_max_leverage,
            )

        while t < n:
            pnl -= entry_cost_usd
            cycles += 1

            j = self._first_stop_horizon(
                start_idx=t,
                closes=closes,
                highs=highs,
                hourly_funding=funding,
                leverage=leverage,
                stop_frac=stop_frac,
                fee_eps=fee_eps,
                maintenance_fn=maintenance_fn,
                base_notional=N_leg_usd,
            )
            j = max(1, min(j, n - t))

            entry_px = closes[t] if 0 <= t < len(closes) else 0.0
            funding_sum = 0.0
            for k in range(1, j + 1):
                idx = t + k
                funding_rate = funding[idx] if idx < len(funding) else 0.0
                if entry_px > 0:
                    px = closes[idx] if idx < len(closes) else entry_px
                    px_ratio = px / entry_px
                else:
                    px_ratio = 1.0
                funding_sum += funding_rate * px_ratio

            funding_usd = N_leg_usd * funding_sum
            pnl += funding_usd
            gross_funding += funding_usd
            hours_in_market += j

            t += j
            if t >= n:
                break

            pnl -= exit_cost_usd
            if cooloff_hours > 0:
                t += cooloff_hours

        return {
            "net_pnl_usd": float(pnl),
            "gross_funding_usd": float(gross_funding),
            "cycles": float(cycles),
            "hours": float(n),
            "hours_in_market": float(hours_in_market),
        }

    @staticmethod
    def _percentile(sorted_values: list[float], pct: float) -> float:
        return analytics_percentile(sorted_values, pct)

    def _block_bootstrap_paths(
        self,
        *,
        funding: list[float],
        closes: list[float],
        highs: list[float],
        block_hours: int,
        sims: int,
        rng: random.Random,
    ) -> list[tuple[list[float], list[float], list[float]]]:
        paths = analytics_block_bootstrap_paths(
            funding,
            closes,
            highs,
            block_hours=block_hours,
            sims=sims,
            rng=rng,
        )
        return [(f, c, h) for (f, c, h) in paths]

    def _bootstrap_churn_metrics(
        self,
        *,
        funding: list[float],
        closes: list[float],
        highs: list[float],
        leverage: int,
        stop_frac: float,
        fee_eps: float,
        N_leg_usd: float,
        entry_cost_usd: float,
        exit_cost_usd: float,
        margin_table_id: int | None,
        fallback_max_leverage: int,
        cooloff_hours: int,
        deposit_usdc: float,
        sims: int,
        block_hours: int,
        seed: int | None,
    ) -> dict[str, Any] | None:
        if sims <= 0 or deposit_usdc <= 0:
            return None

        base_len = min(len(funding), len(closes), len(highs))
        if base_len <= 1:
            return None

        rng_seed = seed if seed is not None else random.randrange(1 << 30)
        rng = random.Random(rng_seed)

        paths = self._block_bootstrap_paths(
            funding=funding,
            closes=closes,
            highs=highs,
            block_hours=block_hours,
            sims=sims,
            rng=rng,
        )
        if not paths:
            return None

        net_apy_samples: list[float] = []
        gross_apy_samples: list[float] = []
        time_in_market_samples: list[float] = []
        hit_rate_samples: list[float] = []
        avg_hold_samples: list[float] = []
        cycles_samples: list[float] = []

        for f_boot, c_boot, h_boot in paths:
            sim_res = self._simulate_barrier_backtest(
                funding=f_boot,
                closes=c_boot,
                highs=h_boot,
                leverage=leverage,
                stop_frac=stop_frac,
                fee_eps=fee_eps,
                N_leg_usd=N_leg_usd,
                entry_cost_usd=entry_cost_usd,
                exit_cost_usd=exit_cost_usd,
                margin_table_id=margin_table_id,
                fallback_max_leverage=fallback_max_leverage,
                cooloff_hours=cooloff_hours,
            )

            hours = max(1.0, float(sim_res["hours"]))
            years = hours / (24.0 * 365.0)
            net_apy = (float(sim_res["net_pnl_usd"]) / max(1e-9, deposit_usdc)) / years
            gross_apy = (
                float(sim_res["gross_funding_usd"]) / max(1e-9, deposit_usdc)
            ) / years
            hit_rate_per_day = (
                float(sim_res["cycles"]) / (hours / 24.0) if hours > 0 else 0.0
            )
            avg_hold_hours = (
                float(sim_res["hours_in_market"]) / max(1.0, float(sim_res["cycles"]))
                if float(sim_res["cycles"]) > 0
                else hours
            )
            time_in_market = float(sim_res["hours_in_market"]) / hours

            net_apy_samples.append(net_apy)
            gross_apy_samples.append(gross_apy)
            time_in_market_samples.append(time_in_market)
            hit_rate_samples.append(hit_rate_per_day)
            avg_hold_samples.append(avg_hold_hours)
            cycles_samples.append(float(sim_res["cycles"]))

        if not net_apy_samples:
            return None

        def summarize(values: list[float]) -> dict[str, float]:
            ordered = sorted(values)
            return {
                "mean": float(fmean(ordered)),
                "p05": self._percentile(ordered, 0.05),
                "p25": self._percentile(ordered, 0.25),
                "p50": self._percentile(ordered, 0.50),
                "p75": self._percentile(ordered, 0.75),
                "p95": self._percentile(ordered, 0.95),
            }

        return {
            "samples": len(net_apy_samples),
            "block_hours": int(block_hours),
            "seed": int(rng_seed),
            "net_apy": summarize(net_apy_samples),
            "gross_funding_apy": summarize(gross_apy_samples),
            "time_in_market_frac": summarize(time_in_market_samples),
            "hit_rate_per_day": summarize(hit_rate_samples),
            "avg_hold_hours": summarize(avg_hold_samples),
            "cycles": summarize(cycles_samples),
        }

    def _buffer_requirement_tiered(
        self,
        *,
        closes: list[float],
        highs: list[float],
        hourly_funding: list[float],
        window: int,
        margin_table_id: int | None,
        base_notional: float,
        fallback_max_leverage: int,
        fee_eps: float,
        require_full_window: bool = True,
    ) -> float:
        fallback_mmr = self.maintenance_rate_from_max_leverage(
            max(1, int(fallback_max_leverage))
        )
        if base_notional <= 0:
            return float(fallback_mmr + fee_eps)

        n = min(len(closes), len(highs), len(hourly_funding))
        if n == 0 or window <= 0:
            return float(fallback_mmr + fee_eps)

        i_max = (n - 1 - window) if require_full_window else (n - 2)
        if i_max < 0:
            return float(fallback_mmr + fee_eps)

        worst_req = 0.0

        for i in range(0, i_max + 1):
            entry = closes[i]
            if entry <= 0:
                continue

            peak = entry
            cum_f = 0.0

            for j in range(1, window + 1):
                idx = i + j
                h = highs[idx]
                if h > peak:
                    peak = h

                runup = (peak / entry) - 1.0
                r = hourly_funding[idx]
                if r < 0.0:
                    cum_f += (-r) * (1.0 + runup)

                notional = base_notional * (1.0 + runup)
                maintenance_fraction = self.maintenance_fraction_for_notional(
                    margin_table_id,
                    notional,
                    fallback_max_leverage,
                )
                req = maintenance_fraction * (1.0 + runup) + runup + cum_f + fee_eps
                if req > worst_req:
                    worst_req = req

        return worst_req if worst_req > 0 else float(fallback_mmr + fee_eps)

    def _size_step(self, asset_id: int) -> Decimal:
        try:
            mapping = self.hyperliquid_adapter.asset_to_sz_decimals
        except Exception as exc:  # noqa: BLE001
            raise ValueError("Hyperliquid asset_to_sz_decimals not available") from exc

        if not isinstance(mapping, dict):
            raise ValueError(f"Unknown asset_id {asset_id}: missing szDecimals")
        return hl_size_step(mapping, asset_id)

    def round_size_for_hypecore_asset(
        self, asset_id: int, size: float | Decimal, *, ensure_min_step: bool = False
    ) -> float:
        try:
            mapping = self.hyperliquid_adapter.asset_to_sz_decimals
        except Exception as exc:  # noqa: BLE001
            raise ValueError("Hyperliquid asset_to_sz_decimals not available") from exc

        if not isinstance(mapping, dict):
            raise ValueError(f"Unknown asset_id {asset_id}: missing szDecimals")
        return hl_round_size_for_asset(
            mapping, asset_id, size, ensure_min_step=ensure_min_step
        )

    def _common_unit_step(
        self, spot_asset_id: int, perp_asset_id: int | None
    ) -> Decimal:
        step_spot = self._size_step(spot_asset_id)
        step_perp = (
            self._size_step(perp_asset_id) if perp_asset_id is not None else step_spot
        )
        return max(step_spot, step_perp)

    def _min_deposit_needed(
        self,
        *,
        mark_price: float | Decimal,
        leverage: int,
        spot_asset_id: int,
        perp_asset_id: int | None,
    ) -> float:
        L = max(1, int(leverage))
        unit_step = self._common_unit_step(spot_asset_id, perp_asset_id)
        mark = _d(mark_price)
        N = unit_step * mark
        Dmin = N * (_d(1) + (_d(1) / _d(L)))
        return float(Dmin)

    def _depth_upper_bound_usd(
        self,
        *,
        book: dict[str, Any],
        side: str,
        day_ntl_usd: float | None,
        params: dict[str, Any] | None,
    ) -> float:
        config: dict[str, Any] = {
            "max_band_bps": 100,
            "max_fill_ratio": 0.10,
            "depth_multiple": 2.0,
            "min_depth_floor_usd": 10_000.0,
            "day_frac_cap": 0.005,
        }
        if params:
            config.update(params)

        max_band = int(config["max_band_bps"])
        depth_side_usd, _mid = self._usd_depth_in_band(book, max_band, side)

        if depth_side_usd <= 0.0 or depth_side_usd < float(
            config["min_depth_floor_usd"]
        ):
            return 0.0

        cap_depth = min(
            float(config["max_fill_ratio"]) * float(depth_side_usd),
            float(depth_side_usd) / max(1e-9, float(config["depth_multiple"])),
        )
        cap_turnover = (
            float("inf")
            if day_ntl_usd is None or float(day_ntl_usd) <= 0.0
            else float(config["day_frac_cap"]) * float(day_ntl_usd)
        )
        return float(max(0.0, min(cap_depth, cap_turnover)))

    @staticmethod
    def _order_scan_points(upper: float, *, growth: float = 1.8) -> list[float]:
        if upper <= 0:
            return []
        if upper <= 1.0:
            return [float(upper)]
        pts: list[float] = []
        v = 1.0
        while v < upper:
            pts.append(float(v))
            v *= float(growth)
            if len(pts) > 256:
                break
        pts.append(float(upper))
        return sorted({float(p) for p in pts if p > 0.0})

    async def max_spot_order_usd_for_book(
        self,
        *,
        spot_asset_id: int,
        spot_symbol: str | None,
        book: dict[str, Any],
        day_ntl_usd: float,
        params: dict[str, Any] | None = None,
        refine_iters: int = 12,
    ) -> dict[str, Any]:
        upper_buy = self._depth_upper_bound_usd(
            book=book, side="buy", day_ntl_usd=day_ntl_usd, params=params
        )
        upper_sell = self._depth_upper_bound_usd(
            book=book, side="sell", day_ntl_usd=day_ntl_usd, params=params
        )
        upper = min(upper_buy, upper_sell)
        if upper <= 0.0:
            return {
                "max_order_usd": 0.0,
                "upper_bound_usd": float(upper),
                "checks": {"buy": None, "sell": None},
            }

        scan_orders = self._order_scan_points(upper)
        best = 0.0
        best_checks: dict[str, Any] | None = None

        for order_usd in scan_orders:
            buy = await self.check_spot_depth_ok(
                spot_asset_id,
                float(order_usd),
                "buy",
                day_ntl_usd=day_ntl_usd,
                params=params,
                book=book,
                spot_symbol=spot_symbol,
            )
            sell = await self.check_spot_depth_ok(
                spot_asset_id,
                float(order_usd),
                "sell",
                day_ntl_usd=day_ntl_usd,
                params=params,
                book=book,
                spot_symbol=spot_symbol,
            )
            if bool(buy.get("pass")) and bool(sell.get("pass")):
                best = float(order_usd)
                best_checks = {"buy": buy, "sell": sell}

        if best <= 0.0:
            # No scan point passed. Provide a diagnostic at the smallest order tested.
            first = float(scan_orders[0])
            buy = await self.check_spot_depth_ok(
                spot_asset_id,
                first,
                "buy",
                day_ntl_usd=day_ntl_usd,
                params=params,
                book=book,
                spot_symbol=spot_symbol,
            )
            sell = await self.check_spot_depth_ok(
                spot_asset_id,
                first,
                "sell",
                day_ntl_usd=day_ntl_usd,
                params=params,
                book=book,
                spot_symbol=spot_symbol,
            )
            return {
                "max_order_usd": 0.0,
                "upper_bound_usd": float(upper),
                "checks": {"buy": buy, "sell": sell},
            }

        # If the upper bound itself passes, we're done.
        if best >= float(upper) - 1e-9:
            return {
                "max_order_usd": float(upper),
                "upper_bound_usd": float(upper),
                "checks": best_checks or {"buy": None, "sell": None},
            }

        # Find a failing point above best to bracket the threshold.
        bracket_high = float(upper)
        for order_usd in scan_orders:
            if float(order_usd) <= best:
                continue
            buy = await self.check_spot_depth_ok(
                spot_asset_id,
                float(order_usd),
                "buy",
                day_ntl_usd=day_ntl_usd,
                params=params,
                book=book,
                spot_symbol=spot_symbol,
            )
            sell = await self.check_spot_depth_ok(
                spot_asset_id,
                float(order_usd),
                "sell",
                day_ntl_usd=day_ntl_usd,
                params=params,
                book=book,
                spot_symbol=spot_symbol,
            )
            if not (bool(buy.get("pass")) and bool(sell.get("pass"))):
                bracket_high = float(order_usd)
                break

        low = float(best)
        high = float(bracket_high)
        for _ in range(max(0, int(refine_iters))):
            if high - low <= 1e-6:
                break
            mid = (low + high) / 2.0
            buy = await self.check_spot_depth_ok(
                spot_asset_id,
                float(mid),
                "buy",
                day_ntl_usd=day_ntl_usd,
                params=params,
                book=book,
                spot_symbol=spot_symbol,
            )
            sell = await self.check_spot_depth_ok(
                spot_asset_id,
                float(mid),
                "sell",
                day_ntl_usd=day_ntl_usd,
                params=params,
                book=book,
                spot_symbol=spot_symbol,
            )
            if bool(buy.get("pass")) and bool(sell.get("pass")):
                low = float(mid)
                best_checks = {"buy": buy, "sell": sell}
            else:
                high = float(mid)

        return {
            "max_order_usd": float(low),
            "upper_bound_usd": float(upper),
            "checks": best_checks or {"buy": None, "sell": None},
        }

    async def solve_candidates_max_net_apy_with_stop(
        self,
        *,
        deposit_usdc: float,
        stop_frac: float = 0.75,
        lookback_days: int = 45,
        oi_floor: float = 50.0,
        day_vlm_floor: float = 1e5,
        max_leverage: int = 3,
        fee_eps: float = 0.003,
        fee_model: dict[str, float] | None = None,
        depth_params: dict[str, Any] | None = None,
        perp_slippage_bps: float = 1.0,
        cooloff_hours: int = 0,
        coin_whitelist: list[str] | None = None,
        bootstrap_sims: int = DEFAULT_BOOTSTRAP_SIMS,
        bootstrap_block_hours: int = DEFAULT_BOOTSTRAP_BLOCK_HOURS,
        bootstrap_seed: int | None = None,
    ) -> list[dict[str, Any]]:
        if deposit_usdc <= 0:
            return []

        max_hours = 5000
        lookback_days = min(int(lookback_days), max_hours // 24)

        (
            success,
            perps_ctx_pack,
        ) = await self.hyperliquid_adapter.get_meta_and_asset_ctxs()
        if not success:
            raise ValueError(f"Failed to fetch perp metadata: {perps_ctx_pack}")

        perps_meta_list = perps_ctx_pack[0]["universe"]
        perps_ctxs = perps_ctx_pack[1]

        coin_to_ctx: dict[str, Any] = {}
        coin_to_maxlev: dict[str, int] = {}
        coin_to_margin_table: dict[str, int | None] = {}
        coins: list[str] = []
        for meta, ctx in zip(perps_meta_list, perps_ctxs, strict=False):
            coin = meta["name"]
            coin_to_ctx[coin] = ctx
            coin_to_maxlev[coin] = int(meta.get("maxLeverage", 10))
            coin_to_margin_table[coin] = meta.get("marginTableId")
            coins.append(coin)
        perps_set = set(coins)

        perp_coin_to_asset_id = {
            k: v for k, v in self.hyperliquid_adapter.coin_to_asset.items() if v < 10000
        }

        success, spot_meta = await self.hyperliquid_adapter.get_spot_meta()
        if not success:
            raise ValueError(f"Failed to fetch spot metadata: {spot_meta}")

        tokens = spot_meta.get("tokens", [])
        spot_pairs = spot_meta.get("universe", [])
        idx_to_token = {t["index"]: t["name"] for t in tokens}

        candidates = self._find_basis_candidates(spot_pairs, idx_to_token, perps_set)

        liquid_candidates = await self._filter_by_liquidity(
            candidates=candidates,
            coin_to_ctx=coin_to_ctx,
            coin_to_maxlev=coin_to_maxlev,
            coin_to_margin_table=coin_to_margin_table,
            deposit_usdc=deposit_usdc,
            max_leverage=max_leverage,
            oi_floor=oi_floor,
            day_vlm_floor=day_vlm_floor,
            perp_coin_to_asset_id=perp_coin_to_asset_id,
            depth_params=depth_params,
        )

        whitelist = (
            {coin.upper() for coin in coin_whitelist} if coin_whitelist else None
        )
        if whitelist is not None:
            liquid_candidates = [
                candidate
                for candidate in liquid_candidates
                if candidate.coin.upper() in whitelist
            ]
            if not liquid_candidates:
                return []

        if not liquid_candidates:
            return []

        ms_now = int(time.time() * 1000)
        start_ms = ms_now - int(lookback_days * 24 * 3600 * 1000)

        ranked: list[dict[str, Any]] = []

        for candidate in liquid_candidates:
            coin = candidate.coin
            spot_sym = candidate.spot_pair
            spot_asset_id = candidate.spot_asset_id
            perp_asset_id = candidate.perp_asset_id
            spot_book = candidate.spot_book
            mark_px = float(candidate.mark_price)
            max_available_lev = max(1, int(candidate.target_leverage))
            margin_table_id = candidate.margin_table_id

            if margin_table_id:
                await self._get_margin_table_tiers(int(margin_table_id))

            client = self._get_hyperliquid_data_client()
            try:
                funding_data, candle_data = await asyncio.gather(
                    client.get_funding_history(coin, start_ms, ms_now),
                    client.get_candles(coin, start_ms, ms_now),
                )
            except Exception:
                self.logger.warning(f"Failed to get historical data for {coin}")
                continue

            hourly_funding = [float(x.get("fundingRate", 0.0)) for x in funding_data]
            closes = [float(c_val) for c in candle_data if (c_val := c.get("c"))]
            highs = [float(h_val) for c in candle_data if (h_val := c.get("h"))]

            n_ok = min(len(hourly_funding), len(closes), len(highs))
            if n_ok < (lookback_days * 24 - 48):
                continue

            best_choice: dict[str, Any] | None = None

            for L in range(1, max_available_lev + 1):
                N_leg_usd = deposit_usdc * (float(L) / (float(L) + 1.0))
                entry_mmr = self.maintenance_fraction_for_notional(
                    margin_table_id,
                    N_leg_usd,
                    max_available_lev,
                )

                (
                    entry_cost,
                    exit_cost,
                    cost_breakdown,
                    depth_checks,
                ) = await self._estimate_cycle_costs(
                    N_leg_usd=N_leg_usd,
                    spot_asset_id=spot_asset_id,
                    spot_book=spot_book,
                    fee_model=fee_model,
                    depth_params=depth_params,
                    perp_slippage_bps=perp_slippage_bps,
                    day_ntl_usd=candidate.day_notional_usd,
                    spot_symbol=spot_sym,
                )

                sim = self._simulate_barrier_backtest(
                    funding=hourly_funding,
                    closes=closes,
                    highs=highs,
                    leverage=L,
                    stop_frac=stop_frac,
                    fee_eps=fee_eps,
                    N_leg_usd=N_leg_usd,
                    entry_cost_usd=entry_cost,
                    exit_cost_usd=exit_cost,
                    margin_table_id=margin_table_id,
                    fallback_max_leverage=max_available_lev,
                    cooloff_hours=cooloff_hours,
                )

                hours = max(1.0, float(sim["hours"]))
                years = hours / (24.0 * 365.0)
                net_apy = (float(sim["net_pnl_usd"]) / max(1e-9, deposit_usdc)) / years
                gross_apy = (
                    float(sim["gross_funding_usd"]) / max(1e-9, deposit_usdc)
                ) / years
                hit_rate_per_day = (
                    float(sim["cycles"]) / (hours / 24.0) if hours > 0 else 0.0
                )
                avg_hold_hours = (
                    float(sim["hours_in_market"]) / max(1.0, float(sim["cycles"]))
                    if float(sim["cycles"]) > 0
                    else hours
                )
                time_in_market = float(sim["hours_in_market"]) / hours

                bootstrap_stats = self._bootstrap_churn_metrics(
                    funding=hourly_funding,
                    closes=closes,
                    highs=highs,
                    leverage=L,
                    stop_frac=stop_frac,
                    fee_eps=fee_eps,
                    N_leg_usd=N_leg_usd,
                    entry_cost_usd=entry_cost,
                    exit_cost_usd=exit_cost,
                    margin_table_id=margin_table_id,
                    fallback_max_leverage=max_available_lev,
                    cooloff_hours=cooloff_hours,
                    deposit_usdc=deposit_usdc,
                    sims=bootstrap_sims,
                    block_hours=bootstrap_block_hours,
                    seed=None
                    if bootstrap_seed is None
                    else hash((bootstrap_seed, coin, L)),
                )

                choice: dict[str, Any] = {
                    "coin": coin,
                    "spot_pair": spot_sym,
                    "spot_asset_id": spot_asset_id,
                    "best_L": int(L),
                    "net_apy": float(net_apy),
                    "gross_funding_apy": float(gross_apy),
                    "entry_cost_usd": float(entry_cost),
                    "exit_cost_usd": float(exit_cost),
                    "cycles": float(sim["cycles"]),
                    "hit_rate_per_day": float(hit_rate_per_day),
                    "avg_hold_hours": float(avg_hold_hours),
                    "time_in_market_frac": float(time_in_market),
                    "stop_frac": float(stop_frac),
                    "cost_breakdown": cost_breakdown,
                    "depth_checks": depth_checks,
                    "mark_price": float(mark_px),
                    "perp_asset_id": int(perp_asset_id),
                    "mmr": float(entry_mmr),
                    "margin_table_id": margin_table_id,
                    "max_coin_leverage": int(max_available_lev),
                }

                if bootstrap_stats is not None:
                    choice["bootstrap_metrics"] = bootstrap_stats

                if best_choice is None or choice["net_apy"] > best_choice["net_apy"]:
                    best_choice = choice

            if best_choice and best_choice["net_apy"] > float("-inf"):
                ranked.append(best_choice)

        ranked.sort(key=lambda x: float(x.get("net_apy", float("-inf"))), reverse=True)
        return ranked

    # ------------------------------------------------------------------ #
    # Utility Methods                                                     #
    # ------------------------------------------------------------------ #

    def _z_from_conf(self, confidence: float) -> float:
        return analytics_z_from_conf(confidence)

    def _rolling_min_sum(self, arr: list[float], window: int) -> float:
        return analytics_rolling_min_sum(arr, window)

    @staticmethod
    def maintenance_rate_from_max_leverage(max_lev: int) -> float:
        if max_lev <= 0:
            return 0.5
        return 0.5 / max_lev

    @staticmethod
    async def policies() -> list[str]:
        return [
            any_hyperliquid_l1_payload(),
            any_hyperliquid_user_payload(),
            any_erc20_function(HYPERLIQUID_BRIDGE),
        ]
