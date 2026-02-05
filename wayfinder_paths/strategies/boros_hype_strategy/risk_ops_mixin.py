"""
Risk and recovery operations for BorosHypeStrategy.

Kept as a mixin so the main strategy file stays readable without changing behavior.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from loguru import logger

from wayfinder_paths.adapters.hyperliquid_adapter.adapter import HyperliquidAdapter
from wayfinder_paths.adapters.hyperliquid_adapter.paired_filler import PairedFiller

from .constants import (
    BOROS_HYPE_MARKET_ID,
    BOROS_HYPE_TOKEN_ID,
    HYPE_NATIVE,
    KHYPE_LST,
    LOOPED_HYPE,
    MIN_HYPE_GAS,
    MIN_NET_DEPOSIT,
    USDC_ARB,
    USDT_ARB,
    WHYPE,
)
from .types import Inventory

if TYPE_CHECKING:
    from .strategy import BorosHypeStrategy


class BorosHypeRiskOpsMixin:
    async def _close_and_redeploy(
        self: BorosHypeStrategy, params: dict[str, Any], inventory: Inventory
    ) -> tuple[bool, str]:
        ok, msg = self._require_adapters(
            "balance_adapter", "hyperliquid_adapter", "brap_adapter"
        )
        if not ok:
            return False, msg
        if not self._sign_callback:
            return False, "No strategy wallet signing callback configured"

        ok_addr, address = self._require_strategy_wallet_address()
        if not ok_addr:
            return False, address
        strategy_wallet = self._config.get("strategy_wallet", {})

        logger.warning("Emergency close and redeploy triggered")

        try:
            await self._cancel_hl_open_orders_for_hype(address)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Failed to cancel HL open orders pre-redeploy: {exc}")

        try:
            ok_state, user_state = await self.hyperliquid_adapter.get_user_state(
                address
            )
            current_short_size = 0.0
            if ok_state and isinstance(user_state, dict):
                for pos in user_state.get("assetPositions", []):
                    p = pos.get("position", {}) if isinstance(pos, dict) else {}
                    if p.get("coin") == "HYPE":
                        szi = float(p.get("szi", 0))
                        if szi < 0:
                            current_short_size = abs(szi)
                        break

            if current_short_size > 0.01:
                perp_asset_id = self.hyperliquid_adapter.coin_to_asset.get("HYPE")
                if perp_asset_id is None:
                    logger.warning(
                        "Missing Hyperliquid perp asset id for HYPE; cannot close hedge"
                    )
                else:
                    rounded_size = self.hyperliquid_adapter.get_valid_order_size(
                        int(perp_asset_id), current_short_size
                    )
                    if rounded_size > 0:
                        (
                            ok_close,
                            res_close,
                        ) = await self.hyperliquid_adapter.place_market_order(
                            asset_id=int(perp_asset_id),
                            is_buy=True,
                            slippage=0.05,
                            size=float(rounded_size),
                            address=address,
                            reduce_only=True,
                            builder=self.builder_fee,
                        )
                        if not ok_close:
                            logger.warning(f"Failed to close HL short: {res_close}")
                        await asyncio.sleep(2)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed closing HL hedge: {exc}")

        if self.boros_adapter:
            try:
                ok_pos, positions = await self.boros_adapter.get_active_positions()
                if ok_pos and isinstance(positions, list):
                    for pos in positions:
                        mid = pos.get("marketId") or pos.get("market_id")
                        try:
                            mid_int = int(mid) if mid is not None else None
                        except (TypeError, ValueError):
                            mid_int = None
                        if mid_int and mid_int > 0:
                            try:
                                await self.boros_adapter.close_positions_market(mid_int)
                            except Exception as exc:  # noqa: BLE001
                                logger.warning(
                                    f"Failed to close Boros market {mid_int}: {exc}"
                                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Failed to close Boros positions: {exc}")

        try:
            ok_khype, khype_raw = await self.balance_adapter.get_vault_wallet_balance(
                KHYPE_LST
            )
            if ok_khype and khype_raw > 0:
                await self.brap_adapter.swap_from_token_ids(
                    from_token_id=KHYPE_LST,
                    to_token_id=HYPE_NATIVE,
                    from_address=address,
                    amount=str(int(khype_raw)),
                    slippage=0.01,
                    strategy_name="boros_hype_strategy",
                )

            ok_lhype, lhype_raw = await self.balance_adapter.get_vault_wallet_balance(
                LOOPED_HYPE
            )
            if ok_lhype and lhype_raw > 0:
                await self.brap_adapter.swap_from_token_ids(
                    from_token_id=LOOPED_HYPE,
                    to_token_id=HYPE_NATIVE,
                    from_address=address,
                    amount=str(int(lhype_raw)),
                    slippage=0.01,
                    strategy_name="boros_hype_strategy",
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed selling spot LSTs to HYPE: {exc}")

        try:
            ok_hype, hype_raw = await self.balance_adapter.get_vault_wallet_balance(
                HYPE_NATIVE
            )
            hype_raw_int = int(hype_raw) if ok_hype and hype_raw and hype_raw > 0 else 0
            gas_reserve_wei = int(float(MIN_HYPE_GAS) * 1e18)
            hype_to_transfer_raw = max(0, hype_raw_int - gas_reserve_wei)

            if hype_to_transfer_raw > int(0.01 * 1e18):
                destination = HyperliquidAdapter.hypercore_index_to_system_address(150)
                ok_send, send_res = await self.balance_adapter.send_to_address(
                    token_id=HYPE_NATIVE,
                    amount=int(hype_to_transfer_raw),
                    from_wallet=strategy_wallet,
                    to_address=destination,
                    signing_callback=self._sign_callback,
                )
                if not ok_send:
                    logger.warning(
                        f"Failed to transfer HYPE to Hyperliquid: {send_res}"
                    )
                await asyncio.sleep(3)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to transfer HYPE to Hyperliquid: {exc}")

        try:
            ok_spot, spot_state = await self.hyperliquid_adapter.get_spot_user_state(
                address
            )
            spot_hype_balance = 0.0
            if ok_spot and isinstance(spot_state, dict):
                for bal in spot_state.get("balances", []):
                    token = bal.get("coin") or bal.get("token")
                    hold = float(bal.get("hold", 0))
                    total = float(bal.get("total", 0))
                    available = total - hold
                    if token == "HYPE":
                        spot_hype_balance = available
                        break

            if spot_hype_balance > 0.01:
                spot_asset_id, _ = await self._get_hype_asset_ids()
                rounded_size = self.hyperliquid_adapter.get_valid_order_size(
                    int(spot_asset_id), spot_hype_balance
                )
                if rounded_size > 0:
                    await self.hyperliquid_adapter.place_market_order(
                        asset_id=int(spot_asset_id),
                        is_buy=False,
                        slippage=0.10,
                        size=float(rounded_size),
                        address=address,
                        builder=self.builder_fee,
                    )
                    await asyncio.sleep(2)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to sell spot HYPE: {exc}")

        try:
            usdc_sz_decimals = (
                await self.hyperliquid_adapter.get_spot_token_sz_decimals("USDC")
            )
            if usdc_sz_decimals is None:
                usdc_sz_decimals = 2

            ok_spot, spot_state = await self.hyperliquid_adapter.get_spot_user_state(
                address
            )
            spot_usdc = 0.0
            spot_total_s = "0"
            spot_hold_s = "0"
            if ok_spot and isinstance(spot_state, dict):
                for bal in spot_state.get("balances", []):
                    token = bal.get("coin") or bal.get("token")
                    if token != "USDC":
                        continue
                    spot_total_s = str(bal.get("total", "0") or "0")
                    spot_hold_s = str(bal.get("hold", "0") or "0")
                    hold = float(spot_hold_s)
                    total = float(spot_total_s)
                    spot_usdc = max(0.0, total - hold)
                    break
            if spot_usdc > 1.0:
                amount = self.hyperliquid_adapter.max_transferable_amount(
                    spot_total_s,
                    spot_hold_s,
                    sz_decimals=int(usdc_sz_decimals),
                    leave_one_tick=True,
                )
                (
                    ok_xfer,
                    res_xfer,
                ) = await self.hyperliquid_adapter.transfer_spot_to_perp(
                    amount=float(amount),
                    address=address,
                )
                if (not ok_xfer) and int(usdc_sz_decimals) != 2:
                    if "insufficient balance" in str(res_xfer).lower():
                        fallback_2dp = self.hyperliquid_adapter.max_transferable_amount(
                            spot_total_s,
                            spot_hold_s,
                            sz_decimals=2,
                            leave_one_tick=True,
                        )
                        if fallback_2dp > 1.0:
                            await self.hyperliquid_adapter.transfer_spot_to_perp(
                                amount=float(fallback_2dp),
                                address=address,
                            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to move spot USDC to perp: {exc}")

        hl_perp_balance = 0.0
        try:
            ok_state, user_state = await self.hyperliquid_adapter.get_user_state(
                address
            )
            if ok_state and isinstance(user_state, dict):
                hl_perp_balance = float(
                    self.hyperliquid_adapter.get_perp_margin_amount(user_state)
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to read HL perp balance: {exc}")

        ok_arb_usdc, arb_usdc_raw = await self.balance_adapter.get_vault_wallet_balance(
            USDC_ARB, wallet_address=address
        )
        arb_usdc_tokens = (
            (int(arb_usdc_raw) / 1e6) if ok_arb_usdc and arb_usdc_raw else 0.0
        )
        total_usdc = hl_perp_balance + arb_usdc_tokens

        if total_usdc < MIN_NET_DEPOSIT:
            return True, "Closed all positions. Insufficient capital to redeploy."

        spot_target = self.hedge_cfg.spot_pct * total_usdc
        boros_target = self.hedge_cfg.boros_pct * total_usdc

        if spot_target > self._planner_config.min_usdc_action:
            try:
                await self.hyperliquid_adapter.transfer_perp_to_spot(
                    amount=float(spot_target),
                    address=address,
                )
                await asyncio.sleep(2)

                success, mids = await self.hyperliquid_adapter.get_all_mid_prices()
                hype_price = (
                    float(mids.get("HYPE", 0.0))
                    if success and isinstance(mids, dict)
                    else 0.0
                )
                if hype_price <= 0:
                    hype_price = float(inventory.hype_price_usd or 0.0)

                if hype_price > 0:
                    hype_to_buy = float(spot_target) / hype_price
                    spot_asset_id, _ = await self._get_hype_asset_ids()
                    rounded_size = self.hyperliquid_adapter.get_valid_order_size(
                        int(spot_asset_id), hype_to_buy
                    )
                    if rounded_size > 0:
                        await self.hyperliquid_adapter.place_market_order(
                            asset_id=int(spot_asset_id),
                            is_buy=True,
                            slippage=0.10,
                            size=float(rounded_size),
                            address=address,
                            builder=self.builder_fee,
                        )
                        await asyncio.sleep(2)

                        (
                            ok_spot,
                            spot_state,
                        ) = await self.hyperliquid_adapter.get_spot_user_state(address)
                        spot_hype = 0.0
                        if ok_spot and isinstance(spot_state, dict):
                            for bal in spot_state.get("balances", []):
                                token = bal.get("coin") or bal.get("token")
                                if token == "HYPE":
                                    hold = float(bal.get("hold", 0))
                                    total = float(bal.get("total", 0))
                                    spot_hype = max(0.0, total - hold)
                                    break

                        amount_to_bridge = spot_hype - 0.001
                        if amount_to_bridge > 0.1:
                            await self.hyperliquid_adapter.hypercore_to_hyperevm(
                                amount=float(amount_to_bridge),
                                address=address,
                            )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Failed to redeploy spot: {exc}")

        if boros_target > self._planner_config.min_usdt_action:
            try:
                ok_wd, wd_res = await self.hyperliquid_adapter.withdraw(
                    amount=float(boros_target),
                    address=address,
                )
                if ok_wd:
                    await self.hyperliquid_adapter.wait_for_withdrawal(
                        address=address,
                        max_poll_time_s=300,
                        poll_interval_s=10,
                    )
                    (
                        ok_arb,
                        arb_raw,
                    ) = await self.balance_adapter.get_vault_wallet_balance(
                        USDC_ARB, wallet_address=address
                    )
                    if ok_arb and arb_raw and int(arb_raw) > 0:
                        await self.brap_adapter.swap_from_token_ids(
                            from_token_id=USDC_ARB,
                            to_token_id=USDT_ARB,
                            from_address=address,
                            amount=str(int(arb_raw)),
                            slippage=0.005,
                            strategy_name="boros_hype_strategy",
                        )
                else:
                    logger.warning(
                        f"Failed to withdraw from HL for Boros redeploy: {wd_res}"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Failed to redeploy Boros: {exc}")

        try:
            inv_after = await self.observe()
            swappable_hype = max(
                0.0, float(inv_after.hype_hyperevm_balance or 0.0) - MIN_HYPE_GAS
            )
            if swappable_hype > self._planner_config.min_hype_swap:
                await self._swap_hype_to_lst({"hype_amount": swappable_hype}, inv_after)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to allocate spot HYPE after redeploy: {exc}")

        try:
            inv_final = await self.observe()
            ok_short, msg_short = await self._ensure_hl_short(
                {
                    "target_size": inv_final.total_hype_exposure,
                    "current_size": inv_final.hl_short_size_hype,
                },
                inv_final,
            )
            if not ok_short:
                logger.error(
                    f"[RECOVERY_FAIL] Could not re-open HL hedge after redeploy: {msg_short}"
                )
                return await self._failsafe_liquidate_all(
                    f"Post-liquidation hedge rebuild failed: {msg_short}"
                )

            # If we cannot get Boros back into a sane state, melt down rather than
            # continuing with a partially functioning multi-venue position.
            try:
                spot_usd = float(inv_final.total_hype_exposure) * float(
                    inv_final.hype_price_usd
                )
                boros_enabled = (
                    float(inv_final.total_value)
                    >= float(self._planner_config.min_total_for_boros)
                    and float(inv_final.boros_pending_withdrawal_usd) <= 0.0
                )
                if boros_enabled and spot_usd >= 10.0 and self.boros_adapter:
                    market_id = (
                        self._planner_runtime.current_boros_market_id
                        or BOROS_HYPE_MARKET_ID
                    )
                    token_id = int(
                        self._planner_runtime.current_boros_token_id
                        or BOROS_HYPE_TOKEN_ID
                    )
                    if token_id == BOROS_HYPE_TOKEN_ID:
                        target_yu = float(inv_final.total_hype_exposure) * float(
                            self._planner_config.boros_coverage_target
                        )
                    else:
                        target_yu = spot_usd * float(
                            self._planner_config.boros_coverage_target
                        )
                    ok_boros, msg_boros = await self._ensure_boros_position(
                        {
                            "market_id": int(market_id),
                            "target_size_yu": float(target_yu),
                        },
                        inv_final,
                    )
                    if not ok_boros:
                        return await self._failsafe_liquidate_all(
                            f"Post-liquidation Boros recovery failed: {msg_boros}"
                        )
            except Exception as exc:  # noqa: BLE001
                return await self._failsafe_liquidate_all(
                    f"Post-liquidation Boros recovery raised: {exc}"
                )

            return (
                True,
                f"Redeployed. Spot={inv_final.total_hype_exposure:.4f} HYPE, short re-opened.",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to verify hedge after redeploy: {exc}")
            return True, "Redeployed (hedge verification pending)"

    async def _failsafe_liquidate_all(
        self: BorosHypeStrategy, reason: str
    ) -> tuple[bool, str]:
        # Called when critical operations fail; close all positions to stable assets
        logger.error(f"[FAILSAFE] Initiating full liquidation: {reason}")
        self._failsafe_triggered = True

        messages: list[str] = []

        ok_addr, address = self._require_strategy_wallet_address()
        if not ok_addr:
            msg = f"[FAILSAFE] No wallet address: {reason}"
            self._failsafe_message = msg
            return False, msg
        if not self._sign_callback:
            msg = f"[FAILSAFE] No signing callback: {reason}"
            self._failsafe_message = msg
            return False, msg
        strategy_wallet = self._config.get("strategy_wallet", {})

        if self.hyperliquid_adapter:
            try:
                ok_state, user_state = await self.hyperliquid_adapter.get_user_state(
                    address
                )
                current_short_size = 0.0
                if ok_state and isinstance(user_state, dict):
                    for pos in user_state.get("assetPositions", []):
                        p = pos.get("position", {}) if isinstance(pos, dict) else {}
                        if p.get("coin") == "HYPE":
                            szi = float(p.get("szi", 0))
                            if szi < 0:
                                current_short_size = abs(szi)
                            break

                if current_short_size > 0.01:
                    perp_asset_id = self.hyperliquid_adapter.coin_to_asset.get("HYPE")
                    if perp_asset_id is not None:
                        rounded_size = self.hyperliquid_adapter.get_valid_order_size(
                            int(perp_asset_id), current_short_size
                        )
                        if rounded_size > 0:
                            (
                                ok_close,
                                res_close,
                            ) = await self.hyperliquid_adapter.place_market_order(
                                asset_id=int(perp_asset_id),
                                is_buy=True,
                                slippage=0.05,
                                size=float(rounded_size),
                                address=address,
                                reduce_only=True,
                                builder=self.builder_fee,
                            )
                            if ok_close:
                                messages.append(f"HL short closed: {rounded_size:.4f}")
                            else:
                                messages.append(f"HL close failed: {res_close}")
                            await asyncio.sleep(2)
                else:
                    messages.append("HL short: none")
            except Exception as e:
                messages.append(f"HL close error: {e}")

        if self.boros_adapter:
            try:
                ok_pos, positions = await self.boros_adapter.get_active_positions()
                if ok_pos and isinstance(positions, list) and positions:
                    for pos in positions:
                        mid = pos.get("marketId") or pos.get("market_id")
                        try:
                            mid_int = int(mid) if mid is not None else None
                        except (TypeError, ValueError):
                            mid_int = None
                        if mid_int and mid_int > 0:
                            try:
                                await self.boros_adapter.close_positions_market(mid_int)
                                messages.append(f"Boros {mid_int} closed")
                            except Exception as exc:
                                messages.append(f"Boros {mid_int} close failed: {exc}")
                else:
                    messages.append("Boros positions: none")
            except Exception as e:
                messages.append(f"Boros close error: {e}")

        if self.brap_adapter and self.balance_adapter:
            try:
                # Swap kHYPE to HYPE
                (
                    ok_khype,
                    khype_raw,
                ) = await self.balance_adapter.get_vault_wallet_balance(KHYPE_LST)
                if ok_khype and khype_raw and int(khype_raw) > 0:
                    await self.brap_adapter.swap_from_token_ids(
                        from_token_id=KHYPE_LST,
                        to_token_id=HYPE_NATIVE,
                        from_address=address,
                        amount=str(int(khype_raw)),
                        slippage=0.02,
                        strategy_name="boros_hype_strategy",
                    )
                    messages.append("kHYPE swapped to HYPE")

                # Swap lHYPE to HYPE
                (
                    ok_lhype,
                    lhype_raw,
                ) = await self.balance_adapter.get_vault_wallet_balance(LOOPED_HYPE)
                if ok_lhype and lhype_raw and int(lhype_raw) > 0:
                    await self.brap_adapter.swap_from_token_ids(
                        from_token_id=LOOPED_HYPE,
                        to_token_id=HYPE_NATIVE,
                        from_address=address,
                        amount=str(int(lhype_raw)),
                        slippage=0.02,
                        strategy_name="boros_hype_strategy",
                    )
                    messages.append("lHYPE swapped to HYPE")
            except Exception as e:
                messages.append(f"Spot swap error: {e}")

        if self.hyperliquid_adapter and self.balance_adapter:
            try:
                # Some routes return WHYPE instead of native HYPE. Unwrap it so we can
                # send native HYPE to Hyperliquid for liquidation.
                (
                    ok_whype,
                    whype_raw,
                ) = await self.balance_adapter.get_vault_wallet_balance(WHYPE)
                if ok_whype and whype_raw and int(whype_raw) > 0:
                    ok_unwrap, unwrap_res = await self._unwrap_whype(
                        address, int(whype_raw)
                    )
                    if ok_unwrap:
                        messages.append("WHYPE unwrapped to HYPE")
                        await asyncio.sleep(2)
                    else:
                        messages.append(f"WHYPE unwrap failed: {unwrap_res}")

                ok_hype, hype_raw = await self.balance_adapter.get_vault_wallet_balance(
                    HYPE_NATIVE
                )
                hype_raw_int = (
                    int(hype_raw) if ok_hype and hype_raw and int(hype_raw) > 0 else 0
                )
                gas_reserve_wei = int(float(MIN_HYPE_GAS) * 1e18)
                hype_to_transfer_raw = max(0, hype_raw_int - gas_reserve_wei)
                hype_to_transfer = float(hype_to_transfer_raw) / 1e18

                if hype_to_transfer_raw > int(0.01 * 1e18):
                    destination = HyperliquidAdapter.hypercore_index_to_system_address(
                        150
                    )
                    ok_send, _ = await self.balance_adapter.send_to_address(
                        token_id=HYPE_NATIVE,
                        amount=int(hype_to_transfer_raw),
                        from_wallet=strategy_wallet,
                        to_address=destination,
                        signing_callback=self._sign_callback,
                    )
                    if ok_send:
                        await asyncio.sleep(3)
                        messages.append(
                            f"HYPE transferred to HL: {hype_to_transfer:.4f}"
                        )

                        # Sell HYPE for USDC on HL spot
                        (
                            ok_spot,
                            spot_state,
                        ) = await self.hyperliquid_adapter.get_spot_user_state(address)
                        spot_hype = 0.0
                        if ok_spot and isinstance(spot_state, dict):
                            for bal in spot_state.get("balances", []):
                                if (
                                    bal.get("coin") == "HYPE"
                                    or bal.get("token") == "HYPE"
                                ):
                                    spot_hype = float(bal.get("total", 0)) - float(
                                        bal.get("hold", 0)
                                    )
                                    break

                        if spot_hype > 0.01:
                            spot_asset_id, _ = await self._get_hype_asset_ids()
                            rounded_size = (
                                self.hyperliquid_adapter.get_valid_order_size(
                                    int(spot_asset_id), spot_hype
                                )
                            )
                            if rounded_size > 0:
                                await self.hyperliquid_adapter.place_market_order(
                                    asset_id=int(spot_asset_id),
                                    is_buy=False,
                                    slippage=0.10,
                                    size=float(rounded_size),
                                    address=address,
                                    builder=self.builder_fee,
                                )
                                messages.append(
                                    f"HYPE sold for USDC: {rounded_size:.4f}"
                                )
            except Exception as e:
                messages.append(f"HYPE liquidation error: {e}")

        result_msg = f"[FAILSAFE] {reason} | {'; '.join(messages)}"
        logger.error(result_msg)
        self._failsafe_message = result_msg

        return False, result_msg

    async def _partial_trim_spot(
        self: BorosHypeStrategy, params: dict[str, Any], inventory: Inventory
    ) -> tuple[bool, str]:
        trim_pct = float(params.get("trim_pct") or 0.25)

        if inventory.spot_value_usd < 10.0:
            return True, "No spot to trim"

        ok, msg = self._require_adapters(
            "balance_adapter", "hyperliquid_adapter", "brap_adapter"
        )
        if not ok:
            return False, msg
        if not self._sign_callback:
            return False, "No strategy wallet signing callback configured"

        ok_addr, address = self._require_strategy_wallet_address()
        if not ok_addr:
            return False, address
        strategy_wallet = self._config.get("strategy_wallet", {})

        hype_price = float(inventory.hype_price_usd or 0.0)
        if hype_price <= 0:
            ok_mid, mids = await self.hyperliquid_adapter.get_all_mid_prices()
            if ok_mid and isinstance(mids, dict):
                hype_price = float(mids.get("HYPE", 0.0))
        if hype_price <= 0:
            return False, "Could not determine HYPE price for trim"

        trim_usd = float(inventory.spot_value_usd) * float(trim_pct)

        # Sell kHYPE first (more liquid), then looped HYPE if needed.
        if inventory.khype_value_usd > 0 and trim_usd > 1.0:
            khype_trim_usd = min(float(inventory.khype_value_usd), trim_usd)
            if inventory.khype_to_hype_ratio > 0:
                khype_trim_tokens = (
                    khype_trim_usd / hype_price / float(inventory.khype_to_hype_ratio)
                )
                khype_trim_wei = int(khype_trim_tokens * 1e18)
                if khype_trim_wei > 0:
                    try:
                        await self.brap_adapter.swap_from_token_ids(
                            from_token_id=KHYPE_LST,
                            to_token_id=HYPE_NATIVE,
                            from_address=address,
                            amount=str(int(khype_trim_wei)),
                            slippage=0.01,
                            strategy_name="boros_hype_strategy",
                        )
                        trim_usd -= khype_trim_usd
                        await asyncio.sleep(2)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"Failed to sell kHYPE: {exc}")

        if trim_usd > 1.0 and inventory.looped_hype_value_usd > 0:
            lhype_trim_usd = min(float(inventory.looped_hype_value_usd), trim_usd)
            if inventory.looped_hype_to_hype_ratio > 0:
                lhype_trim_tokens = (
                    lhype_trim_usd
                    / hype_price
                    / float(inventory.looped_hype_to_hype_ratio)
                )
                lhype_trim_wei = int(lhype_trim_tokens * 1e18)
                if lhype_trim_wei > 0:
                    try:
                        await self.brap_adapter.swap_from_token_ids(
                            from_token_id=LOOPED_HYPE,
                            to_token_id=HYPE_NATIVE,
                            from_address=address,
                            amount=str(int(lhype_trim_wei)),
                            slippage=0.01,
                            strategy_name="boros_hype_strategy",
                        )
                        trim_usd -= lhype_trim_usd
                        await asyncio.sleep(2)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"Failed to sell looped HYPE: {exc}")

        remaining_trim_usd = max(0.0, float(trim_usd))
        gas_reserve_wei = int(float(MIN_HYPE_GAS) * 1e18)

        # If we still need to trim, move just enough HYPE from HyperEVM -> HL spot so we
        # can sell it for USDC and add to perp margin (avoid transferring all HYPE).
        if remaining_trim_usd > 1.0:
            # Some routes return WHYPE instead of native HYPE. Unwrap enough to cover the trim.
            try:
                ok_h0, hype_raw0 = await self.balance_adapter.get_vault_wallet_balance(
                    HYPE_NATIVE
                )
                ok_w0, whype_raw0 = await self.balance_adapter.get_vault_wallet_balance(
                    WHYPE
                )
                hype_raw0_int = (
                    int(hype_raw0) if ok_h0 and hype_raw0 and int(hype_raw0) > 0 else 0
                )
                whype_raw0_int = (
                    int(whype_raw0)
                    if ok_w0 and whype_raw0 and int(whype_raw0) > 0
                    else 0
                )
                available_native_tokens = (
                    float(max(0, hype_raw0_int - gas_reserve_wei)) / 1e18
                )
                needed_tokens = float(remaining_trim_usd) / float(hype_price)
                if (
                    whype_raw0_int > 0
                    and available_native_tokens + 1e-9 < needed_tokens
                ):
                    need_wei = int((needed_tokens - available_native_tokens) * 1e18)
                    unwrap_wei = min(int(need_wei), int(whype_raw0_int))
                    if unwrap_wei > 0:
                        ok_unwrap, unwrap_res = await self._unwrap_whype(
                            address, int(unwrap_wei)
                        )
                        if ok_unwrap:
                            await asyncio.sleep(2)
                        else:
                            logger.warning(
                                f"WHYPE unwrap failed during trim: {unwrap_res}"
                            )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"WHYPE unwrap pre-trim failed: {exc}")

            ok_hype, hype_raw = await self.balance_adapter.get_vault_wallet_balance(
                HYPE_NATIVE
            )
            hype_raw_int = (
                int(hype_raw) if ok_hype and hype_raw and int(hype_raw) > 0 else 0
            )
            transferable_raw = max(0, hype_raw_int - gas_reserve_wei)
            transferable_tokens = float(transferable_raw) / 1e18

            desired_tokens = min(transferable_tokens, remaining_trim_usd / hype_price)
            hype_to_transfer_raw = int(desired_tokens * 1e18)
            if hype_to_transfer_raw >= int(0.01 * 1e18):
                destination = HyperliquidAdapter.hypercore_index_to_system_address(150)
                ok_send, send_res = await self.balance_adapter.send_to_address(
                    token_id=HYPE_NATIVE,
                    amount=int(hype_to_transfer_raw),
                    from_wallet=strategy_wallet,
                    to_address=destination,
                    signing_callback=self._sign_callback,
                )
                if not ok_send:
                    return False, f"Failed to transfer HYPE to Hyperliquid: {send_res}"
                await asyncio.sleep(3)

        try:
            spot_asset_id, perp_asset_id = await self._get_hype_asset_ids()
            ok_spot, spot_state = await self.hyperliquid_adapter.get_spot_user_state(
                address
            )
            spot_hype_balance = 0.0
            if ok_spot and isinstance(spot_state, dict):
                for bal in spot_state.get("balances", []):
                    token = bal.get("coin") or bal.get("token")
                    hold = float(bal.get("hold", 0))
                    total = float(bal.get("total", 0))
                    available = total - hold
                    if token == "HYPE":
                        spot_hype_balance = available
                        break

            if spot_hype_balance > 0.01 and remaining_trim_usd > 1.0:
                desired_units = min(spot_hype_balance, remaining_trim_usd / hype_price)
                rounded_units = self.hyperliquid_adapter.get_valid_order_size(
                    int(spot_asset_id), desired_units
                )
                if rounded_units > 0:
                    if (
                        inventory.hl_short_size_hype > 0.1
                        and inventory.hl_short_size_hype
                        >= inventory.total_hype_exposure
                    ):
                        ok_lev, lev_msg = await self._ensure_hl_hype_leverage_set(
                            address
                        )
                        if not ok_lev:
                            return False, lev_msg
                        paired_filler = PairedFiller(
                            adapter=self.hyperliquid_adapter, address=address
                        )
                        (
                            _filled_spot,
                            _filled_perp,
                            _spot_notional,
                            _perp_notional,
                            spot_pointers,
                            perp_pointers,
                        ) = await paired_filler.fill_pair_units(
                            coin="HYPE",
                            spot_asset_id=int(spot_asset_id),
                            perp_asset_id=int(perp_asset_id),
                            total_units=float(rounded_units),
                            direction="short_spot_long_perp",
                            builder_fee=self.builder_fee,
                        )
                        await self._cancel_lingering_orders(
                            spot_pointers + perp_pointers, address
                        )
                        await asyncio.sleep(2)
                    else:
                        await self.hyperliquid_adapter.place_market_order(
                            asset_id=int(spot_asset_id),
                            is_buy=False,
                            slippage=0.10,
                            size=float(rounded_units),
                            address=address,
                            builder=self.builder_fee,
                        )
                        await asyncio.sleep(2)
                    remaining_trim_usd = max(
                        0.0, remaining_trim_usd - float(rounded_units) * hype_price
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to sell spot HYPE: {exc}")

        try:
            usdc_sz_decimals = (
                await self.hyperliquid_adapter.get_spot_token_sz_decimals("USDC")
            )
            if usdc_sz_decimals is None:
                usdc_sz_decimals = 2

            ok_spot, spot_state = await self.hyperliquid_adapter.get_spot_user_state(
                address
            )
            spot_usdc = 0.0
            spot_total_s = "0"
            spot_hold_s = "0"
            if ok_spot and isinstance(spot_state, dict):
                for bal in spot_state.get("balances", []):
                    token = bal.get("coin") or bal.get("token")
                    if token != "USDC":
                        continue
                    spot_total_s = str(bal.get("total", "0") or "0")
                    spot_hold_s = str(bal.get("hold", "0") or "0")
                    hold = float(spot_hold_s)
                    total = float(spot_total_s)
                    spot_usdc = max(0.0, total - hold)
                    break

            if spot_usdc > 1.0:
                amount = self.hyperliquid_adapter.max_transferable_amount(
                    spot_total_s,
                    spot_hold_s,
                    sz_decimals=int(usdc_sz_decimals),
                    leave_one_tick=True,
                )
                (
                    ok_xfer,
                    res_xfer,
                ) = await self.hyperliquid_adapter.transfer_spot_to_perp(
                    amount=float(amount),
                    address=address,
                )
                if (not ok_xfer) and int(usdc_sz_decimals) != 2:
                    if "insufficient balance" in str(res_xfer).lower():
                        fallback_2dp = self.hyperliquid_adapter.max_transferable_amount(
                            spot_total_s,
                            spot_hold_s,
                            sz_decimals=2,
                            leave_one_tick=True,
                        )
                        if fallback_2dp > 1.0:
                            await self.hyperliquid_adapter.transfer_spot_to_perp(
                                amount=float(fallback_2dp),
                                address=address,
                            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to move USDC spot→perp: {exc}")

        inv_after = await self.observe()
        ok_short, msg_short = await self._ensure_hl_short(
            {
                "target_size": inv_after.total_hype_exposure,
                "current_size": inv_after.hl_short_size_hype,
            },
            inv_after,
        )
        if not ok_short:
            return False, f"Failed to resize short after trim: {msg_short}"

        return (
            True,
            f"Trimmed spot and resized short to {inv_after.total_hype_exposure:.4f} HYPE.",
        )
