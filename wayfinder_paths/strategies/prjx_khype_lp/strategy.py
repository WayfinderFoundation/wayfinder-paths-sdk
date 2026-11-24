"""PRJX kHYPE/WHYPE Concentrated Liquidity Strategy.

Provides concentrated liquidity on PRJX (Uniswap V4 fork on HyperEVM) for the
kHYPE/WHYPE pair. kHYPE is a liquid staking token that appreciates against HYPE
at ~12-18% APY, earning LP swap fees + kHYPE staking yield.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from wayfinder_paths.adapters.balance_adapter.adapter import BalanceAdapter
from wayfinder_paths.adapters.brap_adapter.adapter import BRAPAdapter
from wayfinder_paths.adapters.ledger_adapter.adapter import LedgerAdapter
from wayfinder_paths.adapters.token_adapter.adapter import TokenAdapter
from wayfinder_paths.core.strategies.Strategy import StatusDict, StatusTuple, Strategy
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id
from wayfinder_paths.policies.erc20 import erc20_spender_for_any_token
from wayfinder_paths.policies.hyper_evm import whype_deposit_and_withdraw
from wayfinder_paths.policies.prjx import PRJX_NPM, PRJX_ROUTER, prjx_npm, prjx_swap

from .constants import (
    COMPOUND_MIN_FEES_USD,
    ERC20_APPROVE_ABI,
    HYPE_NATIVE,
    HYPEREVM_CHAIN_ID,
    KHYPE_STAKING_ACCOUNTANT,
    KHYPE_STAKING_ACCOUNTANT_ABI,
    KHYPE_TOKEN_ID,
    MAX_UINT128,
    MIN_HYPE_GAS,
    MIN_NET_DEPOSIT,
    NPM_ABI,
    POOL_ABI,
    POOL_FEE,
    POOL_TICK_SPACING,
    RANGE_WIDTH_TICKS,
    REBALANCE_TICK_DRIFT,
    TOKEN0_ADDRESS,
    TOKEN1_ADDRESS,
    WHYPE_ABI,
)
from .tick_math import (
    amounts_for_liquidity,
    compute_optimal_amounts,
    round_tick_down,
    round_tick_up,
)


class PrjxKhypeLpStrategy(Strategy):
    name = "PRJX kHYPE LP"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        main_wallet: dict[str, Any] | None = None,
        strategy_wallet: dict[str, Any] | None = None,
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
        self.brap_adapter: BRAPAdapter | None = None
        self.token_adapter: TokenAdapter | None = None
        self.ledger_adapter: LedgerAdapter | None = None

        self._position_token_id: int | None = None
        self._pool_address: str | None = None

        strategy_wallet_cfg = self.config.get("strategy_wallet")
        if not strategy_wallet_cfg or not strategy_wallet_cfg.get("address"):
            raise ValueError(
                "strategy_wallet not configured. Provide strategy_wallet address in config."
            )

        adapter_config = {
            "main_wallet": self.config.get("main_wallet"),
            "strategy_wallet": strategy_wallet_cfg,
            "strategy": self.config,
        }

        self.balance_adapter = BalanceAdapter(
            adapter_config,
            main_wallet_signing_callback=self.main_wallet_signing_callback,
            strategy_wallet_signing_callback=self.strategy_wallet_signing_callback,
        )
        self.token_adapter = TokenAdapter()
        self.ledger_adapter = LedgerAdapter()
        self.brap_adapter = BRAPAdapter(
            adapter_config,
            strategy_wallet_signing_callback=self.strategy_wallet_signing_callback,
        )

    async def setup(self) -> None:
        await self._discover_position()

    # ── deposit ──────────────────────────────────────────────────────────────

    async def deposit(
        self, main_token_amount: float = 0.0, gas_token_amount: float = 0.0, **kwargs
    ) -> StatusTuple:
        if main_token_amount < MIN_NET_DEPOSIT:
            return (
                False,
                f"Minimum deposit is {MIN_NET_DEPOSIT} HYPE, got {main_token_amount}",
            )

        strategy_addr = self._get_strategy_wallet_address()

        # Transfer gas HYPE from main → strategy wallet
        if gas_token_amount > 0:
            (
                ok,
                msg,
            ) = await self.balance_adapter.move_from_main_wallet_to_strategy_wallet(
                HYPE_NATIVE, gas_token_amount, strategy_name=self.name
            )
            if not ok:
                return False, f"Gas transfer failed: {msg}"

        # Transfer deposit HYPE from main → strategy wallet
        ok, msg = await self.balance_adapter.move_from_main_wallet_to_strategy_wallet(
            HYPE_NATIVE, main_token_amount, strategy_name=self.name
        )
        if not ok:
            return False, f"Deposit transfer failed: {msg}"

        # Read native HYPE balance on strategy wallet
        ok, hype_balance_raw = await self.balance_adapter.get_balance(
            token_id=HYPE_NATIVE,
            wallet_address=strategy_addr,
        )
        if not ok:
            return False, f"Failed to read HYPE balance: {hype_balance_raw}"

        hype_balance = float(hype_balance_raw) / 1e18
        wrap_amount = hype_balance - MIN_HYPE_GAS
        if wrap_amount <= 0:
            return False, "Insufficient HYPE after gas reserve"

        wrap_wei = int(wrap_amount * 1e18)

        # Wrap HYPE → WHYPE
        tx = await encode_call(
            target=TOKEN0_ADDRESS,
            abi=WHYPE_ABI,
            fn_name="deposit",
            args=[],
            from_address=strategy_addr,
            chain_id=HYPEREVM_CHAIN_ID,
            value=wrap_wei,
        )
        tx_hash = await send_transaction(
            tx, self.strategy_wallet_signing_callback, wait_for_receipt=True
        )
        logger.info(f"Wrapped {wrap_amount:.4f} HYPE → WHYPE (tx={tx_hash})")

        slot0 = await self._read_slot0()
        if slot0 is None:
            return False, "Failed to read pool slot0 — pool may not exist"

        sqrt_price_x96 = slot0[0]
        current_tick = slot0[1]

        # Compute tick range centered on current tick
        half_range = RANGE_WIDTH_TICKS // 2
        tick_lower = round_tick_down(current_tick - half_range, POOL_TICK_SPACING)
        tick_upper = round_tick_up(current_tick + half_range, POOL_TICK_SPACING)

        # Compute how much to allocate as token0 (WHYPE) vs token1 (kHYPE)
        # Try depositing all as WHYPE first, see what ratio is needed
        whype_wei = wrap_wei
        optimal_a0, optimal_a1 = compute_optimal_amounts(
            sqrt_price_x96, tick_lower, tick_upper, whype_wei, 0
        )
        # optimal_a1 tells us how much kHYPE is needed — but we have 0, so we need
        # to swap some WHYPE→kHYPE. The ratio tells us how much.
        # With both tokens: compute the fraction of WHYPE that should become kHYPE
        # by looking at what the position needs at the current price.
        # Use a simulated balanced deposit to figure out the ratio.
        # Compute what fraction of WHYPE should become kHYPE based on pool position
        khype_fraction = self._compute_khype_fraction(
            sqrt_price_x96, tick_lower, tick_upper
        )
        swap_hype_amount = wrap_amount * khype_fraction

        if swap_hype_amount < 0.02:
            return False, "kHYPE swap amount too small"

        # Unwrap the kHYPE portion back to native HYPE for swapping via BRAP
        unwrap_wei = int(swap_hype_amount * 1e18)
        tx = await encode_call(
            target=TOKEN0_ADDRESS,
            abi=WHYPE_ABI,
            fn_name="withdraw",
            args=[unwrap_wei],
            from_address=strategy_addr,
            chain_id=HYPEREVM_CHAIN_ID,
        )
        tx_hash = await send_transaction(
            tx, self.strategy_wallet_signing_callback, wait_for_receipt=True
        )
        logger.info(f"Unwrapped {swap_hype_amount:.4f} WHYPE for kHYPE swap")

        # Swap native HYPE → kHYPE via BRAP
        ok, res = await self.brap_adapter.swap_from_token_ids(
            from_token_id=HYPE_NATIVE,
            to_token_id=KHYPE_TOKEN_ID,
            from_address=strategy_addr,
            amount=str(unwrap_wei),
            slippage=0.01,
            strategy_name=self.name,
        )
        if not ok:
            return False, f"Swap HYPE→kHYPE failed: {res}"
        logger.info(f"Swapped {swap_hype_amount:.4f} HYPE → kHYPE")

        await asyncio.sleep(2)

        # Read actual token balances
        whype_balance = await self._read_erc20_balance(TOKEN0_ADDRESS, strategy_addr)
        khype_balance = await self._read_erc20_balance(TOKEN1_ADDRESS, strategy_addr)

        if whype_balance == 0 and khype_balance == 0:
            return False, "No tokens available to mint position"

        # Approve PRJX NPM for both tokens
        await self._approve_token(
            TOKEN0_ADDRESS, PRJX_NPM, whype_balance, strategy_addr
        )
        await self._approve_token(
            TOKEN1_ADDRESS, PRJX_NPM, khype_balance, strategy_addr
        )

        # Re-read slot0 (may have shifted slightly)
        slot0 = await self._read_slot0()
        if slot0 is None:
            return False, "Failed to re-read pool slot0"
        sqrt_price_x96 = slot0[0]
        current_tick = slot0[1]
        tick_lower = round_tick_down(current_tick - half_range, POOL_TICK_SPACING)
        tick_upper = round_tick_up(current_tick + half_range, POOL_TICK_SPACING)

        # Compute optimal amounts with actual balances
        amount0, amount1 = compute_optimal_amounts(
            sqrt_price_x96, tick_lower, tick_upper, whype_balance, khype_balance
        )
        if amount0 == 0 and amount1 == 0:
            return False, "Could not compute valid deposit amounts"

        # Mint LP position
        deadline = int(time.time()) + 300
        mint_params = (
            TOKEN0_ADDRESS,
            TOKEN1_ADDRESS,
            POOL_FEE,
            tick_lower,
            tick_upper,
            amount0,
            amount1,
            0,  # amount0Min
            0,  # amount1Min
            strategy_addr,
            deadline,
        )

        tx = await encode_call(
            target=PRJX_NPM,
            abi=NPM_ABI,
            fn_name="mint",
            args=[mint_params],
            from_address=strategy_addr,
            chain_id=HYPEREVM_CHAIN_ID,
        )
        tx_hash = await send_transaction(
            tx, self.strategy_wallet_signing_callback, wait_for_receipt=True
        )
        logger.info(f"Minted LP position (tx={tx_hash})")

        # Discover the newly minted token ID
        await self._discover_position()

        return True, f"Deposited {main_token_amount:.4f} HYPE into kHYPE/WHYPE LP"

    # ── update ───────────────────────────────────────────────────────────────

    async def update(self) -> StatusTuple:
        if self._position_token_id is None:
            await self._discover_position()
        if self._position_token_id is None:
            return True, "No active position to update"

        strategy_addr = self._get_strategy_wallet_address()
        slot0 = await self._read_slot0()
        if slot0 is None:
            return False, "Failed to read pool slot0"

        current_tick = slot0[1]
        position = await self._read_position(self._position_token_id)
        if position is None:
            return False, "Failed to read position data"

        tick_lower = position[5]
        tick_upper = position[6]
        liquidity = position[7]

        if liquidity == 0:
            return True, "Position has zero liquidity, nothing to update"

        # Check if current tick is out of range or near the edge
        in_range = tick_lower <= current_tick < tick_upper
        near_lower = (current_tick - tick_lower) < REBALANCE_TICK_DRIFT
        near_upper = (tick_upper - current_tick) < REBALANCE_TICK_DRIFT

        if not in_range or near_lower or near_upper:
            return await self._rebalance(strategy_addr, current_tick)

        return await self._collect_and_compound(strategy_addr)

    async def _rebalance(self, strategy_addr: str, current_tick: int) -> StatusTuple:
        """Remove liquidity, collect, burn, then re-mint at new centered range."""
        token_id = self._position_token_id
        position = await self._read_position(token_id)
        liquidity = position[7]
        deadline = int(time.time()) + 300

        # 1. Decrease all liquidity
        decrease_params = (token_id, liquidity, 0, 0, deadline)
        tx = await encode_call(
            target=PRJX_NPM,
            abi=NPM_ABI,
            fn_name="decreaseLiquidity",
            args=[decrease_params],
            from_address=strategy_addr,
            chain_id=HYPEREVM_CHAIN_ID,
        )
        await send_transaction(
            tx, self.strategy_wallet_signing_callback, wait_for_receipt=True
        )

        # 2. Collect all tokens
        collect_params = (token_id, strategy_addr, MAX_UINT128, MAX_UINT128)
        tx = await encode_call(
            target=PRJX_NPM,
            abi=NPM_ABI,
            fn_name="collect",
            args=[collect_params],
            from_address=strategy_addr,
            chain_id=HYPEREVM_CHAIN_ID,
        )
        await send_transaction(
            tx, self.strategy_wallet_signing_callback, wait_for_receipt=True
        )

        # 3. Burn the empty NFT
        tx = await encode_call(
            target=PRJX_NPM,
            abi=NPM_ABI,
            fn_name="burn",
            args=[token_id],
            from_address=strategy_addr,
            chain_id=HYPEREVM_CHAIN_ID,
        )
        await send_transaction(
            tx, self.strategy_wallet_signing_callback, wait_for_receipt=True
        )
        self._position_token_id = None

        await asyncio.sleep(2)

        # 4. Read balances and re-mint
        whype_balance = await self._read_erc20_balance(TOKEN0_ADDRESS, strategy_addr)
        khype_balance = await self._read_erc20_balance(TOKEN1_ADDRESS, strategy_addr)

        if whype_balance == 0 and khype_balance == 0:
            return True, "Rebalance: no tokens remaining after collection"

        # Re-read slot0 for current tick
        slot0 = await self._read_slot0()
        if slot0 is None:
            return False, "Failed to read pool slot0 during rebalance"
        sqrt_price_x96 = slot0[0]
        current_tick = slot0[1]

        half_range = RANGE_WIDTH_TICKS // 2
        tick_lower = round_tick_down(current_tick - half_range, POOL_TICK_SPACING)
        tick_upper = round_tick_up(current_tick + half_range, POOL_TICK_SPACING)

        # Approve + mint
        await self._approve_token(
            TOKEN0_ADDRESS, PRJX_NPM, whype_balance, strategy_addr
        )
        await self._approve_token(
            TOKEN1_ADDRESS, PRJX_NPM, khype_balance, strategy_addr
        )

        amount0, amount1 = compute_optimal_amounts(
            sqrt_price_x96, tick_lower, tick_upper, whype_balance, khype_balance
        )
        if amount0 == 0 and amount1 == 0:
            return False, "Rebalance: could not compute valid deposit amounts"

        deadline = int(time.time()) + 300
        mint_params = (
            TOKEN0_ADDRESS,
            TOKEN1_ADDRESS,
            POOL_FEE,
            tick_lower,
            tick_upper,
            amount0,
            amount1,
            0,
            0,
            strategy_addr,
            deadline,
        )
        tx = await encode_call(
            target=PRJX_NPM,
            abi=NPM_ABI,
            fn_name="mint",
            args=[mint_params],
            from_address=strategy_addr,
            chain_id=HYPEREVM_CHAIN_ID,
        )
        await send_transaction(
            tx, self.strategy_wallet_signing_callback, wait_for_receipt=True
        )
        await self._discover_position()

        return (
            True,
            f"Rebalanced LP at tick {current_tick} [{tick_lower}, {tick_upper}]",
        )

    async def _collect_and_compound(self, strategy_addr: str) -> StatusTuple:
        """Collect accrued fees. If above threshold, add them back as liquidity."""
        token_id = self._position_token_id
        deadline = int(time.time()) + 300

        collect_params = (token_id, strategy_addr, MAX_UINT128, MAX_UINT128)
        tx = await encode_call(
            target=PRJX_NPM,
            abi=NPM_ABI,
            fn_name="collect",
            args=[collect_params],
            from_address=strategy_addr,
            chain_id=HYPEREVM_CHAIN_ID,
        )
        await send_transaction(
            tx, self.strategy_wallet_signing_callback, wait_for_receipt=True
        )

        # Read collected balances and increase liquidity if worthwhile
        whype_balance = await self._read_erc20_balance(TOKEN0_ADDRESS, strategy_addr)
        khype_balance = await self._read_erc20_balance(TOKEN1_ADDRESS, strategy_addr)

        # Rough USD check (both tokens ≈ HYPE price)
        hype_price = await self._get_hype_price()
        total_fees_usd = ((whype_balance + khype_balance) / 1e18) * hype_price

        if total_fees_usd < COMPOUND_MIN_FEES_USD:
            return (
                True,
                f"Collected fees (${total_fees_usd:.2f}) below compound threshold",
            )

        # Increase liquidity with collected fees
        await self._approve_token(
            TOKEN0_ADDRESS, PRJX_NPM, whype_balance, strategy_addr
        )
        await self._approve_token(
            TOKEN1_ADDRESS, PRJX_NPM, khype_balance, strategy_addr
        )

        increase_params = (
            token_id,
            whype_balance,
            khype_balance,
            0,
            0,
            deadline,
        )
        tx = await encode_call(
            target=PRJX_NPM,
            abi=NPM_ABI,
            fn_name="increaseLiquidity",
            args=[increase_params],
            from_address=strategy_addr,
            chain_id=HYPEREVM_CHAIN_ID,
        )
        await send_transaction(
            tx, self.strategy_wallet_signing_callback, wait_for_receipt=True
        )

        return True, f"Compounded ${total_fees_usd:.2f} of fees into position"

    # ── withdraw ─────────────────────────────────────────────────────────────

    async def withdraw(self, **kwargs) -> StatusTuple:
        if self._position_token_id is None:
            await self._discover_position()
        if self._position_token_id is None:
            return True, "No active position to withdraw"

        strategy_addr = self._get_strategy_wallet_address()
        token_id = self._position_token_id
        position = await self._read_position(token_id)
        liquidity = position[7]
        deadline = int(time.time()) + 300

        # 1. Decrease all liquidity
        if liquidity > 0:
            decrease_params = (token_id, liquidity, 0, 0, deadline)
            tx = await encode_call(
                target=PRJX_NPM,
                abi=NPM_ABI,
                fn_name="decreaseLiquidity",
                args=[decrease_params],
                from_address=strategy_addr,
                chain_id=HYPEREVM_CHAIN_ID,
            )
            await send_transaction(
                tx, self.strategy_wallet_signing_callback, wait_for_receipt=True
            )

        # 2. Collect all tokens + fees
        collect_params = (token_id, strategy_addr, MAX_UINT128, MAX_UINT128)
        tx = await encode_call(
            target=PRJX_NPM,
            abi=NPM_ABI,
            fn_name="collect",
            args=[collect_params],
            from_address=strategy_addr,
            chain_id=HYPEREVM_CHAIN_ID,
        )
        await send_transaction(
            tx, self.strategy_wallet_signing_callback, wait_for_receipt=True
        )

        # 3. Burn the NFT
        tx = await encode_call(
            target=PRJX_NPM,
            abi=NPM_ABI,
            fn_name="burn",
            args=[token_id],
            from_address=strategy_addr,
            chain_id=HYPEREVM_CHAIN_ID,
        )
        await send_transaction(
            tx, self.strategy_wallet_signing_callback, wait_for_receipt=True
        )
        self._position_token_id = None

        await asyncio.sleep(2)

        # 4. Swap kHYPE → native HYPE via BRAP
        khype_balance = await self._read_erc20_balance(TOKEN1_ADDRESS, strategy_addr)
        if khype_balance > int(0.001 * 1e18):
            ok, res = await self.brap_adapter.swap_from_token_ids(
                from_token_id=KHYPE_TOKEN_ID,
                to_token_id=HYPE_NATIVE,
                from_address=strategy_addr,
                amount=str(khype_balance),
                slippage=0.01,
                strategy_name=self.name,
            )
            if not ok:
                logger.warning(f"kHYPE→HYPE swap failed: {res}")

        # 5. Unwrap WHYPE → native HYPE
        whype_balance = await self._read_erc20_balance(TOKEN0_ADDRESS, strategy_addr)
        if whype_balance > 0:
            tx = await encode_call(
                target=TOKEN0_ADDRESS,
                abi=WHYPE_ABI,
                fn_name="withdraw",
                args=[whype_balance],
                from_address=strategy_addr,
                chain_id=HYPEREVM_CHAIN_ID,
            )
            await send_transaction(
                tx, self.strategy_wallet_signing_callback, wait_for_receipt=True
            )

        return True, "Withdrew all liquidity and converted to native HYPE"

    # ── exit ─────────────────────────────────────────────────────────────────

    async def exit(self, **kwargs) -> StatusTuple:
        strategy_addr = self._get_strategy_wallet_address()
        main_addr = self._get_main_wallet_address()

        if strategy_addr.lower() == main_addr.lower():
            return True, "Main wallet is strategy wallet, no transfer needed"

        # Transfer native HYPE from strategy → main
        ok, hype_raw = await self.balance_adapter.get_balance(
            token_id=HYPE_NATIVE,
            wallet_address=strategy_addr,
        )
        if not ok:
            return False, f"Failed to read HYPE balance: {hype_raw}"

        hype_balance = float(hype_raw) / 1e18
        tx_fee_reserve = 0.01
        transferable = hype_balance - tx_fee_reserve
        if transferable <= 0:
            return True, "No HYPE to transfer (balance covers only gas)"

        ok, msg = await self.balance_adapter.move_from_strategy_wallet_to_main_wallet(
            token_id=HYPE_NATIVE,
            amount=transferable,
            strategy_name=self.name,
            skip_ledger=False,
        )
        if not ok:
            return False, f"HYPE transfer to main wallet failed: {msg}"

        return True, f"Transferred {transferable:.4f} HYPE to main wallet"

    # ── status ───────────────────────────────────────────────────────────────

    async def _status(self) -> StatusDict:
        strategy_addr = self._get_strategy_wallet_address()

        if self._position_token_id is None:
            await self._discover_position()

        _, net_deposit = await self.ledger_adapter.get_strategy_net_deposit(
            wallet_address=strategy_addr
        )

        # Read native HYPE balance for gas
        ok, hype_raw = await self.balance_adapter.get_balance(
            token_id=HYPE_NATIVE,
            wallet_address=strategy_addr,
        )
        gas_hype = float(hype_raw) / 1e18 if ok else 0.0

        hype_price = await self._get_hype_price()
        khype_ratio = await self._get_khype_to_hype_ratio()

        position_info: dict[str, Any] = {}
        position_value_usd = 0.0

        if self._position_token_id is not None:
            position = await self._read_position(self._position_token_id)
            if position is not None:
                tick_lower = position[5]
                tick_upper = position[6]
                liquidity = position[7]
                tokens_owed0 = position[10]
                tokens_owed1 = position[11]

                slot0 = await self._read_slot0()
                in_range = False
                current_tick = 0
                sqrt_price_x96 = 0
                if slot0:
                    sqrt_price_x96 = slot0[0]
                    current_tick = slot0[1]
                    in_range = tick_lower <= current_tick < tick_upper

                amount0, amount1 = (0, 0)
                if liquidity > 0 and sqrt_price_x96 > 0:
                    amount0, amount1 = amounts_for_liquidity(
                        sqrt_price_x96, tick_lower, tick_upper, liquidity
                    )

                # WHYPE (amount0) = 1:1 with HYPE, kHYPE (amount1) * ratio = HYPE equivalent
                whype_hype = amount0 / 1e18
                khype_hype = (amount1 / 1e18) * khype_ratio
                fees_hype = (tokens_owed0 / 1e18) + (tokens_owed1 / 1e18) * khype_ratio
                total_hype = whype_hype + khype_hype + fees_hype
                position_value_usd = total_hype * hype_price

                position_info = {
                    "token_id": self._position_token_id,
                    "tick_lower": tick_lower,
                    "tick_upper": tick_upper,
                    "current_tick": current_tick,
                    "in_range": in_range,
                    "liquidity": str(liquidity),
                    "whype_amount": whype_hype,
                    "khype_amount": amount1 / 1e18,
                    "khype_ratio": khype_ratio,
                    "fees_owed_whype": tokens_owed0 / 1e18,
                    "fees_owed_khype": tokens_owed1 / 1e18,
                }

        gas_value_usd = gas_hype * hype_price
        portfolio_value = position_value_usd + gas_value_usd

        return StatusDict(
            portfolio_value=portfolio_value,
            net_deposit=float(net_deposit) if net_deposit else 0.0,
            strategy_status={
                "position": position_info,
                "hype_price_usd": hype_price,
                "gas_hype": gas_hype,
            },
            gas_available=gas_hype,
            gassed_up=gas_hype >= MIN_HYPE_GAS,
        )

    # ── policies ─────────────────────────────────────────────────────────────

    @staticmethod
    async def policies() -> list[str]:
        return [
            await whype_deposit_and_withdraw(),
            erc20_spender_for_any_token(PRJX_NPM),
            await prjx_npm(),
            erc20_spender_for_any_token(PRJX_ROUTER),
            await prjx_swap(),
        ]

    # ── helpers ──────────────────────────────────────────────────────────────

    async def _discover_position(self) -> None:
        """Recover position token ID from the NPM contract after restart."""
        strategy_addr = self._get_strategy_wallet_address()
        try:
            async with web3_from_chain_id(HYPEREVM_CHAIN_ID) as w3:
                npm = w3.eth.contract(
                    address=w3.to_checksum_address(PRJX_NPM), abi=NPM_ABI
                )
                count = await npm.functions.balanceOf(
                    w3.to_checksum_address(strategy_addr)
                ).call()
                if count > 0:
                    token_id = await npm.functions.tokenOfOwnerByIndex(
                        w3.to_checksum_address(strategy_addr), 0
                    ).call()
                    self._position_token_id = int(token_id)
                    logger.info(
                        f"Discovered existing LP position: tokenId={self._position_token_id}"
                    )
                else:
                    self._position_token_id = None
        except Exception as e:
            logger.warning(f"Failed to discover LP position: {e}")
            self._position_token_id = None

    async def _read_slot0(self) -> tuple | None:
        """Read pool slot0 (sqrtPriceX96, tick, ...)."""
        pool_addr = await self._get_pool_address()
        if not pool_addr:
            return None
        try:
            async with web3_from_chain_id(HYPEREVM_CHAIN_ID) as w3:
                pool = w3.eth.contract(
                    address=w3.to_checksum_address(pool_addr), abi=POOL_ABI
                )
                return await pool.functions.slot0().call()
        except Exception as e:
            logger.warning(f"Failed to read slot0: {e}")
            return None

    async def _get_pool_address(self) -> str | None:
        """Get the pool address from a known position, or from config."""
        if self._pool_address:
            return self._pool_address
        # Try to discover from an existing position
        if self._position_token_id is not None:
            position = await self._read_position(self._position_token_id)
            if position:
                # token0 + token1 + fee uniquely identify the pool, but we need
                # the pool address for slot0 reads. Read it from the factory or
                # use the strategy config if available.
                pass

        # Use config override if available
        pool_addr = self.config.get("pool_address")
        if pool_addr:
            self._pool_address = pool_addr
            return pool_addr

        return None

    async def _read_position(self, token_id: int) -> tuple | None:
        """Read position data from the NPM contract."""
        try:
            async with web3_from_chain_id(HYPEREVM_CHAIN_ID) as w3:
                npm = w3.eth.contract(
                    address=w3.to_checksum_address(PRJX_NPM), abi=NPM_ABI
                )
                return await npm.functions.positions(token_id).call()
        except Exception as e:
            logger.warning(f"Failed to read position {token_id}: {e}")
            return None

    async def _read_erc20_balance(self, token_address: str, owner: str) -> int:
        """Read an ERC20 token balance (returns raw wei)."""
        try:
            async with web3_from_chain_id(HYPEREVM_CHAIN_ID) as w3:
                contract = w3.eth.contract(
                    address=w3.to_checksum_address(token_address),
                    abi=ERC20_APPROVE_ABI,
                )
                return await contract.functions.balanceOf(
                    w3.to_checksum_address(owner)
                ).call()
        except Exception as e:
            logger.warning(f"Failed to read balance for {token_address}: {e}")
            return 0

    async def _approve_token(
        self, token_address: str, spender: str, amount: int, from_address: str
    ) -> None:
        """Approve a spender for an ERC20 token."""
        if amount == 0:
            return
        tx = await encode_call(
            target=token_address,
            abi=ERC20_APPROVE_ABI,
            fn_name="approve",
            args=[spender, amount],
            from_address=from_address,
            chain_id=HYPEREVM_CHAIN_ID,
        )
        await send_transaction(
            tx, self.strategy_wallet_signing_callback, wait_for_receipt=True
        )

    async def _get_khype_to_hype_ratio(self) -> float:
        """Query Kinetiq StakingAccountant for HYPE per 1 kHYPE."""
        try:
            async with web3_from_chain_id(HYPEREVM_CHAIN_ID) as w3:
                contract = w3.eth.contract(
                    address=w3.to_checksum_address(KHYPE_STAKING_ACCOUNTANT),
                    abi=KHYPE_STAKING_ACCOUNTANT_ABI,
                )
                hype_raw = await contract.functions.kHYPEToHYPE(10**18).call()
                return int(hype_raw) / (10**18)
        except Exception as e:
            logger.warning(f"Failed to get kHYPE exchange rate: {e}")
            return 1.0

    async def _get_hype_price(self) -> float:
        """Get HYPE price in USD via token adapter."""
        try:
            ok, token_info = await self.token_adapter.get_token(HYPE_NATIVE)
            if ok and token_info:
                return float(token_info.get("price_usd", 0.0))
        except Exception:
            pass
        return 0.0

    def _compute_khype_fraction(
        self, sqrt_price_x96: int, tick_lower: int, tick_upper: int
    ) -> float:
        """Compute what fraction of total WHYPE should be swapped to kHYPE.

        Uses the position math: given 1 unit of WHYPE as token0 and 0 token1,
        figure out the optimal split.
        """
        # Deposit a hypothetical 1e18 of token0 with equal token1
        test_amount = 10**18
        a0, a1 = compute_optimal_amounts(
            sqrt_price_x96, tick_lower, tick_upper, test_amount, test_amount
        )
        total = a0 + a1
        if total == 0:
            return 0.5
        return a1 / total
