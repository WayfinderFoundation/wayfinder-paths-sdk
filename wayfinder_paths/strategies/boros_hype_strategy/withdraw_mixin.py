"""
Withdrawal operations for BorosHypeStrategy.

Kept as a mixin so the main strategy file stays readable without changing behavior.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import TYPE_CHECKING

from loguru import logger

from wayfinder_paths.adapters.hyperliquid_adapter.adapter import HyperliquidAdapter
from wayfinder_paths.adapters.hyperliquid_adapter.paired_filler import (
    MIN_NOTIONAL_USD,
    PairedFiller,
)
from wayfinder_paths.core.strategies import StatusTuple
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction

from .constants import (
    ARBITRUM_CHAIN_ID,
    BOROS_HYPE_MARKET_ID,
    BOROS_HYPE_TOKEN_ID,
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


class BorosHypeWithdrawMixin:
    async def withdraw(self, **kwargs) -> StatusTuple:
        # Liquidates to USDC on Arb but does NOT transfer to main wallet (call exit() after)
        max_wait_s = int(
            kwargs.get("max_wait_s") or kwargs.get("max_wait_seconds") or 20 * 60
        )
        poll_interval_s = int(kwargs.get("poll_interval_s") or 10)
        if max_wait_s < 0:
            max_wait_s = 0
        if poll_interval_s < 1:
            poll_interval_s = 1
        withdraw_start_ts = time.time()
        deadline_ts = withdraw_start_ts + max_wait_s

        ok, msg = self._require_adapters(
            "balance_adapter",
            "hyperliquid_adapter",
            "brap_adapter",
            "boros_adapter",
        )
        if not ok:
            return False, msg
        if not self._sign_callback:
            return False, "No strategy wallet signing callback configured"

        ok_addr, address = self._require_strategy_wallet_address()
        if not ok_addr:
            return False, address
        strategy_wallet = self._config.get("strategy_wallet", {})

        # Ensure builder fee is approved before placing any orders
        if self.hyperliquid_adapter and self.builder_fee:
            ok, msg = await self.hyperliquid_adapter.ensure_builder_fee_approved(
                address=address,
                builder_fee=self.builder_fee,
            )
            if not ok:
                # If the user has never deposited to Hyperliquid, HL rejects builder-fee approval.
                # That's not fatal for withdraw when there are no HL positions/balances to unwind.
                if "must deposit before performing actions" in str(msg).lower():
                    logger.warning(
                        f"Deferring Hyperliquid builder fee approval until after first deposit: {msg}"
                    )
                else:
                    return False, f"Builder fee approval failed: {msg}"
            else:
                logger.info(f"Builder fee status: {msg}")

        # Get inventory once - use it for initial decisions/logging.
        inv = await self.observe()
        isolated_usd = float(inv.boros_idle_collateral_isolated or 0.0) * float(
            inv.hype_price_usd or 0.0
        )
        cross_usd = float(inv.boros_idle_collateral_cross or 0.0) * float(
            inv.hype_price_usd or 0.0
        )
        logger.info(
            "Withdraw starting. Inventory: "
            f"hl_perp_margin=${inv.hl_perp_margin:.2f}, "
            f"boros_collateral=${inv.boros_collateral_usd:.2f} "
            f"(isolated={inv.boros_idle_collateral_isolated:.6f} HYPE ≈${isolated_usd:.2f}, "
            f"cross={inv.boros_idle_collateral_cross:.6f} HYPE ≈${cross_usd:.2f}), "
            f"boros_position_size=${inv.boros_position_size:.2f}, "
            f"boros_market_ids={inv.boros_position_market_ids}, "
            f"spot=${inv.spot_value_usd:.2f}, "
            f"hl_short={inv.hl_short_size_hype:.4f} HYPE"
        )

        try:
            await self._cancel_hl_open_orders_for_hype(address)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Failed to cancel HL open orders pre-withdraw: {exc}")

        # ─────────────────────────────────────────────────────────────────
        # STEP 1: Close all Boros positions (settles to USDT, no delta risk)
        # ─────────────────────────────────────────────────────────────────
        market_ids_to_close = inv.boros_position_market_ids or []
        if not market_ids_to_close and inv.boros_position_size > 0:
            try:
                ok_pos, positions = await self.boros_adapter.get_active_positions()
                if ok_pos and isinstance(positions, list):
                    mids: set[int] = set()
                    for pos in positions:
                        mid = pos.get("marketId") or pos.get("market_id")
                        try:
                            mid_int = int(mid) if mid is not None else None
                        except (TypeError, ValueError):
                            mid_int = None
                        if mid_int and mid_int > 0:
                            mids.add(mid_int)
                    market_ids_to_close = sorted(mids)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Failed to fetch Boros market IDs for close: {exc}")

        if inv.boros_position_size > 0 and not market_ids_to_close:
            logger.warning(
                f"Boros position size > 0 but no market IDs found; trying default {BOROS_HYPE_MARKET_ID}"
            )
            market_ids_to_close = [BOROS_HYPE_MARKET_ID]

        boros_position_closed = False
        for market_id in market_ids_to_close:
            try:
                ok_close, res_close = await self.boros_adapter.close_positions_market(
                    market_id, token_id=BOROS_HYPE_TOKEN_ID
                )
                if ok_close:
                    boros_position_closed = True
                    logger.info(f"Closed Boros position in market {market_id}")
                else:
                    logger.warning(
                        f"Failed to close Boros position in market {market_id}: {res_close}"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"Failed to close Boros position in market {market_id}: {exc}"
                )

        if boros_position_closed:
            await asyncio.sleep(5)

        # ─────────────────────────────────────────────────────────────────
        # STEP 2: Move isolated HYPE collateral to cross margin, then withdraw
        # ─────────────────────────────────────────────────────────────────
        boros_wait_min_hype_raw: int | None = None
        try:
            ok_bal, balances = await self.boros_adapter.get_account_balances(
                token_id=BOROS_HYPE_TOKEN_ID
            )
            if ok_bal and isinstance(balances, dict):
                isolated_hype = float(balances.get("isolated", 0.0))
                cross_hype = float(balances.get("cross", 0.0))
                total_hype = float(balances.get("total", 0.0))
                cross_hype_wei = int(balances.get("cross_wei") or 0)
                isolated_positions = balances.get("isolated_positions", [])
                logger.info(
                    "Boros balances after position close: "
                    f"isolated={isolated_hype:.6f}, cross={cross_hype:.6f}, total={total_hype:.6f}"
                )

                for iso_pos in isolated_positions:
                    iso_market_id = iso_pos.get("market_id")
                    iso_balance_wei = int(iso_pos.get("balance_wei") or 0)
                    iso_balance = float(iso_pos.get("balance", 0) or 0.0)
                    if iso_market_id and iso_balance_wei > int(0.001 * 1e18):
                        logger.info(
                            f"Moving {iso_balance:.6f} collateral from isolated market {iso_market_id} to cross"
                        )
                        ok_xfer, res_xfer = await self.boros_adapter.cash_transfer(
                            market_id=iso_market_id,  # Use actual market ID
                            amount_wei=iso_balance_wei,
                            is_deposit=False,  # isolated -> cross
                        )
                        if ok_xfer:
                            await asyncio.sleep(2)
                        else:
                            logger.warning(
                                f"Failed Boros isolated->cross transfer for market {iso_market_id}: {res_xfer}"
                            )

                # Re-fetch balances after transfers
                if isolated_positions:
                    ok_bal, balances = await self.boros_adapter.get_account_balances(
                        token_id=BOROS_HYPE_TOKEN_ID
                    )
                    if ok_bal and isinstance(balances, dict):
                        cross_hype = float(balances.get("cross", 0.0))
                        cross_hype_wei = int(balances.get("cross_wei") or 0)

                if cross_hype_wei > int(0.001 * 1e18):
                    # IMPORTANT: Use raw integer balances to avoid float rounding up
                    # and triggering a Boros revert on gas estimation.
                    withdraw_native = int(cross_hype_wei)  # HYPE native decimals (18)

                    (
                        ok_hype0,
                        res0,
                    ) = await self.balance_adapter.get_wallet_balances_multicall(
                        assets=[
                            {
                                "token_address": HYPE_OFT_ADDRESS,
                                "chain_id": ARBITRUM_CHAIN_ID,
                            }
                        ]
                    )
                    hype_raw_before_int = (
                        int(res0[0].get("balance_raw") or 0)
                        if ok_hype0
                        and isinstance(res0, list)
                        and res0
                        and res0[0].get("success")
                        else 0
                    )

                    ok_wd, res_wd = await self.boros_adapter.withdraw_collateral(
                        token_id=BOROS_HYPE_TOKEN_ID,
                        amount_native=withdraw_native,
                    )
                    if ok_wd:
                        logger.info(
                            f"Withdrew {cross_hype:.6f} HYPE collateral from Boros"
                        )
                        min_expected = max(
                            0, int(withdraw_native * 99 // 100)
                        )  # 1% tolerance
                        boros_wait_min_hype_raw = hype_raw_before_int + min_expected
                    else:
                        logger.warning(f"Failed to withdraw Boros collateral: {res_wd}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Boros collateral withdrawal step failed: {exc}")

        # ─────────────────────────────────────────────────────────────────
        # STEP 3: Sell spot positions on HyperEVM to HYPE (hedge still active)
        # ─────────────────────────────────────────────────────────────────
        try:
            ok_khype, khype_raw = await self.balance_adapter.get_vault_wallet_balance(
                KHYPE_LST
            )
            if ok_khype and khype_raw > 0:
                ok, res = await self.brap_adapter.swap_from_token_ids(
                    from_token_id=KHYPE_LST,
                    to_token_id=HYPE_NATIVE,
                    from_address=address,
                    amount=str(int(khype_raw)),
                    slippage=0.01,
                    strategy_name="boros_hype_strategy",
                )
                if ok:
                    logger.info(f"Sold kHYPE → HYPE: {khype_raw / 1e18:.4f} kHYPE")
                    await asyncio.sleep(2)
                else:
                    logger.warning(f"Failed to sell kHYPE → HYPE: {res}")

            ok_lhype, lhype_raw = await self.balance_adapter.get_vault_wallet_balance(
                LOOPED_HYPE
            )
            if ok_lhype and lhype_raw > 0:
                ok, res = await self.brap_adapter.swap_from_token_ids(
                    from_token_id=LOOPED_HYPE,
                    to_token_id=HYPE_NATIVE,
                    from_address=address,
                    amount=str(int(lhype_raw)),
                    slippage=0.01,
                    strategy_name="boros_hype_strategy",
                )
                if ok:
                    logger.info(
                        f"Sold looped HYPE → HYPE: {lhype_raw / 1e18:.4f} lHYPE"
                    )
                    await asyncio.sleep(2)
                else:
                    logger.warning(f"Failed to sell looped HYPE → HYPE: {res}")

            # Check for WHYPE and unwrap if present (swap may output WHYPE instead of native HYPE)
            ok_whype, whype_raw = await self.balance_adapter.get_vault_wallet_balance(
                WHYPE
            )
            if ok_whype and whype_raw > 0:
                logger.info(
                    f"Unwrapping {float(whype_raw) / 1e18:.4f} WHYPE to native HYPE"
                )
                ok_unwrap, unwrap_res = await self._unwrap_whype(
                    address, int(whype_raw)
                )
                if ok_unwrap:
                    await asyncio.sleep(2)
                else:
                    logger.warning(f"WHYPE unwrap failed: {unwrap_res}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed selling HyperEVM spot to HYPE: {exc}")

        # ─────────────────────────────────────────────────────────────────
        # STEP 4: Transfer HYPE from HyperEVM to Hyperliquid spot (keep gas)
        # ─────────────────────────────────────────────────────────────────
        sent_hype_to_hl = False
        sent_hype_to_hl_amount_hype = 0.0
        try:
            ok_hype, hype_raw = await self.balance_adapter.get_vault_wallet_balance(
                HYPE_NATIVE
            )
            hype_raw_int = int(hype_raw) if ok_hype and hype_raw and hype_raw > 0 else 0
            gas_reserve_wei = int(float(MIN_HYPE_GAS) * 1e18)
            hype_to_transfer_raw = max(0, hype_raw_int - gas_reserve_wei)
            hype_to_transfer = float(hype_to_transfer_raw) / 1e18

            if hype_to_transfer_raw > int(0.01 * 1e18):
                destination = HyperliquidAdapter.hypercore_index_to_system_address(
                    150
                )  # native HYPE
                ok_send, send_res = await self.balance_adapter.send_to_address(
                    token_id=HYPE_NATIVE,
                    amount=int(hype_to_transfer_raw),
                    from_wallet=strategy_wallet,
                    to_address=destination,
                    signing_callback=self._sign_callback,
                )
                if ok_send:
                    logger.info(
                        f"Transferred {hype_to_transfer:.4f} HYPE to Hyperliquid spot (kept {MIN_HYPE_GAS} for gas)"
                    )
                    sent_hype_to_hl = True
                    sent_hype_to_hl_amount_hype = float(hype_to_transfer)
                    await asyncio.sleep(3)
                else:
                    logger.warning(
                        f"Failed to transfer HYPE to Hyperliquid: {send_res}"
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to transfer HYPE to Hyperliquid: {exc}")

        # ─────────────────────────────────────────────────────────────────
        # STEP 4B: Bridge Arbitrum OFT HYPE -> HyperEVM, then forward to HL
        # Boros withdrawals can take ~15 minutes; do NOT close the HL hedge
        # until this HYPE is tradable and sold.
        # ─────────────────────────────────────────────────────────────────
        try:
            if self.balance_adapter and self.boros_adapter:
                # 1) Wait for Boros withdrawal to arrive on Arbitrum (OFT token)
                if boros_wait_min_hype_raw is not None:
                    logger.info(
                        "Waiting for Boros OFT HYPE to arrive on Arbitrum before unwinding hedge..."
                    )
                    last_log_ts = 0.0
                    while time.time() < deadline_ts:
                        (
                            ok_oft,
                            res_oft,
                        ) = await self.balance_adapter.get_wallet_balances_multicall(
                            assets=[
                                {
                                    "token_address": HYPE_OFT_ADDRESS,
                                    "chain_id": ARBITRUM_CHAIN_ID,
                                }
                            ]
                        )
                        oft_raw = (
                            int(res_oft[0].get("balance_raw") or 0)
                            if ok_oft
                            and isinstance(res_oft, list)
                            and res_oft
                            and res_oft[0].get("success")
                            else 0
                        )
                        if oft_raw >= int(boros_wait_min_hype_raw):
                            break
                        now_ts = time.time()
                        if now_ts - last_log_ts >= 60:
                            logger.info(
                                f"OFT HYPE on Arbitrum: {oft_raw / 1e18:.6f} / "
                                f"{int(boros_wait_min_hype_raw) / 1e18:.6f}"
                            )
                            last_log_ts = now_ts
                        await asyncio.sleep(poll_interval_s)

                    # If we're out of time, stop early so we don't unwind into a one-leg.
                    if time.time() >= deadline_ts:
                        return False, (
                            "Withdrawal paused: waiting for Boros OFT HYPE to arrive on Arbitrum timed out. "
                            "Re-run withdraw with a higher max_wait_s."
                        )

                # 2) If any OFT HYPE exists on Arbitrum, bridge it back to HyperEVM
                (
                    ok_oft,
                    res_oft,
                ) = await self.balance_adapter.get_wallet_balances_multicall(
                    assets=[
                        {
                            "token_address": HYPE_OFT_ADDRESS,
                            "chain_id": ARBITRUM_CHAIN_ID,
                        }
                    ]
                )
                oft_raw = (
                    int(res_oft[0].get("balance_raw") or 0)
                    if ok_oft
                    and isinstance(res_oft, list)
                    and res_oft
                    and res_oft[0].get("success")
                    else 0
                )
                if oft_raw > int(0.0001 * 1e18):
                    (
                        ok_h0,
                        hype_raw0,
                    ) = await self.balance_adapter.get_vault_wallet_balance(HYPE_NATIVE)
                    hype_raw0_int = (
                        int(hype_raw0) if ok_h0 and hype_raw0 and hype_raw0 > 0 else 0
                    )

                    (
                        ok_bridge,
                        bridge_res,
                    ) = await self.boros_adapter.bridge_hype_oft_arbitrum_to_hyperevm(
                        amount_wei=int(oft_raw),
                        from_address=address,
                        to_address=address,
                    )
                    if not ok_bridge:
                        return False, (
                            "Withdrawal paused: failed bridging Arbitrum OFT HYPE back to HyperEVM. "
                            f"Error: {bridge_res}"
                        )

                    bridged_amount_wei = int(bridge_res.get("amount_wei") or 0)
                    if bridged_amount_wei > 0:
                        logger.info(
                            f"Bridged {bridged_amount_wei / 1e18:.6f} OFT HYPE from Arbitrum → HyperEVM"
                        )

                        # Wait for arrival on HyperEVM (bridge is async)
                        logger.info(
                            "Waiting for bridged HYPE to arrive on HyperEVM before selling/closing hedge..."
                        )
                        target = hype_raw0_int + int(bridged_amount_wei * 0.95)
                        last_log_ts = 0.0
                        while time.time() < deadline_ts:
                            (
                                ok_h1,
                                hype_raw1,
                            ) = await self.balance_adapter.get_vault_wallet_balance(
                                HYPE_NATIVE
                            )
                            hype_raw1_int = (
                                int(hype_raw1)
                                if ok_h1 and hype_raw1 and hype_raw1 > 0
                                else 0
                            )
                            if hype_raw1_int >= target:
                                break
                            now_ts = time.time()
                            if now_ts - last_log_ts >= 60:
                                logger.info(
                                    f"HyperEVM HYPE after bridge: {hype_raw1_int / 1e18:.6f} / {target / 1e18:.6f}"
                                )
                                last_log_ts = now_ts
                            await asyncio.sleep(poll_interval_s)

                        if time.time() >= deadline_ts:
                            return False, (
                                "Withdrawal paused: OFT bridge sent but HYPE not visible on HyperEVM yet. "
                                "Re-run withdraw to continue."
                            )

                        # Forward newly-arrived HYPE from HyperEVM -> HL spot (keep gas)
                        (
                            ok_hype,
                            hype_raw,
                        ) = await self.balance_adapter.get_vault_wallet_balance(
                            HYPE_NATIVE
                        )
                        hype_raw_int = (
                            int(hype_raw)
                            if ok_hype and hype_raw and hype_raw > 0
                            else 0
                        )
                        gas_reserve_wei = int(float(MIN_HYPE_GAS) * 1e18)
                        hype_to_transfer_raw = max(0, hype_raw_int - gas_reserve_wei)
                        hype_to_transfer = float(hype_to_transfer_raw) / 1e18

                        if hype_to_transfer_raw > int(0.01 * 1e18):
                            destination = (
                                HyperliquidAdapter.hypercore_index_to_system_address(
                                    150
                                )
                            )
                            (
                                ok_send,
                                send_res,
                            ) = await self.balance_adapter.send_to_address(
                                token_id=HYPE_NATIVE,
                                amount=int(hype_to_transfer_raw),
                                from_wallet=strategy_wallet,
                                to_address=destination,
                                signing_callback=self._sign_callback,
                            )
                            if ok_send:
                                logger.info(
                                    f"Transferred {hype_to_transfer:.4f} bridged HYPE to Hyperliquid spot (kept {MIN_HYPE_GAS} for gas)"
                                )
                                sent_hype_to_hl = True
                                sent_hype_to_hl_amount_hype = float(
                                    sent_hype_to_hl_amount_hype + hype_to_transfer
                                )
                                await asyncio.sleep(3)
                            else:
                                return False, (
                                    "Withdrawal paused: HYPE arrived on HyperEVM but transfer to Hyperliquid failed. "
                                    f"Error: {send_res}"
                                )

                # We handle Boros/OFT earlier; do not wait on an increasing Arb OFT balance later.
                boros_wait_min_hype_raw = None
        except Exception as exc:  # noqa: BLE001
            return False, f"Withdrawal paused: failed bridging Boros OFT HYPE: {exc}"

        # ─────────────────────────────────────────────────────────────────
        # SAFETY CHECK: Verify delta-neutral before closing perp
        # If significant HyperEVM value AND perp exists, wait for spot to arrive
        # ─────────────────────────────────────────────────────────────────
        hyperevm_hype_value = 0.0
        try:
            ok_hype, hype_raw = await self.balance_adapter.get_vault_wallet_balance(
                HYPE_NATIVE
            )
            ok_whype, whype_raw = await self.balance_adapter.get_vault_wallet_balance(
                WHYPE
            )
            ok_khype, khype_raw = await self.balance_adapter.get_vault_wallet_balance(
                KHYPE_LST
            )
            ok_lhype, lhype_raw = await self.balance_adapter.get_vault_wallet_balance(
                LOOPED_HYPE
            )

            # Calculate total HYPE-equivalent value on HyperEVM (above gas reserve)
            native_hype = (float(hype_raw) / 1e18) if ok_hype and hype_raw > 0 else 0.0
            whype_bal = (float(whype_raw) / 1e18) if ok_whype and whype_raw > 0 else 0.0
            khype_bal = (float(khype_raw) / 1e18) if ok_khype and khype_raw > 0 else 0.0
            lhype_bal = (float(lhype_raw) / 1e18) if ok_lhype and lhype_raw > 0 else 0.0

            # Native HYPE above gas reserve
            hedgeable_hype = max(0.0, native_hype - MIN_HYPE_GAS)
            # WHYPE is 1:1, LSTs use approximate ratio (close enough for safety check)
            hyperevm_hype_value = (
                hedgeable_hype + whype_bal + khype_bal * 1.1 + lhype_bal * 1.1
            )

            logger.info(
                f"HyperEVM balances: native={native_hype:.4f}, whype={whype_bal:.4f}, "
                f"khype={khype_bal:.4f}, lhype={lhype_bal:.4f}, total={hyperevm_hype_value:.4f}"
            )
        except Exception as exc:
            logger.debug(f"Error checking HyperEVM balances: {exc}")

        spot_hype_for_check = 0.0
        perp_short_for_check = 0.0

        try:
            (
                ok_spot_chk,
                spot_state_chk,
            ) = await self.hyperliquid_adapter.get_spot_user_state(address)
            if ok_spot_chk and isinstance(spot_state_chk, dict):
                for bal in spot_state_chk.get("balances", []):
                    if (bal.get("coin") or bal.get("token")) == "HYPE":
                        spot_hype_for_check = float(bal.get("total", 0)) - float(
                            bal.get("hold", 0)
                        )
                        break
        except Exception as exc:
            logger.debug(f"Error checking HL spot state: {exc}")

        try:
            ok_perp_chk, perp_state_chk = await self.hyperliquid_adapter.get_user_state(
                address
            )
            if ok_perp_chk and isinstance(perp_state_chk, dict):
                for pos in perp_state_chk.get("assetPositions", []):
                    p = pos.get("position", {}) if isinstance(pos, dict) else {}
                    if p.get("coin") == "HYPE" and float(p.get("szi", 0)) < 0:
                        perp_short_for_check = abs(float(p.get("szi", 0)))
                        break
        except Exception as exc:
            logger.debug(f"Error checking perp state: {exc}")

        logger.info(
            f"Pre-unwind check: hl_spot_hype={spot_hype_for_check:.4f}, "
            f"perp_short={perp_short_for_check:.4f}, hyperevm_hype_value={hyperevm_hype_value:.4f}"
        )

        dust_threshold = 0.1

        if sent_hype_to_hl and spot_hype_for_check < 0.01:
            logger.info(
                f"Sent {sent_hype_to_hl_amount_hype:.4f} HYPE to HL spot but balance not visible yet; waiting..."
            )
            for attempt in range(6):  # 60s max additional wait
                await asyncio.sleep(10)
                try:
                    (
                        ok_spot_chk,
                        spot_state_chk,
                    ) = await self.hyperliquid_adapter.get_spot_user_state(address)
                    if ok_spot_chk and isinstance(spot_state_chk, dict):
                        for bal in spot_state_chk.get("balances", []):
                            if (bal.get("coin") or bal.get("token")) == "HYPE":
                                spot_hype_for_check = float(
                                    bal.get("total", 0)
                                ) - float(bal.get("hold", 0))
                                break
                    if spot_hype_for_check > 0.01:
                        logger.info(
                            f"HYPE arrived on HL spot: {spot_hype_for_check:.4f}"
                        )
                        break
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"Error re-checking HL spot: {exc}")
                logger.info(
                    f"Still waiting for HYPE to arrive on HL spot (attempt {attempt + 1}/6)"
                )

        if (
            hyperevm_hype_value > dust_threshold
            and perp_short_for_check > dust_threshold
        ):
            logger.warning(
                f"Significant value on HyperEVM ({hyperevm_hype_value:.4f} HYPE) "
                f"but perp short still open ({perp_short_for_check:.4f}). "
                "Waiting for spot to arrive on HL before closing perp..."
            )
            for attempt in range(6):  # 60s max additional wait
                await asyncio.sleep(10)
                try:
                    (
                        ok_spot_chk,
                        spot_state_chk,
                    ) = await self.hyperliquid_adapter.get_spot_user_state(address)
                    if ok_spot_chk and isinstance(spot_state_chk, dict):
                        for bal in spot_state_chk.get("balances", []):
                            if (bal.get("coin") or bal.get("token")) == "HYPE":
                                spot_hype_for_check = float(
                                    bal.get("total", 0)
                                ) - float(bal.get("hold", 0))
                                break
                    if spot_hype_for_check > dust_threshold:
                        logger.info(
                            f"HYPE arrived on HL spot: {spot_hype_for_check:.4f}"
                        )
                        break
                except Exception as exc:
                    logger.debug(f"Error re-checking HL spot: {exc}")
                logger.info(
                    f"Still waiting for HYPE to arrive on HL spot (attempt {attempt + 1}/6)"
                )

        if (
            spot_hype_for_check > dust_threshold
            and perp_short_for_check < dust_threshold
        ):
            logger.info(
                "Spot HYPE present, perp already closed - proceeding with spot sale only"
            )
        elif (
            perp_short_for_check > dust_threshold
            and spot_hype_for_check < dust_threshold
        ):
            if hyperevm_hype_value < dust_threshold:
                logger.info(
                    "Perp short present, no spot - proceeding with perp close only (one-leg scenario)"
                )
            else:
                logger.warning(
                    f"Perp short present but spot HYPE not yet on HL "
                    f"(HyperEVM value: {hyperevm_hype_value:.4f}) - delta risk!"
                )

        # ─────────────────────────────────────────────────────────────────
        # STEP 5: Unwind HYPE exposure on Hyperliquid (paired when possible)
        # ─────────────────────────────────────────────────────────────────
        sold_hl_spot_hype = False

        try:
            await self._cancel_hl_open_orders_for_hype(address)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Failed to cancel HL open orders before unwind: {exc}")

        try:
            spot_asset_id, perp_asset_id = await self._get_hype_asset_ids()
        except Exception as exc:  # noqa: BLE001
            return False, f"Failed to resolve Hyperliquid HYPE asset ids: {exc}"

        try:
            hype_price_usd = float(inv.hype_price_usd or 0.0)

            ok_spot, spot_state = await self.hyperliquid_adapter.get_spot_user_state(
                address
            )
            spot_hype_balance = 0.0
            if ok_spot and isinstance(spot_state, dict):
                for bal in spot_state.get("balances", []):
                    token = bal.get("coin") or bal.get("token")
                    if token != "HYPE":
                        continue
                    hold = float(bal.get("hold", 0))
                    total = float(bal.get("total", 0))
                    spot_hype_balance = max(0.0, total - hold)
                    break

            ok_state, user_state = await self.hyperliquid_adapter.get_user_state(
                address
            )
            current_short_size = 0.0
            if ok_state and isinstance(user_state, dict):
                for pos in user_state.get("assetPositions", []):
                    p = pos.get("position", {}) if isinstance(pos, dict) else {}
                    if p.get("coin") != "HYPE":
                        continue
                    szi = float(p.get("szi", 0))
                    if szi < 0:
                        current_short_size = abs(szi)
                    break

            # Paired unwind: sell spot + buy perp (reduce short) together to avoid one-leg risk.
            if spot_hype_balance > 0.01 and current_short_size > 0.01:
                pair_units = min(spot_hype_balance, current_short_size)
                if (pair_units * hype_price_usd) >= MIN_NOTIONAL_USD:
                    paired_filler = PairedFiller(
                        adapter=self.hyperliquid_adapter, address=address
                    )
                    (
                        filled_spot,
                        filled_perp,
                        spot_notional,
                        perp_notional,
                        spot_pointers,
                        perp_pointers,
                    ) = await paired_filler.fill_pair_units(
                        coin="HYPE",
                        spot_asset_id=int(spot_asset_id),
                        perp_asset_id=int(perp_asset_id),
                        total_units=float(pair_units),
                        direction="short_spot_long_perp",
                        builder_fee=self.builder_fee,
                    )
                    sold_hl_spot_hype = filled_spot > 0.0
                    logger.info(
                        f"Paired unwind complete: sold_spot={filled_spot:.4f} (${spot_notional:.2f}), "
                        f"closed_perp={filled_perp:.4f} (${perp_notional:.2f})"
                    )
                    await self._cancel_lingering_orders(
                        spot_pointers + perp_pointers, address
                    )
                    await asyncio.sleep(2)

                    # Refresh post-paired balances
                    (
                        ok_spot,
                        spot_state,
                    ) = await self.hyperliquid_adapter.get_spot_user_state(address)
                    spot_hype_balance = 0.0
                    if ok_spot and isinstance(spot_state, dict):
                        for bal in spot_state.get("balances", []):
                            token = bal.get("coin") or bal.get("token")
                            if token != "HYPE":
                                continue
                            hold = float(bal.get("hold", 0))
                            total = float(bal.get("total", 0))
                            spot_hype_balance = max(0.0, total - hold)
                            break

                    (
                        ok_state,
                        user_state,
                    ) = await self.hyperliquid_adapter.get_user_state(address)
                    current_short_size = 0.0
                    if ok_state and isinstance(user_state, dict):
                        for pos in user_state.get("assetPositions", []):
                            p = pos.get("position", {}) if isinstance(pos, dict) else {}
                            if p.get("coin") != "HYPE":
                                continue
                            szi = float(p.get("szi", 0))
                            if szi < 0:
                                current_short_size = abs(szi)
                            break

            # Sell any remaining spot HYPE to USDC
            if spot_hype_balance > 0.01:
                rounded_size = self.hyperliquid_adapter.get_valid_order_size(
                    int(spot_asset_id), spot_hype_balance
                )
                if rounded_size > 0:
                    (
                        ok_sell,
                        res_sell,
                    ) = await self.hyperliquid_adapter.place_market_order(
                        asset_id=int(spot_asset_id),
                        is_buy=False,
                        slippage=0.10,
                        size=float(rounded_size),
                        address=address,
                        builder=self.builder_fee,
                    )
                    if not ok_sell:
                        return False, f"Failed to sell HL spot HYPE: {res_sell}"
                    logger.info(f"Sold {rounded_size:.4f} HYPE to USDC on HL spot")
                    sold_hl_spot_hype = True
                    await asyncio.sleep(10)  # HL spot trades need time to clear hold

            # Close any remaining perp short (use 0.001 threshold to catch dust)
            if current_short_size > 0.001:
                rounded_size = self.hyperliquid_adapter.get_valid_order_size(
                    int(perp_asset_id), current_short_size
                )
                meets_notional = (
                    rounded_size > 0
                    and (rounded_size * hype_price_usd) >= MIN_NOTIONAL_USD
                )
                # For dust positions below minimum notional, still attempt close
                # Hyperliquid may accept reduce_only orders even below opening minimums
                is_dust_position = (
                    rounded_size > 0
                    and not meets_notional
                    and (rounded_size * hype_price_usd) > 0.01
                )
                if meets_notional or is_dust_position:
                    if is_dust_position:
                        logger.info(
                            f"Attempting to close dust perp position: {rounded_size:.4f} HYPE "
                            f"(${rounded_size * hype_price_usd:.2f} < ${MIN_NOTIONAL_USD} min)"
                        )
                    (
                        ok_close,
                        res_close,
                    ) = await self.hyperliquid_adapter.place_market_order(
                        asset_id=int(perp_asset_id),
                        is_buy=True,  # buy to close short
                        slippage=0.01,
                        size=float(rounded_size),
                        address=address,
                        reduce_only=True,
                        builder=self.builder_fee,
                    )
                    if not ok_close:
                        if is_dust_position:
                            # Dust close failed - log warning but don't fail withdrawal
                            logger.warning(
                                f"Could not close dust perp position ({rounded_size:.4f} HYPE): {res_close}. "
                                "Position requires margin, will adjust withdrawal amount."
                            )
                        else:
                            return False, f"Failed to close HL hedge: {res_close}"
                    else:
                        logger.info(f"Closed HL perp short: {rounded_size:.4f} HYPE")
                        await asyncio.sleep(2)
        except Exception as exc:  # noqa: BLE001
            return False, f"Failed to unwind HYPE on Hyperliquid: {exc}"

        # ─────────────────────────────────────────────────────────────────
        # STEP 7: Move all USDC from spot to perp margin (poll until cleared)
        # ─────────────────────────────────────────────────────────────────
        usdc_sz_decimals = await self.hyperliquid_adapter.get_spot_token_sz_decimals(
            "USDC"
        )
        if usdc_sz_decimals is None:
            usdc_sz_decimals = 2

        spot_transfer_succeeded = False
        did_transfer_spot_usdc_to_perp = False
        observed_spot_usdc_after_sell = not sold_hl_spot_hype
        spot_total = 0.0
        spot_hold = 0.0
        spot_usdc = 0.0
        attempt = 0
        while True:
            attempt += 1
            try:
                spot_total = 0.0
                spot_hold = 0.0
                spot_usdc = 0.0
                spot_total_s = "0"
                spot_hold_s = "0"

                (
                    ok_spot,
                    spot_state,
                ) = await self.hyperliquid_adapter.get_spot_user_state(address)
                if ok_spot and isinstance(spot_state, dict):
                    for bal in spot_state.get("balances", []):
                        token = bal.get("coin") or bal.get("token")
                        if token == "USDC":
                            spot_total_s = str(bal.get("total", "0") or "0")
                            spot_hold_s = str(bal.get("hold", "0") or "0")
                            spot_hold = float(spot_hold_s)
                            spot_total = float(spot_total_s)
                            spot_usdc = spot_total - spot_hold
                            break

                logger.info(
                    f"Spot USDC balance (attempt {attempt}): "
                    f"total={spot_total:.2f}, hold={spot_hold:.2f}, available={spot_usdc:.2f}"
                )

                if not observed_spot_usdc_after_sell:
                    if spot_total > 1.0 or spot_hold > 0.5 or spot_usdc > 1.0:
                        observed_spot_usdc_after_sell = True
                    else:
                        if time.time() >= deadline_ts:
                            break
                        logger.info(
                            "Waiting for HL spot USDC to settle after HYPE sale..."
                        )
                        await asyncio.sleep(poll_interval_s)
                        continue

                if spot_total <= 1.0:
                    # No significant USDC remaining on spot (including hold), nothing to transfer.
                    spot_transfer_succeeded = True
                    break

                if spot_usdc > 1.0:
                    # Compute a safe amount using Decimal math and szDecimals, leaving 1 tick.
                    spot_usdc_to_xfer = (
                        self.hyperliquid_adapter.max_transferable_amount(
                            spot_total_s,
                            spot_hold_s,
                            sz_decimals=int(usdc_sz_decimals),
                            leave_one_tick=True,
                        )
                    )
                    # Fallback: some Hyperliquid client versions effectively round to 2dp
                    # internally for usdClassTransfer. If we get an "insufficient balance"
                    # error, retry with 2dp floor.
                    fallback_2dp = (
                        self.hyperliquid_adapter.max_transferable_amount(
                            spot_total_s,
                            spot_hold_s,
                            sz_decimals=2,
                            leave_one_tick=True,
                        )
                        if int(usdc_sz_decimals) != 2
                        else 0.0
                    )

                    if spot_usdc_to_xfer <= 1.0:
                        if time.time() >= deadline_ts:
                            break
                        await asyncio.sleep(poll_interval_s)
                        continue

                    # Transfer the full available amount (fresh balance query each attempt)
                    (
                        ok_xfer,
                        res_xfer,
                    ) = await self.hyperliquid_adapter.transfer_spot_to_perp(
                        amount=float(spot_usdc_to_xfer),
                        address=address,
                    )
                    if ok_xfer:
                        logger.info(
                            f"Transferred ${spot_usdc_to_xfer:.2f} USDC from spot to perp"
                        )
                        did_transfer_spot_usdc_to_perp = True
                        await asyncio.sleep(3)
                    else:
                        res_s = str(res_xfer)
                        if fallback_2dp > 1.0 and (
                            "insufficient balance" in res_s.lower()
                        ):
                            (
                                ok_xfer2,
                                res_xfer2,
                            ) = await self.hyperliquid_adapter.transfer_spot_to_perp(
                                amount=float(fallback_2dp),
                                address=address,
                            )
                            if ok_xfer2:
                                logger.info(
                                    f"Transferred ${fallback_2dp:.2f} USDC from spot to perp (2dp fallback)"
                                )
                                did_transfer_spot_usdc_to_perp = True
                                await asyncio.sleep(3)
                                continue
                            res_xfer = res_xfer2

                        logger.warning(
                            f"Failed to move USDC spot→perp (attempt {attempt}): {res_xfer}"
                        )
                else:
                    # USDC exists but is still held (trade settlement). Wait and retry.
                    if time.time() >= deadline_ts:
                        break
                    await asyncio.sleep(poll_interval_s)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"Failed to move USDC spot→perp (attempt {attempt}): {exc}"
                )
                if time.time() >= deadline_ts:
                    break
                await asyncio.sleep(poll_interval_s)

        remaining_spot_usdc = 0.0
        if not spot_transfer_succeeded:
            remaining_spot_usdc = spot_total
            logger.error(
                f"Failed to transfer spot USDC to perp before timeout. "
                f"Spot USDC may still be on Hyperliquid spot account (total=${spot_total:.2f}, hold=${spot_hold:.2f})."
            )

        # ─────────────────────────────────────────────────────────────────
        # STEP 8: Withdraw all from Hyperliquid to Arbitrum
        # ─────────────────────────────────────────────────────────────────
        hl_wait_min_usdc_raw: int | None = None
        try:
            attempt = 0
            while True:
                attempt += 1
                ok_state, user_state = await self.hyperliquid_adapter.get_user_state(
                    address
                )
                perp_balance = (
                    self.hyperliquid_adapter.get_perp_margin_amount(user_state)
                    if ok_state and isinstance(user_state, dict)
                    else 0.0
                )

                if perp_balance > 1.0:
                    # Check for remaining open positions that require margin
                    positions = user_state.get("assetPositions", [])
                    has_dust_position = False
                    dust_position_margin = 0.0
                    for pos in positions:
                        p = pos.get("position", {}) if isinstance(pos, dict) else {}
                        szi = abs(float(p.get("szi", 0)))
                        if szi > 0.001:
                            has_dust_position = True
                            # Estimate margin required: use position value / leverage (assume 5x)
                            # or use the reported margin if available
                            pos_value = float(p.get("positionValue", 0))
                            if pos_value > 0:
                                dust_position_margin += (
                                    pos_value / 5.0
                                )  # Conservative 5x estimate
                            else:
                                # Fallback: reserve $2 per dust position
                                dust_position_margin += 2.0
                            logger.info(
                                f"Dust position detected: {p.get('coin')} size={szi:.4f}, "
                                f"reserving ~${dust_position_margin:.2f} margin"
                            )

                    # Calculate withdrawable amount
                    if has_dust_position:
                        # Leave buffer for dust position margin (minimum $2, or estimated margin + $1 buffer)
                        margin_buffer = max(2.0, dust_position_margin + 1.0)
                        amount_to_withdraw = max(0.0, perp_balance - margin_buffer)
                        # Floor to 2 decimals for Hyperliquid
                        amount_to_withdraw = math.floor(amount_to_withdraw * 100) / 100
                        if amount_to_withdraw < 1.0:
                            logger.warning(
                                f"Cannot withdraw - dust position requires margin. "
                                f"Balance: ${perp_balance:.2f}, required margin: ~${margin_buffer:.2f}"
                            )
                            break
                        logger.info(
                            f"Withdrawing ${amount_to_withdraw:.2f} (leaving ${margin_buffer:.2f} for dust position margin)"
                        )
                    else:
                        # No positions - withdraw full balance (floored to 2 decimals)
                        amount_to_withdraw = math.floor(perp_balance * 100) / 100

                    (
                        ok_usdc,
                        usdc_raw_before,
                    ) = await self.balance_adapter.get_vault_wallet_balance(USDC_ARB)
                    usdc_raw_before_int = int(usdc_raw_before) if ok_usdc else 0
                    expected_usdc_raw = max(0, int(float(amount_to_withdraw) * 1e6))
                    ok_wd, res_wd = await self.hyperliquid_adapter.withdraw(
                        amount=float(amount_to_withdraw),
                        address=address,
                    )
                    if ok_wd:
                        min_expected = max(int(1e6), int(expected_usdc_raw * 0.99))
                        hl_wait_min_usdc_raw = usdc_raw_before_int + min_expected
                        break
                    # If withdrawal failed and we have a dust position, it might need more margin
                    if (
                        has_dust_position
                        and "insufficient balance" in str(res_wd).lower()
                    ):
                        logger.warning(
                            f"Withdrawal failed due to dust position margin requirement. "
                            f"Attempted: ${amount_to_withdraw:.2f}, error: {res_wd}"
                        )
                        break
                    logger.warning(f"Failed to withdraw from Hyperliquid: {res_wd}")
                    if time.time() >= deadline_ts:
                        break
                    await asyncio.sleep(poll_interval_s)
                    continue

                # No perp balance yet. If we just moved spot→perp, wait for it to reflect.
                if not did_transfer_spot_usdc_to_perp:
                    break
                if time.time() >= deadline_ts:
                    break
                logger.info(
                    f"Waiting for spot→perp transfer to reflect in HL margin (attempt {attempt})..."
                )
                await asyncio.sleep(poll_interval_s)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed Hyperliquid withdrawal: {exc}")

        # ─────────────────────────────────────────────────────────────────
        # WAIT: Boros + Hyperliquid withdrawals concurrently
        # ─────────────────────────────────────────────────────────────────
        async def _wait_for_wallet_balance_at_least(
            *,
            token_id: str | None = None,
            token_address: str | None = None,
            chain_id: int | None = None,
            min_raw: int,
        ) -> tuple[bool, int]:
            label = token_id or f"{token_address}@{chain_id}"
            last_log_ts = 0.0

            def _fmt(raw: int) -> str:
                if token_id in {USDC_ARB, USDT_ARB}:
                    return f"{raw / 1e6:.2f}"
                if (
                    token_address
                    and chain_id == ARBITRUM_CHAIN_ID
                    and token_address.lower() == HYPE_OFT_ADDRESS.lower()
                ):
                    return f"{raw / 1e18:.6f}"
                return str(raw)

            if token_address and chain_id is not None:
                assets = [{"token_address": token_address, "chain_id": int(chain_id)}]

                async def _get_raw() -> tuple[bool, int]:
                    ok, res = await self.balance_adapter.get_wallet_balances_multicall(
                        assets=assets
                    )
                    if not ok or not isinstance(res, list) or not res:
                        return False, 0
                    item = res[0]
                    if not item.get("success"):
                        return False, 0
                    return True, int(item.get("balance_raw") or 0)

                if min_raw <= 0:
                    return await _get_raw()
            else:
                if token_id is None:
                    return False, 0

                async def _get_raw() -> tuple[bool, int]:
                    ok, raw = await self.balance_adapter.get_vault_wallet_balance(
                        token_id
                    )
                    return bool(ok), int(raw) if ok else 0

                if min_raw <= 0:
                    return await _get_raw()

            deadline = deadline_ts
            last_raw = 0
            # Allow $0.50 (500000 raw) tolerance for USDC/USDT rounding differences
            tolerance = 500_000 if token_id in {USDC_ARB, USDT_ARB} else 0
            while True:
                ok, raw = await _get_raw()
                if ok:
                    last_raw = int(raw)
                    if last_raw + tolerance >= int(min_raw):
                        return True, last_raw
                    now_ts = time.time()
                    if now_ts - last_log_ts >= 60:
                        logger.info(
                            f"Waiting for {label} balance: {_fmt(last_raw)} / {_fmt(int(min_raw))}"
                        )
                        last_log_ts = now_ts
                if time.time() >= deadline:
                    return False, last_raw
                await asyncio.sleep(poll_interval_s)

        wait_tasks: list[asyncio.Task] = []
        if max_wait_s > 0 and time.time() < deadline_ts:
            if boros_wait_min_hype_raw is not None:
                wait_tasks.append(
                    asyncio.create_task(
                        _wait_for_wallet_balance_at_least(
                            token_address=HYPE_OFT_ADDRESS,
                            chain_id=ARBITRUM_CHAIN_ID,
                            min_raw=int(boros_wait_min_hype_raw),
                        )
                    )
                )
            if hl_wait_min_usdc_raw is not None:
                wait_tasks.append(
                    asyncio.create_task(
                        _wait_for_wallet_balance_at_least(
                            token_id=USDC_ARB,
                            min_raw=int(hl_wait_min_usdc_raw),
                        )
                    )
                )

        if wait_tasks:
            try:
                await asyncio.gather(*wait_tasks)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Withdrawal wait phase errored: {exc}")

        # ─────────────────────────────────────────────────────────────────
        # STEP 9: Swap any USDT to USDC on Arbitrum
        # ─────────────────────────────────────────────────────────────────
        try:
            ok_usdt, usdt_raw = await self.balance_adapter.get_vault_wallet_balance(
                USDT_ARB
            )
            if ok_usdt and usdt_raw > 0:
                ok_swap, swap_res = await self.brap_adapter.swap_from_token_ids(
                    from_token_id=USDT_ARB,
                    to_token_id=USDC_ARB,
                    from_address=address,
                    amount=str(int(usdt_raw)),
                    slippage=0.005,
                    strategy_name="boros_hype_strategy",
                )
                if ok_swap:
                    await asyncio.sleep(2)
                else:
                    logger.warning(f"Failed to swap USDT→USDC: {swap_res}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to swap USDT to USDC: {exc}")

        ok_usdc, vault_usdc_raw = await self.balance_adapter.get_vault_wallet_balance(
            USDC_ARB
        )
        usdc_tokens = (
            float(vault_usdc_raw) / 1e6 if ok_usdc and vault_usdc_raw > 0 else 0.0
        )

        # If we initiated a Hyperliquid withdrawal but USDC hasn't arrived on Arbitrum yet,
        # do not claim completion. This is a common in-flight state (HL withdrawals can take minutes).
        if hl_wait_min_usdc_raw is not None:
            vault_usdc_raw_int = int(vault_usdc_raw) if ok_usdc else 0
            if vault_usdc_raw_int + 1 < int(hl_wait_min_usdc_raw):
                elapsed_s = int(time.time() - withdraw_start_ts)
                target_usdc = float(int(hl_wait_min_usdc_raw)) / 1e6
                last_usdc = float(vault_usdc_raw_int) / 1e6
                if max_wait_s > 0:
                    return False, (
                        f"Withdrawal paused after {elapsed_s}s: Hyperliquid USDC withdrawal not yet "
                        f"received on Arbitrum (strategy wallet USDC=${last_usdc:.2f} / ${target_usdc:.2f}). "
                        "Wait a few minutes and re-run withdraw (or increase max_wait_s)."
                    )
                return True, (
                    "Positions unwound, but Hyperliquid USDC withdrawal is still in-flight on Arbitrum "
                    f"(strategy wallet USDC=${last_usdc:.2f} / ${target_usdc:.2f}). "
                    "Wait a few minutes then re-run withdraw; call exit() once USDC is visible."
                )

        try:
            elapsed_s = int(time.time() - withdraw_start_ts)
            inv_final = await self.observe()
            remaining_hype_usd = 0.0
            if inv_final.whype_balance > 0.001:
                remaining_hype_usd += inv_final.whype_value_usd
            if inv_final.khype_balance > 0.001:
                remaining_hype_usd += inv_final.khype_value_usd
            if inv_final.looped_hype_balance > 0.001:
                remaining_hype_usd += inv_final.looped_hype_value_usd

            hedgeable_hype = max(0.0, inv_final.hype_hyperevm_balance - MIN_HYPE_GAS)
            if hedgeable_hype > 0.001:
                remaining_hype_usd += hedgeable_hype * float(inv_final.hype_price_usd)

            if inv_final.hl_spot_hype > 0.01:
                remaining_hype_usd += inv_final.hl_spot_hype_value_usd

            remaining_non_usdc_arb_usd = 0.0
            if inv_final.hl_spot_usdc > 1.0:
                remaining_non_usdc_arb_usd += inv_final.hl_spot_usdc
            if inv_final.hl_perp_margin > 1.0:
                remaining_non_usdc_arb_usd += inv_final.hl_perp_margin
            if inv_final.boros_collateral_usd > 1.0:
                remaining_non_usdc_arb_usd += inv_final.boros_collateral_usd
            if inv_final.boros_pending_withdrawal_usd > 1.0:
                remaining_non_usdc_arb_usd += inv_final.boros_pending_withdrawal_usd
            if inv_final.usdt_arb_idle > 1.0:
                remaining_non_usdc_arb_usd += inv_final.usdt_arb_idle
            if inv_final.hype_oft_arb_value_usd > 1.0:
                remaining_non_usdc_arb_usd += inv_final.hype_oft_arb_value_usd

            if remaining_hype_usd > 1.0:
                return False, (
                    f"Withdrawal incomplete after {elapsed_s}s: "
                    f"~${remaining_hype_usd:.2f} still in HYPE/WHYPE on HyperEVM/HL spot. "
                    "Run withdraw again (or increase max_wait_s)."
                )
            if remaining_non_usdc_arb_usd > 1.0:
                parts: list[str] = []
                if inv_final.hl_spot_usdc > 1.0:
                    parts.append(f"HL spot USDC=${inv_final.hl_spot_usdc:.2f}")
                if inv_final.hl_perp_margin > 1.0:
                    parts.append(f"HL perp margin=${inv_final.hl_perp_margin:.2f}")
                if inv_final.boros_collateral_usd > 1.0:
                    parts.append(
                        f"Boros collateral≈${inv_final.boros_collateral_usd:.2f}"
                    )
                if inv_final.boros_pending_withdrawal_usd > 1.0:
                    parts.append(
                        f"Boros pending≈${inv_final.boros_pending_withdrawal_usd:.2f}"
                    )
                if inv_final.usdt_arb_idle > 1.0:
                    parts.append(f"Arbitrum USDT=${inv_final.usdt_arb_idle:.2f}")
                if inv_final.hype_oft_arb_value_usd > 1.0:
                    parts.append(
                        f"Arbitrum OFT HYPE={inv_final.hype_oft_arb_balance:.6f} "
                        f"(~${inv_final.hype_oft_arb_value_usd:.2f})"
                    )
                detail = (
                    ", ".join(parts) if parts else f"~${remaining_non_usdc_arb_usd:.2f}"
                )
                return False, (
                    f"Withdrawal incomplete after {elapsed_s}s: "
                    f"{detail} still not in Arbitrum USDC. "
                    "Run withdraw again (or increase max_wait_s)."
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Final withdrawal inventory check failed: {exc}")

        if remaining_spot_usdc > 1.0:
            return False, (
                f"Withdrawal incomplete: ${remaining_spot_usdc:.2f} USDC still on Hyperliquid spot. "
                "Run withdraw again (or increase max_wait_s) to retry the spot→perp transfer."
            )

        return (
            True,
            f"Fully unwound all positions. USDC balance: ${usdc_tokens:.2f}. Call exit() to transfer to main wallet.",
        )

    async def _unwrap_whype(
        self, address: str, amount_wei: int
    ) -> tuple[bool, str]:
        try:
            if not self._sign_callback:
                return False, "No signing callback configured"

            tx = await encode_call(
                target=WHYPE_ADDRESS,
                abi=WHYPE_ABI,
                fn_name="withdraw",
                args=[int(amount_wei)],
                from_address=address,
                chain_id=HYPEREVM_CHAIN_ID,
            )

            txn_hash = await send_transaction(
                tx, self._sign_callback, wait_for_receipt=True
            )
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
