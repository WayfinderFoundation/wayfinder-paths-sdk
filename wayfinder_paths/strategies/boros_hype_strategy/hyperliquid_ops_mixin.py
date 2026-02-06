"""
Hyperliquid operations for BorosHypeStrategy.

Kept as a mixin so the main strategy file stays readable without changing behavior.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Any

from loguru import logger

from wayfinder_paths.adapters.hyperliquid_adapter.paired_filler import (
    MIN_NOTIONAL_USD,
    FillConfig,
    PairedFiller,
)
from wayfinder_paths.core.constants import HYPERLIQUID_BRIDGE_ADDRESS

from .constants import MAX_HL_LEVERAGE, USDC_ARB
from .types import Inventory


class BorosHypeHyperliquidOpsMixin:
    def _paired_fill_cfg(self, coin: str) -> FillConfig:
        # HYPE can be volatile; slightly higher slippage improves paired-fill reliability.
        if coin.upper() == "HYPE":
            return FillConfig(max_slip_bps=100)
        return FillConfig()

    async def _repair_paired_mismatch(
        self,
        *,
        coin: str,
        spot_asset_id: int,
        perp_asset_id: int,
        filled_spot: float,
        filled_perp: float,
        mismatch_tol: float,
        address: str,
    ) -> tuple[bool, float, float, str]:
        """Try to repair a paired-fill mismatch by resizing the missing leg."""
        mismatch = float(filled_spot) - float(filled_perp)
        if abs(mismatch) <= float(mismatch_tol):
            return True, float(filled_spot), float(filled_perp), "ok"

        if not self.hyperliquid_adapter:
            return (
                False,
                float(filled_spot),
                float(filled_perp),
                "Hyperliquid adapter not configured",
            )

        # Positive mismatch => more spot than perp, need to increase perp short.
        if mismatch > 0:
            rounded = self.hyperliquid_adapter.get_valid_order_size(
                int(perp_asset_id), abs(mismatch)
            )
            if rounded <= 0:
                return (
                    False,
                    float(filled_spot),
                    float(filled_perp),
                    f"{coin} mismatch repair: perp size rounds to 0",
                )
            ok, res = await self.hyperliquid_adapter.place_market_order(
                asset_id=int(perp_asset_id),
                is_buy=False,  # sell to increase short
                slippage=0.10,
                size=float(rounded),
                address=address,
                builder=self.builder_fee,
            )
            if not ok:
                return (
                    False,
                    float(filled_spot),
                    float(filled_perp),
                    f"{coin} mismatch repair failed (perp): {res}",
                )
            filled_perp = float(filled_perp) + float(rounded)
        else:
            # Negative mismatch => more perp than spot, reduce the short.
            rounded = self.hyperliquid_adapter.get_valid_order_size(
                int(perp_asset_id), abs(mismatch)
            )
            if rounded <= 0:
                return True, float(filled_spot), float(filled_perp), "no_op"
            ok, res = await self.hyperliquid_adapter.place_market_order(
                asset_id=int(perp_asset_id),
                is_buy=True,  # buy to reduce short
                slippage=0.10,
                size=float(rounded),
                address=address,
                reduce_only=True,
                builder=self.builder_fee,
            )
            if not ok:
                return (
                    False,
                    float(filled_spot),
                    float(filled_perp),
                    f"{coin} mismatch repair failed (reduce perp): {res}",
                )
            filled_perp = max(0.0, float(filled_perp) - float(rounded))

        mismatch_after = float(filled_spot) - float(filled_perp)
        if abs(mismatch_after) > float(mismatch_tol):
            return (
                False,
                float(filled_spot),
                float(filled_perp),
                f"{coin} mismatch remains after repair: Δ={mismatch_after:.4f} tol={mismatch_tol:.4f}",
            )

        return True, float(filled_spot), float(filled_perp), "repaired"

    async def _get_hype_asset_ids(self) -> tuple[int, int]:
        if not self.hyperliquid_adapter:
            raise RuntimeError("Hyperliquid adapter not configured")

        perp_asset_id = self.hyperliquid_adapter.coin_to_asset.get("HYPE")
        if perp_asset_id is None:
            raise RuntimeError("HYPE perp asset ID not found")

        spot_asset_id = await self.hyperliquid_adapter.get_asset_id(
            "HYPE", is_perp=False
        )
        if spot_asset_id is None:
            raise RuntimeError("HYPE/USDC spot asset ID not found")

        return int(spot_asset_id), int(perp_asset_id)

    async def _ensure_hl_hype_leverage_set(self, address: str) -> tuple[bool, str]:
        if not self.hyperliquid_adapter:
            return False, "Hyperliquid adapter not configured"
        if not self._sign_callback:
            return False, "No strategy wallet signing callback configured"

        # Ensure builder attribution is approved before any other HL actions. New
        # accounts often require an initial deposit before approvals can succeed.
        if self.builder_fee:
            (
                ok_fee,
                fee_msg,
            ) = await self.hyperliquid_adapter.ensure_builder_fee_approved(
                address=address,
                builder_fee=self.builder_fee,
            )
            if not ok_fee:
                return False, fee_msg

        if self._planner_runtime.leverage_set_for_hype:
            return True, "HYPE leverage already set"

        perp_asset_id = self.hyperliquid_adapter.coin_to_asset.get("HYPE")
        if perp_asset_id is None:
            return False, "HYPE perp asset ID not found"

        ok_lev, lev_res = await self.hyperliquid_adapter.update_leverage(
            asset_id=int(perp_asset_id),
            leverage=int(MAX_HL_LEVERAGE),
            is_cross=True,
            address=address,
        )
        if not ok_lev:
            return False, f"Failed to update Hyperliquid leverage: {lev_res}"

        self._planner_runtime.leverage_set_for_hype = True
        logger.info(f"Set Hyperliquid HYPE leverage to {int(MAX_HL_LEVERAGE)}x (cross)")
        return True, f"Set Hyperliquid HYPE leverage to {int(MAX_HL_LEVERAGE)}x (cross)"

    async def _cancel_lingering_orders(
        self, pointers: list[dict[str, Any]], address: str
    ) -> None:
        if not self.hyperliquid_adapter:
            return

        for pointer in pointers:
            metadata = pointer.get("metadata") if isinstance(pointer, dict) else None
            if not isinstance(metadata, dict):
                continue
            asset_id = metadata.get("asset_id")
            cloid = metadata.get("client_id")
            if asset_id is None or not cloid:
                continue
            try:
                await self.hyperliquid_adapter.cancel_order_by_cloid(
                    int(asset_id), str(cloid), address
                )
            except Exception as exc:
                logger.debug(
                    f"Failed to cancel lingering order: asset_id={asset_id}, cloid={cloid}, err={exc}"
                )

    async def _cancel_hl_open_orders_for_hype(self, address: str) -> None:
        if not self.hyperliquid_adapter:
            return

        try:
            spot_asset_id, perp_asset_id = await self._get_hype_asset_ids()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to resolve HYPE asset ids for cancel: {exc}")
            return

        spot_coin = f"@{spot_asset_id - 10000}" if spot_asset_id >= 10000 else None

        success, open_orders = await self.hyperliquid_adapter.get_frontend_open_orders(
            address
        )
        if not success or not isinstance(open_orders, list):
            logger.warning("Could not fetch Hyperliquid open orders to cancel")
            return

        canceled = 0
        for order in open_orders:
            if not isinstance(order, dict):
                continue
            order_coin = order.get("coin") or order.get("asset") or ""
            order_id = order.get("oid") or order.get("orderId") or order.get("id")
            if not order_id:
                continue

            try:
                if str(order_coin) == "HYPE":
                    await self.hyperliquid_adapter.cancel_order(
                        asset_id=int(perp_asset_id),
                        order_id=order_id,
                        address=address,
                    )
                    canceled += 1
                elif spot_coin and str(order_coin) == spot_coin:
                    await self.hyperliquid_adapter.cancel_order(
                        asset_id=int(spot_asset_id),
                        order_id=order_id,
                        address=address,
                    )
                    canceled += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    f"Failed cancel HL order: coin={order_coin} oid={order_id} err={exc}"
                )

        if canceled:
            logger.info(f"Canceled {canceled} Hyperliquid HYPE order(s)")

    async def _sweep_hl_spot_usdc_to_perp(
        self,
        *,
        address: str,
        min_usdc: float = 0.5,
    ) -> tuple[bool, str]:
        """Best-effort: keep HL USDC on perp margin (not spot) when possible."""
        if not self.hyperliquid_adapter:
            return False, "Hyperliquid adapter not configured"

        try:
            # HL spot USDC amounts can have >6dp; usdClassTransfer may effectively round
            # to USDC decimals. Round down and retry with a small epsilon if needed.
            quant = Decimal("0.000001")  # USDC precision (6dp)
            min_usdc_dec = Decimal(str(min_usdc))
            last_res: object | None = None

            for attempt in range(3):
                (
                    success,
                    spot_state,
                ) = await self.hyperliquid_adapter.get_spot_user_state(address)
                if not success or not isinstance(spot_state, dict):
                    return False, "Failed to read HL spot balances"

                balances = spot_state.get("balances", [])
                total_dec = Decimal("0")
                hold_dec = Decimal("0")
                for bal in balances:
                    token = bal.get("coin") or bal.get("token")
                    if token != "USDC":
                        continue
                    hold_dec = Decimal(str(bal.get("hold", 0)))
                    total_dec = Decimal(str(bal.get("total", 0)))
                    break

                available_dec = max(Decimal("0"), total_dec - hold_dec)
                amount_dec = available_dec.quantize(quant, rounding=ROUND_DOWN)
                if attempt:
                    amount_dec = max(
                        Decimal("0"), amount_dec - (quant * Decimal(attempt))
                    )

                if amount_dec <= min_usdc_dec:
                    return True, "No meaningful HL spot USDC to sweep"

                ok, res = await self.hyperliquid_adapter.transfer_spot_to_perp(
                    amount=float(amount_dec),
                    address=address,
                )
                if ok:
                    return True, f"Swept ${float(amount_dec):.2f} HL spot USDC → perp"

                last_res = res
                err_str = str(res.get("response") if isinstance(res, dict) else res)
                if "insufficient balance" not in err_str.lower():
                    break

            return False, f"HL spot→perp transfer failed: {last_res}"
        except Exception as exc:  # noqa: BLE001
            return False, f"Failed to sweep HL spot USDC → perp: {exc}"

    async def _deploy_excess_hl_margin(
        self, params: dict[str, Any], inventory: Inventory
    ) -> tuple[bool, str]:
        # Flow: Transfer USDC perp→spot, buy HYPE on spot, bridge to HyperEVM
        excess_margin = float(params.get("excess_margin_usd") or 0.0)

        if excess_margin < 5:
            return True, "Skipping small excess margin deployment"

        ok, msg = self._require_adapters("hyperliquid_adapter")
        if not ok:
            return False, msg

        ok_addr, address = self._require_strategy_wallet_address()
        if not ok_addr:
            return False, address

        hype_price = float(inventory.hype_price_usd or 0.0)
        if hype_price <= 0:
            return False, "HYPE price unavailable for margin deployment"

        # This step is often executed after another HL step in the same tick.
        # Do not trust the passed `inventory` for margin sizing; fetch fresh HL state.
        withdrawable_now = float(inventory.hl_withdrawable_usd or 0.0)
        try:
            ok_state, user_state = await self.hyperliquid_adapter.get_user_state(
                address
            )
            if ok_state and isinstance(user_state, dict):
                withdrawable_now = float(
                    user_state.get("withdrawable", withdrawable_now) or withdrawable_now
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                f"Failed to refresh HL withdrawable before deploying excess margin: {exc}"
            )

        leverage = float(MAX_HL_LEVERAGE or 2.0)
        if leverage <= 0:
            leverage = 2.0
        buffer_usd = float(
            getattr(self._planner_config, "hl_withdrawable_buffer_usd", 5.0) or 0.0
        )

        # Moving $X from perp->spot and opening an additional short of ~$X consumes ~X*(1 + 1/leverage)
        # of "free" margin. Cap the deployment so we keep some withdrawable buffer for safety.
        denom = 1.0 + (1.0 / leverage)
        max_excess = max(0.0, (withdrawable_now - buffer_usd) / denom)
        deploy_usd = min(excess_margin, max_excess)
        if deploy_usd < 5:
            return True, (
                "Skipping excess margin deployment: insufficient HL withdrawable after buffer "
                f"(withdrawable=${withdrawable_now:.2f}, buffer=${buffer_usd:.2f})"
            )

        success, result = await self.hyperliquid_adapter.transfer_perp_to_spot(
            amount=float(deploy_usd),
            address=address,
        )

        if not success:
            error_msg = result if isinstance(result, str) else str(result)
            return False, f"Perp to spot transfer failed: {error_msg}"

        logger.info(f"Transferred ${deploy_usd:.2f} from perp margin to spot")

        if deploy_usd < MIN_NOTIONAL_USD:
            return True, f"Excess margin ${deploy_usd:.2f} too small for paired fill"

        # PairedFiller performs its own cash check with slippage buffer; avoid
        # over-haircutting here which leaves USDC stranded on HL spot.
        hype_to_buy = deploy_usd / hype_price

        spot_asset_id, perp_asset_id = await self._get_hype_asset_ids()
        paired_filler = PairedFiller(
            adapter=self.hyperliquid_adapter,
            address=address,
            cfg=self._paired_fill_cfg("HYPE"),
        )

        ok_lev, lev_msg = await self._ensure_hl_hype_leverage_set(address)
        if not ok_lev:
            return False, lev_msg

        try:
            (
                filled_spot,
                filled_perp,
                spot_notional,
                perp_notional,
                spot_pointers,
                perp_pointers,
            ) = await paired_filler.fill_pair_units(
                coin="HYPE",
                spot_asset_id=spot_asset_id,
                perp_asset_id=perp_asset_id,
                total_units=hype_to_buy,
                direction="long_spot_short_perp",
                builder_fee=self.builder_fee,
            )
            mismatch_tol = float(
                self._planner_config.delta_neutral_abs_tol_hype or 0.11
            )
            (
                ok_rep,
                filled_spot,
                filled_perp,
                rep_msg,
            ) = await self._repair_paired_mismatch(
                coin="HYPE",
                spot_asset_id=int(spot_asset_id),
                perp_asset_id=int(perp_asset_id),
                filled_spot=float(filled_spot),
                filled_perp=float(filled_perp),
                mismatch_tol=mismatch_tol,
                address=address,
            )
            if not ok_rep:
                return False, f"Paired fill mismatch too large to repair: {rep_msg}"
            logger.info(
                f"Paired fill from excess margin complete: "
                f"spot={filled_spot:.4f} (${spot_notional:.2f}), "
                f"perp={filled_perp:.4f} (${perp_notional:.2f})"
            )
            await self._cancel_lingering_orders(spot_pointers + perp_pointers, address)
        except Exception as exc:
            logger.error(f"Paired fill from excess margin failed: {exc}")
            return False, f"Paired fill failed: {exc}"

        try:
            success, spot_state = await self.hyperliquid_adapter.get_spot_user_state(
                address
            )
            if not success or not isinstance(spot_state, dict):
                return (
                    True,
                    f"Deployed ${deploy_usd:.2f} (bridge pending spot state)",
                )

            balances = spot_state.get("balances", [])
            actual_hype = 0.0
            for bal in balances:
                coin = bal.get("coin") or bal.get("token")
                if coin != "HYPE":
                    continue
                hold = float(bal.get("hold", 0))
                total = float(bal.get("total", 0))
                actual_hype = max(0.0, total - hold)
                break

            amount_to_bridge = actual_hype - 0.001
            if amount_to_bridge < 0.1:
                return True, f"Deployed ${deploy_usd:.2f} (no HYPE to bridge)"

            ok, res = await self.hyperliquid_adapter.hypercore_to_hyperevm(
                amount=amount_to_bridge,
                address=address,
            )
            if ok:
                try:
                    ok_sweep, sweep_msg = await self._sweep_hl_spot_usdc_to_perp(
                        address=address
                    )
                    if not ok_sweep:
                        logger.warning(sweep_msg)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"HL spot USDC sweep failed: {exc}")
                return (
                    True,
                    f"Deployed ${deploy_usd:.2f} excess margin → {amount_to_bridge:.4f} HYPE to HyperEVM",
                )

            err = res if isinstance(res, str) else str(res)
            return False, f"Bridge failed: {err}"

        except Exception as exc:
            return False, f"Bridge failed: {exc}"

    async def _transfer_hl_spot_to_hyperevm(
        self, params: dict[str, Any], inventory: Inventory
    ) -> tuple[bool, str]:
        # HL spot HYPE withdrawal goes directly to HyperEVM (native chain) via L1 withdrawal
        hype_amount = params.get("hype_amount", 0)

        if hype_amount < 0.1:
            return True, "Skipping small HYPE transfer"

        ok, msg = self._require_adapters("hyperliquid_adapter")
        if not ok:
            return False, msg

        ok_addr, address = self._require_strategy_wallet_address()
        if not ok_addr:
            return False, address

        try:
            success, spot_state = await self.hyperliquid_adapter.get_spot_user_state(
                address
            )
            if not success or not isinstance(spot_state, dict):
                return False, "Failed to read HL spot balances"

            balances = spot_state.get("balances", [])
            actual_hype = 0.0
            for bal in balances:
                coin = bal.get("coin") or bal.get("token")
                if coin != "HYPE":
                    continue
                hold = float(bal.get("hold", 0))
                total = float(bal.get("total", 0))
                actual_hype = max(0.0, total - hold)
                break

            transfer_amount = actual_hype - 0.001
            if transfer_amount < 0.1:
                return True, "Insufficient HYPE balance to transfer"

            ok, res = await self.hyperliquid_adapter.hypercore_to_hyperevm(
                amount=transfer_amount,
                address=address,
            )
            if ok:
                return True, f"Transferred {transfer_amount:.4f} HYPE to HyperEVM"

            err = res if isinstance(res, str) else str(res)
            return False, f"Transfer failed: {err}"

        except Exception as exc:
            logger.error(f"HYPE spot transfer failed: {exc}")
            return False, f"HL spot transfer failed: {exc}"

    async def _ensure_hl_short(
        self, params: dict[str, Any], inventory: Inventory
    ) -> tuple[bool, str]:
        # Safety: 2x leverage, venue-valid rounding, check free margin before increasing
        target_size = float(params.get("target_size") or 0.0)
        current_size = float(params.get("current_size") or 0.0)

        # If both target and current are negligible, do nothing.
        # If target is ~0 but we still have a short, fall through so we can close it.
        if target_size < 0.01 and abs(current_size) < 0.01:
            return True, "No HYPE exposure to hedge"

        tol = max(
            self._planner_config.delta_neutral_abs_tol_hype,
            target_size * self._planner_config.delta_neutral_rel_tol,
        )
        delta = target_size - current_size
        diff = abs(delta)
        if diff <= tol:
            return (
                True,
                f"HYPE hedge within tolerance: Δ={diff:.4f} tol={tol:.4f} "
                f"(spot={target_size:.4f}, short={current_size:.4f})",
            )

        ok, msg = self._require_adapters("hyperliquid_adapter")
        if not ok:
            return False, msg

        ok_addr, address = self._require_strategy_wallet_address()
        if not ok_addr:
            return False, address

        perp_asset_id = self.hyperliquid_adapter.coin_to_asset.get("HYPE")
        if perp_asset_id is None:
            return False, "HYPE perp asset ID not found"

        ok_lev, lev_msg = await self._ensure_hl_hype_leverage_set(address)
        if not ok_lev:
            return False, lev_msg

        hype_price = float(inventory.hype_price_usd or 0.0)
        if hype_price <= 0:
            return False, "Cannot hedge: HYPE price unavailable"

        # delta > 0 => need to INCREASE short (sell more)
        if delta > 0:
            min_increase_needed = max(0.0, diff - tol)
            free_margin = float(inventory.hl_withdrawable_usd or 0.0)
            max_increase_by_margin = (
                (free_margin * MAX_HL_LEVERAGE) / hype_price if hype_price > 0 else 0.0
            )
            desired_increase = min(diff, max_increase_by_margin)
            required_margin = (min_increase_needed * hype_price) / MAX_HL_LEVERAGE

            if free_margin < required_margin * 0.9:
                return (
                    False,
                    f"Insufficient free margin (${free_margin:.2f}) to increase short by {diff:.4f} HYPE "
                    f"(need ${required_margin:.2f}, tol={tol:.4f}). Consider trimming spot.",
                )

            rounded_size = self.hyperliquid_adapter.get_valid_order_size(
                int(perp_asset_id), desired_increase
            )
            if rounded_size <= 0:
                return (
                    False,
                    f"Hedge mismatch Δ={diff:.4f} tol={tol:.4f} but order size rounds to 0.",
                )
            if rounded_size + 1e-9 < min_increase_needed:
                return (
                    False,
                    "Insufficient free margin to hedge within tolerance after rounding "
                    f"(need ≥{min_increase_needed:.4f} HYPE, got {rounded_size:.4f}).",
                )

            order_value_usd = rounded_size * hype_price
            if order_value_usd < float(MIN_NOTIONAL_USD):
                return True, (
                    f"Delta {diff:.4f} HYPE below HL ${MIN_NOTIONAL_USD:.0f} minimum, acceptable"
                )

            ok, res = await self.hyperliquid_adapter.place_market_order(
                asset_id=int(perp_asset_id),
                is_buy=False,  # sell to increase short
                slippage=0.05,
                size=float(rounded_size),
                address=address,
                builder=self.builder_fee,
            )
            if not ok:
                return False, f"Failed to increase HYPE short: {res}"

            return True, f"Increased HYPE short by {rounded_size:.4f}"

        # delta < 0 => need to REDUCE short (buy back)
        reduce_units = min(diff, current_size)
        rounded_size = self.hyperliquid_adapter.get_valid_order_size(
            int(perp_asset_id), reduce_units
        )
        if rounded_size <= 0:
            return True, "No short position to reduce"

        order_value_usd = rounded_size * hype_price
        if order_value_usd < float(MIN_NOTIONAL_USD):
            return True, (
                f"Delta {diff:.4f} HYPE below HL ${MIN_NOTIONAL_USD:.0f} minimum, acceptable"
            )

        ok, res = await self.hyperliquid_adapter.place_market_order(
            asset_id=int(perp_asset_id),
            is_buy=True,  # buy to reduce short
            slippage=0.05,
            size=float(rounded_size),
            address=address,
            reduce_only=True,
            builder=self.builder_fee,
        )
        if not ok:
            return False, f"Failed to reduce HYPE short: {res}"

        return True, f"Reduced HYPE short by {rounded_size:.4f}"

    async def _send_usdc_to_hl(
        self, params: dict[str, Any], inventory: Inventory
    ) -> tuple[bool, str]:
        amount_usd = params.get("amount_usd", 0.0)

        if amount_usd < self._planner_config.min_usdc_action:
            return True, f"Skipping small USDC send (${amount_usd:.2f})"

        ok, msg = self._require_adapters("balance_adapter", "hyperliquid_adapter")
        if not ok:
            return False, msg

        ok_addr, address = self._require_strategy_wallet_address()
        if not ok_addr:
            return False, address

        strategy_wallet = self._config.get("strategy_wallet", {})

        usdc_raw = int(float(amount_usd) * 1e6)
        success, result = await self.balance_adapter.send_to_address(
            token_id=USDC_ARB,
            amount=usdc_raw,
            from_wallet=strategy_wallet,
            to_address=HYPERLIQUID_BRIDGE_ADDRESS,
            signing_callback=self._sign_callback,
        )
        if not success:
            return False, f"Failed to send USDC to HL bridge: {result}"

        confirmed, final_balance = await self.hyperliquid_adapter.wait_for_deposit(
            address=address,
            expected_increase=float(amount_usd),
            timeout_s=240,
            poll_interval_s=10,
        )
        if not confirmed:
            return False, (
                f"USDC sent to bridge but not confirmed on Hyperliquid within timeout. "
                f"Current HL balance: ${final_balance:.2f}"
            )

        return (
            True,
            f"Sent ${amount_usd:.2f} USDC to Hyperliquid (balance=${final_balance:.2f})",
        )

    async def _bridge_to_hyperevm(
        self, params: dict[str, Any], inventory: Inventory
    ) -> tuple[bool, str]:
        # Assumes Arb→HL deposit handled by SEND_USDC_TO_HL. We: 1) xfer perp→spot,
        # 2) paired fill (long spot / short perp), 3) bridge spot HYPE to HyperEVM.
        desired_usd = float(params.get("amount_usd") or 0.0)
        reserve_hl_margin_usd = float(params.get("reserve_hl_margin_usd") or 0.0)

        if desired_usd < max(self._planner_config.min_usdc_action, MIN_NOTIONAL_USD):
            # Return False to not trigger re-observation when nothing changes
            return False, f"Skipping small bridge (${desired_usd:.2f})"

        ok, msg = self._require_adapters("hyperliquid_adapter")
        if not ok:
            return False, msg

        ok_addr, address = self._require_strategy_wallet_address()
        if not ok_addr:
            return False, address

        hype_price = float(inventory.hype_price_usd or 0.0)
        if hype_price <= 0:
            success, prices = await self.hyperliquid_adapter.get_all_mid_prices()
            if success and isinstance(prices, dict):
                hype_price = float(prices.get("HYPE", 0.0))
        if hype_price <= 0:
            return False, "Could not determine HYPE price"

        # Transfer from perp→spot, but never below the reserved perp margin.
        spot_usdc_before = float(inventory.hl_spot_usdc or 0.0)
        perp_margin = float(inventory.hl_perp_margin or 0.0)
        withdrawable = float(inventory.hl_withdrawable_usd or 0.0)

        leverage = float(MAX_HL_LEVERAGE or 2.0)
        if leverage <= 0:
            leverage = 2.0
        buffer_usd = float(
            getattr(self._planner_config, "hl_withdrawable_buffer_usd", 5.0) or 0.0
        )
        denom = 1.0 + (1.0 / leverage)
        max_xfer_by_margin = max(
            0.0,
            (withdrawable - buffer_usd - (spot_usdc_before / leverage)) / denom,
        )
        transferable_from_perp = max(
            0.0,
            min(
                max_xfer_by_margin,
                perp_margin - reserve_hl_margin_usd,
            ),
        )

        need_from_perp = max(0.0, desired_usd - spot_usdc_before)
        xfer_usd = min(need_from_perp, transferable_from_perp)

        if xfer_usd >= self._planner_config.min_usdc_action:
            xfer_ok, xfer_res = await self.hyperliquid_adapter.transfer_perp_to_spot(
                amount=float(xfer_usd),
                address=address,
            )
            if not xfer_ok:
                return False, f"Perp→spot transfer failed: {xfer_res}"
            logger.info(
                f"Transferred ${xfer_usd:.2f} from HL perp→spot "
                f"(reserve_perp=${reserve_hl_margin_usd:.2f})"
            )

        deployable_usd = min(desired_usd, spot_usdc_before + xfer_usd)
        if deployable_usd < MIN_NOTIONAL_USD:
            return False, (
                "Insufficient deployable HL spot USDC after reserving perp margin "
                f"(${deployable_usd:.2f} < ${MIN_NOTIONAL_USD:.2f}; "
                f"reserve=${reserve_hl_margin_usd:.2f}, perp=${perp_margin:.2f}, "
                f"withdrawable=${withdrawable:.2f}, spot=${spot_usdc_before:.2f})"
            )

        # Atomic Paired Fill: buy spot HYPE, short perp HYPE
        # PairedFiller performs its own cash check with slippage buffer; avoid
        # over-haircutting here which leaves USDC stranded on HL spot.
        hype_units = deployable_usd / hype_price
        spot_asset_id, perp_asset_id = await self._get_hype_asset_ids()
        paired_filler = PairedFiller(
            adapter=self.hyperliquid_adapter,
            address=address,
            cfg=self._paired_fill_cfg("HYPE"),
        )

        ok_lev, lev_msg = await self._ensure_hl_hype_leverage_set(address)
        if not ok_lev:
            return False, lev_msg

        try:
            (
                filled_spot,
                filled_perp,
                spot_notional,
                perp_notional,
                spot_pointers,
                perp_pointers,
            ) = await paired_filler.fill_pair_units(
                coin="HYPE",
                spot_asset_id=spot_asset_id,
                perp_asset_id=perp_asset_id,
                total_units=hype_units,
                direction="long_spot_short_perp",
                builder_fee=self.builder_fee,
            )
            mismatch_tol = float(
                self._planner_config.delta_neutral_abs_tol_hype or 0.11
            )
            (
                ok_rep,
                filled_spot,
                filled_perp,
                rep_msg,
            ) = await self._repair_paired_mismatch(
                coin="HYPE",
                spot_asset_id=int(spot_asset_id),
                perp_asset_id=int(perp_asset_id),
                filled_spot=float(filled_spot),
                filled_perp=float(filled_perp),
                mismatch_tol=mismatch_tol,
                address=address,
            )
            if not ok_rep:
                return False, f"Paired fill mismatch too large to repair: {rep_msg}"
            logger.info(
                f"Paired fill complete: spot={filled_spot:.4f} (${spot_notional:.2f}), "
                f"perp={filled_perp:.4f} (${perp_notional:.2f})"
            )
            await self._cancel_lingering_orders(spot_pointers + perp_pointers, address)
        except Exception as exc:
            logger.error(f"Paired fill failed: {exc}")
            return False, f"Paired fill failed: {exc}"

        success, spot_state = await self.hyperliquid_adapter.get_spot_user_state(
            address
        )
        if not success or not isinstance(spot_state, dict):
            return False, "Failed to read HL spot balances after paired fill"

        balances = spot_state.get("balances", [])
        actual_hype = 0.0
        for bal in balances:
            coin = bal.get("coin") or bal.get("token")
            if coin != "HYPE":
                continue
            hold = float(bal.get("hold", 0))
            total = float(bal.get("total", 0))
            actual_hype = max(0.0, total - hold)
            break

        amount_to_bridge = actual_hype - 0.001
        if amount_to_bridge < 0.1:
            return False, "No HYPE available to bridge to HyperEVM"

        ok, res = await self.hyperliquid_adapter.hypercore_to_hyperevm(
            amount=amount_to_bridge,
            address=address,
        )
        if ok:
            try:
                ok_sweep, sweep_msg = await self._sweep_hl_spot_usdc_to_perp(
                    address=address
                )
                if not ok_sweep:
                    logger.warning(sweep_msg)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"HL spot USDC sweep failed: {exc}")
            return True, f"Bridged {amount_to_bridge:.4f} HYPE to HyperEVM"

        err = res if isinstance(res, str) else str(res)
        return False, f"Bridge failed: {err}"
