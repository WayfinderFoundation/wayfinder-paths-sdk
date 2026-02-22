"""
Boros HYPE snapshot helpers.

Kept as a mixin so the main strategy file stays readable without changing behavior.
"""

from __future__ import annotations

import time
from typing import Any

import aiohttp
from loguru import logger

from wayfinder_paths.core.utils.web3 import web3_from_chain_id

from .constants import (
    BOROS_HYPE_TOKEN_ID,
    ETH_ARB,
    HYPE_NATIVE,
    HYPE_OFT_ADDRESS,
    HYPEREVM_CHAIN_ID,
    KHYPE_API_URL,
    KHYPE_LST,
    KHYPE_STAKING_ACCOUNTANT,
    KHYPE_STAKING_ACCOUNTANT_ABI,
    LHYPE_ACCOUNTANT,
    LHYPE_ACCOUNTANT_ABI,
    LHYPE_API_URL,
    LOOPED_HYPE,
    MIN_HYPE_GAS,
    USDC_ARB,
    USDT_ARB,
    WHYPE,
    WHYPE_ADDRESS,
)
from .types import Inventory


async def fetch_lhype_apy() -> float | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(LHYPE_API_URL, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("success") and data.get("result"):
                        reward_rate = data["result"].get("reward_rate")
                        if reward_rate is not None:
                            return float(reward_rate) / 100.0
    except Exception as e:
        logger.warning(f"Failed to fetch lHYPE APY: {e}")
    return None


async def fetch_khype_apy() -> float | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(KHYPE_API_URL, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    apy_14d = data.get("apy_14d")
                    if apy_14d is not None:
                        return float(apy_14d)
    except Exception as e:
        logger.warning(f"Failed to fetch kHYPE APY: {e}")
    return None


class BorosHypeSnapshotMixin:
    async def observe(self) -> Inventory:
        self._planner_runtime.reset_virtual_ledger()

        ok_addr, user_address = self._require_strategy_wallet_address()
        if not ok_addr:
            user_address = None

        hype_price_usd = 0.0
        hl_perp_margin = 0.0
        hl_spot_usdc = 0.0
        hl_spot_hype = 0.0
        hl_short_size_hype = 0.0
        hl_unrealized_pnl = 0.0
        hl_withdrawable_usd = 0.0
        mid_prices: dict[str, float] = {}
        perp_position: dict[str, Any] | None = None

        if self.hyperliquid_adapter and user_address:
            try:
                success, prices = await self.hyperliquid_adapter.get_all_mid_prices()
                if success and isinstance(prices, dict):
                    mid_prices = prices
                    hype_raw = prices.get("HYPE")
                    try:
                        hype_price_usd = (
                            float(hype_raw) if hype_raw is not None else 0.0
                        )
                    except (TypeError, ValueError):
                        hype_price_usd = 0.0

                success, user_state = await self.hyperliquid_adapter.get_user_state(
                    user_address
                )
                if success and isinstance(user_state, dict):
                    hl_perp_margin = self.hyperliquid_adapter.get_perp_margin_amount(
                        user_state
                    )
                    hl_withdrawable_usd = float(
                        user_state.get("withdrawable", 0)
                        or user_state.get("marginSummary", {}).get("totalRawUsd", 0)
                    )

                    positions = user_state.get("assetPositions", [])
                    for pos in positions:
                        pos_info = pos.get("position", {})
                        if pos_info.get("coin") == "HYPE":
                            perp_position = pos_info
                            szi = float(pos_info.get("szi", 0))
                            # Negative szi = short position
                            if szi < 0:
                                hl_short_size_hype = abs(szi)
                            hl_unrealized_pnl = float(pos_info.get("unrealizedPnl", 0))
                            break

                (
                    success,
                    spot_state,
                ) = await self.hyperliquid_adapter.get_spot_user_state(user_address)
                if success and isinstance(spot_state, dict):
                    balances = spot_state.get("balances", [])
                    for bal in balances:
                        token = bal.get("coin") or bal.get("token")
                        hold = float(bal.get("hold", 0))
                        total = float(bal.get("total", 0))
                        available = total - hold
                        if token == "USDC":
                            hl_spot_usdc = available
                        elif token == "HYPE":
                            hl_spot_hype = available

            except Exception as e:
                logger.warning(f"Failed to get Hyperliquid state: {e}")

        boros_idle_collateral_cross = 0.0
        boros_idle_collateral_isolated = 0.0
        boros_collateral_hype = 0.0
        boros_collateral_usd = 0.0
        boros_pending_withdrawal_hype = 0.0
        boros_pending_withdrawal_usd = 0.0
        boros_position_size = 0.0
        boros_position_value = 0.0
        boros_position_market_ids: set[int] = set()

        if self.boros_adapter:
            try:
                token_id = (
                    self._planner_runtime.current_boros_token_id or BOROS_HYPE_TOKEN_ID
                )
                success, balances = await self.boros_adapter.get_account_balances(
                    token_id=int(token_id)
                )
                if success and isinstance(balances, dict):
                    # Balances are returned in Boros cash units; for the HYPE-collateralized
                    # market these correspond to HYPE (18 decimals).
                    boros_collateral_hype = float(balances.get("total", 0))
                    boros_idle_collateral_cross = float(balances.get("cross", 0))
                    boros_idle_collateral_isolated = float(balances.get("isolated", 0))

                (
                    ok_pending,
                    pending_hype,
                ) = await self.boros_adapter.get_pending_withdrawal_amount(
                    token_id=int(token_id), token_decimals=18
                )
                if ok_pending:
                    boros_pending_withdrawal_hype = float(pending_hype)

                success, positions = await self.boros_adapter.get_active_positions()
                if success and isinstance(positions, list):
                    for pos in positions:
                        size = float(pos.get("size") or pos.get("notional", 0))
                        boros_position_size += abs(size)
                        boros_position_value += abs(size)
                        mid = pos.get("marketId") or pos.get("market_id")
                        try:
                            mid_int = int(mid) if mid is not None else None
                        except (TypeError, ValueError):
                            mid_int = None
                        if mid_int and mid_int > 0:
                            boros_position_market_ids.add(mid_int)

            except Exception as e:
                logger.warning(f"Failed to get Boros state: {e}")

        hype_hyperevm_balance = 0.0
        whype_balance = 0.0
        khype_balance = 0.0
        looped_hype_balance = 0.0
        usdc_arb_idle = 0.0
        usdt_arb_idle = 0.0
        eth_arb_balance = 0.0
        hype_oft_arb_balance = 0.0

        if self.balance_adapter:
            try:
                assets = [
                    {"token_id": HYPE_NATIVE},  # 0: HyperEVM native HYPE
                    {"token_id": WHYPE},  # 1: Wrapped HYPE
                    {"token_id": KHYPE_LST},  # 2: kHYPE
                    {"token_id": LOOPED_HYPE},  # 3: lHYPE
                    {"token_id": USDC_ARB},  # 4: Arbitrum USDC
                    {"token_id": USDT_ARB},  # 5: Arbitrum USDT
                    {"token_id": ETH_ARB},  # 6: Arbitrum ETH
                    {
                        "token_address": HYPE_OFT_ADDRESS,
                        "chain_id": 42161,
                    },  # 7: Arbitrum OFT HYPE
                ]
                ok, results = await self.balance_adapter.get_wallet_balances_multicall(
                    assets=assets
                )
                if ok and isinstance(results, list):
                    if results[0].get("success"):
                        hype_hyperevm_balance = results[0].get("balance_decimal") or 0.0
                    if results[1].get("success"):
                        whype_balance = results[1].get("balance_decimal") or 0.0
                    if results[2].get("success"):
                        khype_balance = results[2].get("balance_decimal") or 0.0
                    if results[3].get("success"):
                        looped_hype_balance = results[3].get("balance_decimal") or 0.0
                    if results[4].get("success"):
                        usdc_arb_idle = results[4].get("balance_decimal") or 0.0
                    if results[5].get("success"):
                        usdt_arb_idle = results[5].get("balance_decimal") or 0.0
                    if results[6].get("success"):
                        eth_arb_balance = results[6].get("balance_decimal") or 0.0
                    if results[7].get("success"):
                        hype_oft_arb_balance = results[7].get("balance_decimal") or 0.0

            except Exception as e:
                logger.warning(f"Failed to get wallet balances via multicall: {e}")

        # If we recently initiated a HyperEVM -> Arbitrum OFT bridge, the HYPE is
        # temporarily "in flight" (deducted from HyperEVM, not yet minted on Arb).
        # Track it in runtime to prevent hedge thrash + repeated funding.
        in_flight_hype = float(self._planner_runtime.in_flight_boros_oft_hype or 0.0)
        if in_flight_hype > 0:
            balance_before = float(
                self._planner_runtime.in_flight_boros_oft_hype_balance_before or 0.0
            )
            # Clear in-flight once Arb balance has increased by ~the bridged amount.
            if hype_oft_arb_balance >= balance_before + (in_flight_hype * 0.95):
                logger.info(
                    "Detected OFT HYPE arrival on Arbitrum; clearing in-flight bridge tracking"
                )
                self._planner_runtime.in_flight_boros_oft_hype = 0.0
                self._planner_runtime.in_flight_boros_oft_hype_balance_before = 0.0
                self._planner_runtime.in_flight_boros_oft_hype_started_at = None
                in_flight_hype = 0.0

        khype_to_hype_ratio = await self._get_khype_to_hype_ratio()
        looped_hype_to_hype_ratio = await self._get_looped_hype_to_hype_ratio()

        hl_spot_hype_value_usd = hl_spot_hype * hype_price_usd
        hype_hyperevm_value_usd = hype_hyperevm_balance * hype_price_usd
        whype_value_usd = whype_balance * hype_price_usd  # WHYPE is 1:1 with HYPE
        khype_value_usd = khype_balance * khype_to_hype_ratio * hype_price_usd
        looped_hype_value_usd = (
            looped_hype_balance * looped_hype_to_hype_ratio * hype_price_usd
        )
        hype_oft_arb_value_usd = hype_oft_arb_balance * hype_price_usd
        in_flight_hype_value_usd = in_flight_hype * hype_price_usd

        boros_collateral_usd = boros_collateral_hype * hype_price_usd
        boros_pending_withdrawal_usd = boros_pending_withdrawal_hype * hype_price_usd
        boros_committed_collateral_usd = (
            boros_collateral_usd + hype_oft_arb_value_usd + in_flight_hype_value_usd
        )

        # HyperEVM spot value (LSTs + native HYPE/WHYPE). Boros collateral and
        # Arbitrum OFT HYPE are tracked separately.
        spot_value_usd = (
            hype_hyperevm_value_usd
            + whype_value_usd
            + khype_value_usd
            + looped_hype_value_usd
            + hl_spot_hype_value_usd
        )

        # Gas reserve shouldn't be hedged; WHYPE counts as 1:1 HYPE exposure
        hedgeable_hyperevm_hype = max(0.0, hype_hyperevm_balance - MIN_HYPE_GAS)
        total_hype_exposure = (
            hedgeable_hyperevm_hype
            + whype_balance  # WHYPE is 1:1 with HYPE
            + (khype_balance * khype_to_hype_ratio)
            + (looped_hype_balance * looped_hype_to_hype_ratio)
            + hl_spot_hype
            + hype_oft_arb_balance
            + in_flight_hype
            + boros_collateral_hype
            + boros_pending_withdrawal_hype
        )

        hl_short_value_usd = hl_short_size_hype * hype_price_usd

        total_value = (
            spot_value_usd
            + hl_perp_margin
            + hl_spot_usdc
            + boros_collateral_usd
            + boros_pending_withdrawal_usd
            + usdc_arb_idle
            + usdt_arb_idle
            + hype_oft_arb_value_usd
            + in_flight_hype_value_usd
        )

        inv = Inventory(
            hype_hyperevm_balance=hype_hyperevm_balance,
            hype_hyperevm_value_usd=hype_hyperevm_value_usd,
            whype_balance=whype_balance,
            whype_value_usd=whype_value_usd,
            khype_balance=khype_balance,
            khype_value_usd=khype_value_usd,
            looped_hype_balance=looped_hype_balance,
            looped_hype_value_usd=looped_hype_value_usd,
            usdc_arb_idle=usdc_arb_idle,
            usdt_arb_idle=usdt_arb_idle,
            eth_arb_balance=eth_arb_balance,
            hype_oft_arb_balance=hype_oft_arb_balance,
            hype_oft_arb_value_usd=hype_oft_arb_value_usd,
            hl_perp_margin=hl_perp_margin,
            hl_spot_usdc=hl_spot_usdc,
            hl_spot_hype=hl_spot_hype,
            hl_spot_hype_value_usd=hl_spot_hype_value_usd,
            hl_short_size_hype=hl_short_size_hype,
            hl_short_value_usd=hl_short_value_usd,
            hl_unrealized_pnl=hl_unrealized_pnl,
            hl_withdrawable_usd=hl_withdrawable_usd,
            boros_idle_collateral_isolated=boros_idle_collateral_isolated,
            boros_idle_collateral_cross=boros_idle_collateral_cross,
            boros_collateral_hype=boros_collateral_hype,
            boros_collateral_usd=boros_collateral_usd,
            boros_pending_withdrawal_hype=boros_pending_withdrawal_hype,
            boros_pending_withdrawal_usd=boros_pending_withdrawal_usd,
            boros_committed_collateral_usd=boros_committed_collateral_usd,
            boros_position_size=boros_position_size,
            boros_position_value=boros_position_value,
            khype_to_hype_ratio=khype_to_hype_ratio,
            looped_hype_to_hype_ratio=looped_hype_to_hype_ratio,
            hype_price_usd=hype_price_usd,
            spot_value_usd=spot_value_usd,
            total_hype_exposure=total_hype_exposure,
            total_value=total_value,
            boros_position_market_ids=sorted(boros_position_market_ids)
            if boros_position_market_ids
            else None,
        )

        self._opa_alloc = self._get_allocation_status(inv)

        self._opa_risk_progress = self._hyperliquid_liquidation_progress(
            perp_position, mid_prices
        )

        # Only set pending flag for actual Boros withdrawal (not for idle USDT on Arb)
        if inv.boros_pending_withdrawal_usd > 1.0:
            self._opa_pending_withdrawal = True

        # Check for HL liquidation only when hedge is gone but spot exposure exists
        has_no_short = abs(inv.hl_short_size_hype) < 0.01
        has_spot_exposure = inv.total_hype_exposure > 0.1

        if (
            has_no_short
            and has_spot_exposure
            and self.hyperliquid_adapter
            and user_address
        ):
            since_ms = int((time.time() - 43200) * 1000)  # last 12 hours
            try:
                (
                    ok,
                    liq_fills,
                ) = await self.hyperliquid_adapter.check_recent_liquidations(
                    user_address, since_ms
                )
                if ok and liq_fills:
                    inv.hl_liquidation_detected = True
                    inv.hl_liquidation_fills = liq_fills
                    logger.warning(
                        f"[LIQUIDATION] HL position was liquidated! "
                        f"Short={inv.hl_short_size_hype:.4f}, "
                        f"Spot exposure={inv.total_hype_exposure:.4f}"
                    )
                    for fill in liq_fills:
                        liq = fill.get("liquidation", {})
                        logger.warning(
                            f"[LIQUIDATION] coin={fill.get('coin')}, sz={fill.get('sz')}, "
                            f"method={liq.get('method')}, markPx={liq.get('markPx')}"
                        )
            except Exception as e:
                logger.warning(f"Failed to check for liquidations: {e}")

        if self.boros_adapter:
            try:
                success, quotes = await self.boros_adapter.quote_markets_for_underlying(
                    "HYPE"
                )
                if success:
                    self._opa_boros_quotes = quotes
                    logger.debug(f"Fetched {len(quotes)} Boros HYPE quotes")
            except Exception as e:
                logger.warning(f"Failed to get Boros quotes: {e}")
                self._opa_boros_quotes = []

        return inv

    def _hyperliquid_liquidation_progress(
        self,
        perp_pos: dict[str, Any] | None,
        mid_prices: dict[str, float] | None = None,
    ) -> float:
        # Returns fraction [0,1] of distance from entry to liquidation (0 = at entry, 1 = at liq)
        if not perp_pos:
            return 0.0

        liq_px = perp_pos.get("liquidationPx") or perp_pos.get("liqPx")
        entry_px = perp_pos.get("entryPx") or perp_pos.get("entryPrice")
        szi = perp_pos.get("szi") or perp_pos.get("size")
        coin = perp_pos.get("coin", "HYPE")

        mark_px = None
        if mid_prices:
            mark_px = mid_prices.get(coin)
        if mark_px is None:
            mark_px = perp_pos.get("px") or perp_pos.get("markPx")

        if not all([liq_px, entry_px, mark_px, szi]):
            return 0.0

        try:
            liq = float(liq_px)
            entry = float(entry_px)
            mark = float(mark_px)
            size = float(szi)

            if abs(liq - entry) < 0.0001:
                return 0.0

            # For SHORT positions (szi < 0):
            # Progress = (mark - entry) / (liq - entry)
            # When mark rises toward liq, progress → 1
            if size < 0:
                progress = (mark - entry) / (liq - entry)
            else:
                # For LONG positions (szi > 0):
                # Progress = (entry - mark) / (entry - liq)
                # When mark falls toward liq, progress → 1
                progress = (entry - mark) / (entry - liq)

            return max(0.0, min(1.0, progress))
        except (ValueError, ZeroDivisionError):
            return 0.0

    async def _get_khype_to_hype_ratio(self) -> float:
        # Query Kinetiq StakingAccountant kHYPEToHYPE(1e18) for HYPE per 1 kHYPE
        try:
            async with web3_from_chain_id(HYPEREVM_CHAIN_ID) as w3:
                # kHYPE has 18 decimals, so 1 kHYPE = 1e18
                one_khype = 10**18

                contract = w3.eth.contract(
                    address=w3.to_checksum_address(KHYPE_STAKING_ACCOUNTANT),
                    abi=KHYPE_STAKING_ACCOUNTANT_ABI,
                )
                hype_raw = await contract.functions.kHYPEToHYPE(one_khype).call()

                # HYPE also has 18 decimals
                return int(hype_raw) / (10**18)
        except Exception as e:
            logger.warning(f"Failed to get kHYPE exchange rate: {e}")
            return 1.0  # Default to 1:1

    async def _get_looped_hype_to_hype_ratio(self) -> float:
        # Query Looping Accountant getRateInQuote(WHYPE) for HYPE per 1 LHYPE
        try:
            async with web3_from_chain_id(HYPEREVM_CHAIN_ID) as w3:
                contract = w3.eth.contract(
                    address=w3.to_checksum_address(LHYPE_ACCOUNTANT),
                    abi=LHYPE_ACCOUNTANT_ABI,
                )
                rate_raw = await contract.functions.getRateInQuote(
                    w3.to_checksum_address(WHYPE_ADDRESS)
                ).call()

                # Rate is returned with 18 decimals (WHYPE decimals)
                return int(rate_raw) / (10**18)
        except Exception as e:
            logger.warning(f"Failed to get LHYPE exchange rate: {e}")
            return 1.0  # Default to 1:1
