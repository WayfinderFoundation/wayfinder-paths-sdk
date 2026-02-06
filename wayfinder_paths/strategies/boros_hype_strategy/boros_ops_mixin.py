"""
Boros venue operations for BorosHypeStrategy.

Kept as a mixin so the main strategy file stays readable without changing behavior.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any

from loguru import logger

from wayfinder_paths.core.utils.transaction import encode_call, send_transaction

from .constants import (
    BOROS_HYPE_MARKET_ID,
    BOROS_HYPE_TOKEN_ID,
    BOROS_MIN_DEPOSIT_HYPE,
    HYPE_NATIVE,
    HYPE_OFT_ADDRESS,
    HYPEREVM_CHAIN_ID,
    KHYPE_LST,
    LOOPED_HYPE,
    MIN_HYPE_GAS,
    USDC_ARB,
    USDT_ARB,
    WHYPE,
    WHYPE_ABI,
    WHYPE_ADDRESS,
)
from .types import Inventory

if TYPE_CHECKING:
    from .strategy import BorosHypeStrategy



class BorosHypeBorosOpsMixin:
    async def _fund_boros(
        self: BorosHypeStrategy, params: dict[str, Any], inventory: Inventory
    ) -> tuple[bool, str]:
        """Fund Boros using native HYPE collateral.

        Flow:
        - If we already have Arbitrum OFT HYPE, deposit it to Boros cross margin.
        - Otherwise, bridge native HYPE from HyperEVM -> Arbitrum via LayerZero OFT.
        """

        amount_usd = float(params.get("amount_usd") or 0.0)
        market_id = int(
            params.get("market_id")
            or self._planner_runtime.current_boros_market_id
            or BOROS_HYPE_MARKET_ID
        )
        token_id = int(
            params.get("token_id")
            or self._planner_runtime.current_boros_token_id
            or BOROS_HYPE_TOKEN_ID
        )

        collateral_address = str(
            params.get("collateral_address")
            or self._planner_runtime.current_boros_collateral_address
            or ""
        ).strip()
        if not collateral_address:
            collateral_address = HYPE_OFT_ADDRESS

        if amount_usd < 1.0:
            return True, f"Skipping tiny Boros funding (${amount_usd:.2f})"

        if not self._sign_callback:
            return False, "No signing callback configured"

        ok, msg = self._require_adapters("boros_adapter")
        if not ok:
            return False, msg

        ok_addr, wallet_address = self._require_strategy_wallet_address()
        if not ok_addr:
            return False, wallet_address

        hype_price = float(inventory.hype_price_usd or 0.0)
        if hype_price <= 0:
            return False, f"Invalid HYPE price (${hype_price:.6f})"

        target_hype = float(amount_usd) / hype_price
        target_wei = int(target_hype * 1e18)

        # 1) If we have OFT HYPE on Arbitrum, deposit it first (idempotent; avoids double-bridging).
        available_oft_hype = float(inventory.hype_oft_arb_balance or 0.0)
        if available_oft_hype > 0:
            deposit_hype = min(available_oft_hype, target_hype)
            deposit_usd = deposit_hype * hype_price
            if deposit_usd >= 1.0:
                deposit_wei = int(deposit_hype * 1e18)
                ok_dep, dep_res = await self.boros_adapter.deposit_to_cross_margin(
                    collateral_address=collateral_address,
                    amount_wei=deposit_wei,
                    token_id=token_id,
                    market_id=market_id,
                )
                if not ok_dep:
                    return False, f"Boros deposit failed: {dep_res}"

                logger.info(
                    f"Deposited {deposit_hype:.6f} HYPE (≈${deposit_usd:.2f}) to Boros cross margin"
                )

                # If this fully satisfies the target, stop here.
                if deposit_hype >= target_hype - 1e-9:
                    return True, (
                        f"Funded Boros with {deposit_hype:.6f} HYPE (≈${deposit_usd:.2f}) "
                        "from Arbitrum OFT balance"
                    )

                # Otherwise, bridge the remainder.
                target_hype = max(0.0, target_hype - deposit_hype)
                target_wei = int(target_hype * 1e18)
            else:
                logger.info(
                    f"Skipping tiny OFT HYPE deposit (${deposit_usd:.2f}); will top up via bridge"
                )

        # 2) Bridge native HYPE from HyperEVM to Arbitrum using the OFT contract.
        hype_balance = float(inventory.hype_hyperevm_balance or 0.0)
        whype_balance = float(inventory.whype_balance or 0.0)
        fee_buffer_hype = 0.03  # conservative buffer for OFT native fee
        desired_native_hype = float(MIN_HYPE_GAS) + float(target_hype) + fee_buffer_hype
        if whype_balance > 0.0:
            # On HyperEVM, HYPE can be split between native HYPE and WHYPE. The OFT
            # bridge requires native HYPE (msg.value), so unwrap enough WHYPE to
            # cover (amount + fee) while still leaving MIN_HYPE_GAS for future gas.
            if hype_balance < desired_native_hype:
                unwrap_hype = min(whype_balance, desired_native_hype - hype_balance)
                unwrap_wei = int(unwrap_hype * 1e18)
                if self.balance_adapter:
                    (
                        ok_whype,
                        whype_raw,
                    ) = await self.balance_adapter.get_vault_wallet_balance(WHYPE)
                    if ok_whype and isinstance(whype_raw, int) and whype_raw > 0:
                        unwrap_wei = min(int(unwrap_wei), int(whype_raw))
                if unwrap_wei > 0:
                    if not self._sign_callback:
                        return False, "No signing callback configured"

                    logger.info(
                        f"Unwrapping {unwrap_hype:.6f} WHYPE → native HYPE to fund OFT bridge"
                    )
                    try:
                        tx = await encode_call(
                            target=WHYPE_ADDRESS,
                            abi=WHYPE_ABI,
                            fn_name="withdraw",
                            args=[int(unwrap_wei)],
                            from_address=wallet_address,
                            chain_id=HYPEREVM_CHAIN_ID,
                        )
                        tx_hash = await send_transaction(
                            tx, self._sign_callback, wait_for_receipt=True
                        )
                        logger.info(f"WHYPE unwrap tx={tx_hash}")
                        await asyncio.sleep(2)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"WHYPE unwrap failed: {exc}")

                    if self.balance_adapter:
                        (
                            ok_hype,
                            hype_raw,
                        ) = await self.balance_adapter.get_vault_wallet_balance(
                            HYPE_NATIVE
                        )
                        if ok_hype and hype_raw:
                            hype_balance = float(hype_raw) / 1e18

        # If we still don't have enough native HYPE and there are LSTs on HyperEVM,
        # sell a small amount back to native HYPE so Boros can be funded.
        if (
            hype_balance < desired_native_hype
            and (inventory.khype_value_usd > 0 or inventory.looped_hype_value_usd > 0)
            and self.brap_adapter
        ):
            needed_hype = max(0.0, desired_native_hype - hype_balance)
            if needed_hype > 0.01:
                if inventory.khype_value_usd > 0 and inventory.khype_to_hype_ratio > 0:
                    max_hype_from_khype = float(inventory.khype_value_usd) / hype_price
                    sell_hype = min(needed_hype, max_hype_from_khype)
                    sell_khype = sell_hype / float(inventory.khype_to_hype_ratio)
                    sell_khype_wei = int(sell_khype * 1e18)
                    if sell_khype_wei > 0:
                        logger.info(
                            f"Selling {sell_khype:.6f} kHYPE → native HYPE to fund Boros"
                        )
                        ok_swap, res_swap = await self.brap_adapter.swap_from_token_ids(
                            from_token_id=KHYPE_LST,
                            to_token_id=HYPE_NATIVE,
                            from_address=wallet_address,
                            amount=str(int(sell_khype_wei)),
                            slippage=0.01,
                            strategy_name="boros_hype_strategy",
                        )
                        if ok_swap:
                            needed_hype = max(0.0, needed_hype - sell_hype)
                            await asyncio.sleep(2)
                        else:
                            logger.warning(f"Sell kHYPE→HYPE failed: {res_swap}")

                if (
                    needed_hype > 0.01
                    and inventory.looped_hype_value_usd > 0
                    and inventory.looped_hype_to_hype_ratio > 0
                ):
                    max_hype_from_lhype = (
                        float(inventory.looped_hype_value_usd) / hype_price
                    )
                    sell_hype = min(needed_hype, max_hype_from_lhype)
                    sell_lhype = sell_hype / float(inventory.looped_hype_to_hype_ratio)
                    sell_lhype_wei = int(sell_lhype * 1e18)
                    if sell_lhype_wei > 0:
                        logger.info(
                            f"Selling {sell_lhype:.6f} looped HYPE → native HYPE to fund Boros"
                        )
                        ok_swap, res_swap = await self.brap_adapter.swap_from_token_ids(
                            from_token_id=LOOPED_HYPE,
                            to_token_id=HYPE_NATIVE,
                            from_address=wallet_address,
                            amount=str(int(sell_lhype_wei)),
                            slippage=0.01,
                            strategy_name="boros_hype_strategy",
                        )
                        if ok_swap:
                            await asyncio.sleep(2)
                        else:
                            logger.warning(f"Sell looped HYPE→HYPE failed: {res_swap}")

                if self.balance_adapter:
                    (
                        ok_hype,
                        hype_raw,
                    ) = await self.balance_adapter.get_vault_wallet_balance(HYPE_NATIVE)
                    if ok_hype and hype_raw:
                        hype_balance = float(hype_raw) / 1e18
        if hype_balance <= MIN_HYPE_GAS + 0.0005:
            return (
                False,
                f"Insufficient HyperEVM HYPE to bridge (balance={hype_balance:.6f}, min_gas={MIN_HYPE_GAS:.6f})",
            )

        reserve_wei = int(MIN_HYPE_GAS * 1e18)
        balance_wei = int(hype_balance * 1e18)
        max_value_wei = max(0, balance_wei - reserve_wei)
        if max_value_wei <= 0:
            return False, "Insufficient HyperEVM HYPE to bridge after reserving gas"

        bridge_amount_wei = min(target_wei, max_value_wei)
        if bridge_amount_wei <= 0:
            return True, "Boros funding: nothing to bridge"
        (
            ok_bridge,
            bridge_res,
        ) = await self.boros_adapter.bridge_hype_oft_hyperevm_to_arbitrum(
            amount_wei=int(bridge_amount_wei),
            max_value_wei=int(max_value_wei),
            to_address=str(wallet_address),
            from_address=str(wallet_address),
        )
        if not ok_bridge:
            return False, f"OFT bridge failed: {bridge_res}"

        tx_hash = str(bridge_res.get("tx_hash") or "")
        bridged_wei = int(bridge_res.get("amount_wei") or 0)
        bridged_hype = float(bridged_wei) / 1e18
        bridged_usd = bridged_hype * hype_price

        # Track in-flight amount so planner doesn't double-fund while the bridge settles.
        self._planner_runtime.in_flight_boros_oft_hype = bridged_hype
        self._planner_runtime.in_flight_boros_oft_hype_balance_before = float(
            inventory.hype_oft_arb_balance or 0.0
        )
        self._planner_runtime.in_flight_boros_oft_hype_started_at = datetime.utcnow()

        logger.info(
            f"Initiated OFT bridge HyperEVM->Arbitrum: {bridged_hype:.6f} HYPE (≈${bridged_usd:.2f}), tx={tx_hash}"
        )

        return True, (
            f"Bridging {bridged_hype:.6f} HYPE (≈${bridged_usd:.2f}) HyperEVM→Arbitrum via OFT; "
            f"tx={tx_hash} (LayerZero: {bridge_res.get('layerzeroscan')}). "
            "Once bridged HYPE lands on Arbitrum, the next tick will deposit it to Boros."
        )

    async def _ensure_boros_position(
        self: BorosHypeStrategy, params: dict[str, Any], inventory: Inventory
    ) -> tuple[bool, str]:
        # If Boros operations fail unexpectedly, triggers fail-safe liquidation.
        market_id = int(
            params.get("market_id")
            or self._planner_runtime.current_boros_market_id
            or BOROS_HYPE_MARKET_ID
        )
        token_id = int(
            params.get("token_id")
            or self._planner_runtime.current_boros_token_id
            or BOROS_HYPE_TOKEN_ID
        )
        target_size_yu = float(params.get("target_size_yu") or 0.0)

        if inventory.boros_pending_withdrawal_usd > 0:
            return True, (
                f"Boros withdrawal pending (~${inventory.boros_pending_withdrawal_usd:.2f}). "
                "Skipping Boros rate position actions until it settles."
            )

        if not self.boros_adapter or not market_id:
            return False, "Boros adapter not configured or no market selected"

        try:
            return await self._ensure_boros_position_impl(
                market_id=market_id,
                token_id=token_id,
                target_size_yu=target_size_yu,
                inventory=inventory,
            )
        except Exception as exc:
            logger.error(
                f"[BOROS_FAIL] Critical failure in Boros position management: {exc}"
            )
            # Trigger fail-safe liquidation
            return await self._failsafe_liquidate_all(
                f"Boros position management failed: {exc}"
            )

    async def _ensure_boros_position_impl(
        self: BorosHypeStrategy,
        *,
        market_id: int,
        token_id: int,
        target_size_yu: float,
        inventory: Inventory,
    ) -> tuple[bool, str]:
        # Only try to manage the rate position once we have at least the minimum
        # HYPE collateral funded on Boros (or sitting idle as OFT HYPE on Arbitrum).
        depositable_hype = float(inventory.boros_collateral_hype or 0.0) + float(
            inventory.hype_oft_arb_balance or 0.0
        )
        if depositable_hype < BOROS_MIN_DEPOSIT_HYPE:
            return (
                True,
                "Skipping Boros position: collateral not funded yet "
                f"({depositable_hype:.6f} HYPE)",
            )

        # 0) Cleanup: sweep any isolated collateral back to cross margin.
        try:
            ok_sweep, sweep_res = await self.boros_adapter.sweep_isolated_to_cross(
                token_id=int(token_id)
            )
            if not ok_sweep:
                logger.warning(f"Failed Boros isolated->cross sweep: {sweep_res}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed Boros isolated->cross sweep: {exc}")

        # 1) Best-effort: if any OFT HYPE is sitting idle on Arbitrum, deposit it to cross margin.
        if inventory.hype_oft_arb_balance > 0.0:
            try:
                deposit_hype = float(inventory.hype_oft_arb_balance or 0.0)
                deposit_usd = deposit_hype * float(inventory.hype_price_usd or 0.0)
                if deposit_usd >= 1.0:
                    ok_dep, dep_res = await self.boros_adapter.deposit_to_cross_margin(
                        collateral_address=HYPE_OFT_ADDRESS,
                        amount_wei=int(deposit_hype * 1e18),
                        token_id=int(token_id),
                        market_id=int(market_id),
                    )
                    if ok_dep:
                        logger.info(
                            f"Deposited idle OFT HYPE to Boros: {deposit_hype:.6f} (≈${deposit_usd:.2f})"
                        )
                        await asyncio.sleep(2)
                    else:
                        logger.warning(
                            f"Failed to deposit OFT HYPE to Boros: {dep_res}"
                        )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Failed to deposit OFT HYPE to Boros: {exc}")

        # 2) Rollover: close positions in other markets (best effort).
        try:
            ok_roll, roll_res = await self.boros_adapter.close_positions_except(
                keep_market_id=int(market_id),
                token_id=int(token_id),
                market_ids=inventory.boros_position_market_ids or [],
                best_effort=True,
            )
            if not ok_roll:
                logger.warning(f"Failed Boros rollover close: {roll_res}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed Boros rollover close: {exc}")

        yu_to_usd = (
            float(inventory.hype_price_usd or 0.0)
            if int(token_id) == BOROS_HYPE_TOKEN_ID
            else 1.0
        )
        ok_set, set_res = await self.boros_adapter.ensure_position_size_yu(
            market_id=int(market_id),
            token_id=int(token_id),
            target_size_yu=float(target_size_yu),
            tif="IOC",
            min_resize_excess_usd=float(
                self._planner_config.boros_resize_min_excess_usd
            ),
            yu_to_usd=float(yu_to_usd),
        )
        if not ok_set:
            return False, f"Failed to ensure Boros position: {set_res}"

        action = str(set_res.get("action") or "unknown")
        diff_yu = float(set_res.get("diff_yu") or 0.0)
        if action == "no_op":
            return (
                True,
                f"Boros position already at target ({set_res.get('current_size_yu', 0.0):.4f} YU)",
            )
        if action == "increase_short":
            return (
                True,
                f"Boros position increased by {diff_yu:.4f} YU on market {market_id}",
            )
        if action == "decrease":
            return (
                True,
                f"Boros position decreased by {abs(diff_yu):.4f} YU on market {market_id}",
            )

        return (
            True,
            f"Boros position adjusted ({action}) on market {market_id}: Δ={diff_yu:.4f} YU",
        )

    async def _complete_pending_withdrawal(
        self: BorosHypeStrategy, params: dict[str, Any], inventory: Inventory
    ) -> tuple[bool, str]:
        # Legacy helper used by some withdrawal flows: swap USDT->USDC on Arbitrum
        usdt_idle = float(params.get("usdt_idle") or 0.0)

        if usdt_idle < 1.0:
            return True, f"Withdrawal completion: no USDT to swap (${usdt_idle:.2f})"

        ok, msg = self._require_adapters("balance_adapter", "brap_adapter")
        if not ok:
            return False, msg

        ok_addr, address = self._require_strategy_wallet_address()
        if not ok_addr:
            return False, address

        ok_usdt, usdt_raw = await self.balance_adapter.get_vault_wallet_balance(
            USDT_ARB
        )
        if not ok_usdt or usdt_raw <= 0:
            return True, f"Withdrawal completion: no USDT to swap (${usdt_idle:.2f})"

        ok_swap, swap_res = await self.brap_adapter.swap_from_token_ids(
            from_token_id=USDT_ARB,
            to_token_id=USDC_ARB,
            from_address=address,
            amount=str(int(usdt_raw)),
            slippage=0.005,
            strategy_name="boros_hype_strategy",
        )
        if not ok_swap:
            return False, f"Withdrawal completion swap failed: {swap_res}"

        ok_usdc, usdc_raw = await self.balance_adapter.get_vault_wallet_balance(
            USDC_ARB
        )
        usdc_tokens = (float(usdc_raw) / 1e6) if ok_usdc and usdc_raw > 0 else 0.0

        return True, (
            f"Withdrawal completion: swapped ${usdt_idle:.2f} USDT→USDC "
            f"(${usdc_tokens:.2f} USDC)"
        )
