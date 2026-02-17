from __future__ import annotations

import logging
from typing import Any

from eth_utils import to_checksum_address

from wayfinder_paths.core.adapters.models import EvmTxn
from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.constants.aave_v3_pool_abi import (
    AAVE_V3_POOL_ABI,
    AAVE_V3_WRAPPED_GATEWAY_ABI,
)
from wayfinder_paths.core.constants.base import MAX_UINT256
from wayfinder_paths.core.constants.erc20_abi import ERC20_ABI
from wayfinder_paths.core.utils.transaction import (
    encode_call,
    gas_limit_transaction,
    gas_price_transaction,
    nonce_transaction,
)
from wayfinder_paths.core.utils.web3 import web3_from_chain_id
from wayfinder_paths.core.constants.contracts import HYPEREVM_WHYPE

logger = logging.getLogger(__name__)

INTEREST_RATE_MODE_STABLE = 1
INTEREST_RATE_MODE_VARIABLE = 2


class AaveAdapter(BaseAdapter):
    adapter_type = "AAVE"

    def __init__(
        self,
        config: dict[str, Any],
    ) -> None:
        super().__init__("aave_adapter", config)
        config = config or {}

        strategy_wallet = config.get("strategy_wallet") or {}
        self.strategy_wallet_address = to_checksum_address(strategy_wallet["address"])

        aave_config = config.get("aave") or {}
        gw = aave_config.get("wrapped_token_gateway")
        self.wrapped_token_gateway: str | None = to_checksum_address(gw) if gw else None
        wnt = aave_config.get("wrapped_native_token")
        self.wrapped_native_token: str | None = (
            to_checksum_address(wnt) if wnt else None
        )

    async def _prepare_txn(self, transaction: dict[str, Any], chain_id: int) -> EvmTxn:
        """Estimate gas, set nonce and gas price, return an unsigned EvmTxn."""
        transaction = await gas_limit_transaction(transaction)
        transaction = await nonce_transaction(transaction)
        transaction = await gas_price_transaction(transaction)
        gas_estimate = transaction.get("gas", 0)
        return EvmTxn(
            txn=transaction,
            gas_estimate=gas_estimate,
            chain_id=chain_id,
        )

    # https://github.com/aave/aave-v3-core/blob/master/contracts/protocol/libraries/types/DataTypes.sol
    async def get_a_token_address(
        self,
        *,
        supply_token_address: str,
        pool_address: str,
        chain_id: int,
    ) -> str:
        pool_addr = to_checksum_address(pool_address)
        async with web3_from_chain_id(chain_id) as w3:
            pool = w3.eth.contract(
                address=pool_addr, abi=AAVE_V3_POOL_ABI
            )
            reserve_data = await pool.functions.getReserveData(
                w3.to_checksum_address(supply_token_address)
            ).call()
            # index 8 = aTokenAddress in the ReserveData struct
            return reserve_data[8]

    async def build_aave_supply(
        self,
        *,
        supply_token_address: str,
        amount: int,
        pool_address: str,
        chain_id: int,
        use_wrapped_gateway: bool = False,
    ) -> EvmTxn:
        logger.info(
            f"Building AAVE supply: pool={pool_address}, "
            f"token={supply_token_address}, amount={amount}, "
            f"use_wrapped_gateway={use_wrapped_gateway}"
        )

        if use_wrapped_gateway:
            return await self._build_hyperlend_gateway_supply(
                amount=amount, pool_address=pool_address, chain_id=chain_id,
            )

        return await self._build_pool_supply(
            supply_token_address=supply_token_address,
            amount=amount,
            pool_address=pool_address,
            chain_id=chain_id,
        )

    async def _build_pool_supply(
        self,
        *,
        supply_token_address: str,
        amount: int,
        pool_address: str,
        chain_id: int,
    ) -> EvmTxn:
        strategy_wallet = self.strategy_wallet_address
        token_addr = to_checksum_address(supply_token_address)
        pool_addr = to_checksum_address(pool_address)

        supply_txn = await encode_call(
            target=pool_addr,
            abi=AAVE_V3_POOL_ABI,
            fn_name="supply",
            args=[token_addr, amount, strategy_wallet, 0],
            from_address=strategy_wallet,
            chain_id=chain_id,
        )

        return await self._prepare_txn(supply_txn, chain_id)

    async def _build_hyperlend_gateway_supply(
        self,
        *,
        amount: int,
        pool_address: str,
        chain_id: int,
    ) -> EvmTxn:
        if not self.wrapped_token_gateway:
            raise ValueError("wrapped_token_gateway not configured for native supply")

        strategy_wallet = self.strategy_wallet_address
        pool_addr = to_checksum_address(pool_address)

        transaction = await encode_call(
            target=self.wrapped_token_gateway,
            abi=AAVE_V3_WRAPPED_GATEWAY_ABI,
            fn_name="depositETH",
            args=[
                HYPEREVM_WHYPE,
                strategy_wallet,
                0,
            ],
            from_address=strategy_wallet,
            chain_id=chain_id,
            value=amount,
        )

        return await self._prepare_txn(transaction, chain_id)

    async def build_aave_withdraw(
        self,
        *,
        supply_token_address: str,
        amount: int,
        pool_address: str,
        chain_id: int,
        use_wrapped_gateway: bool = False,
    ) -> EvmTxn:
        logger.info(
            f"Building AAVE withdraw: pool={pool_address}, "
            f"token={supply_token_address}, amount={amount}, "
            f"use_wrapped_gateway={use_wrapped_gateway}"
        )

        if use_wrapped_gateway:
            return await self._build_hyperlend_gateway_withdraw(
                amount=amount, pool_address=pool_address, chain_id=chain_id,
            )

        return await self._build_pool_withdraw(
            supply_token_address=supply_token_address,
            amount=amount,
            pool_address=pool_address,
            chain_id=chain_id,
        )

    async def _build_pool_withdraw(
        self,
        *,
        supply_token_address: str,
        amount: int,
        pool_address: str,
        chain_id: int,
    ) -> EvmTxn:
        strategy_wallet = self.strategy_wallet_address
        token_addr = to_checksum_address(supply_token_address)
        pool_addr = to_checksum_address(pool_address)

        transaction = await encode_call(
            target=pool_addr,
            abi=AAVE_V3_POOL_ABI,
            fn_name="withdraw",
            args=[token_addr, amount, strategy_wallet],
            from_address=strategy_wallet,
            chain_id=chain_id,
        )

        return await self._prepare_txn(transaction, chain_id)

    async def _build_hyperlend_gateway_withdraw(
        self,
        *,
        amount: int,
        pool_address: str,
        chain_id: int,
    ) -> EvmTxn:
        if not self.wrapped_token_gateway:
            raise ValueError("wrapped_token_gateway not configured for native withdraw")

        strategy_wallet = self.strategy_wallet_address
        pool_addr = to_checksum_address(pool_address)

        transaction = await encode_call(
            target=self.wrapped_token_gateway,
            abi=AAVE_V3_WRAPPED_GATEWAY_ABI,
            fn_name="withdrawETH",
            args=[HYPEREVM_WHYPE, amount, strategy_wallet],
            from_address=strategy_wallet,
            chain_id=chain_id,
        )

        return await self._prepare_txn(transaction, chain_id)

    async def build_aave_borrow(
        self,
        *,
        borrow_token_address: str,
        amount: int,
        pool_address: str,
        chain_id: int,
        interest_rate_mode: int = INTEREST_RATE_MODE_VARIABLE,
        use_wrapped_gateway: bool = False,
    ) -> EvmTxn:
        logger.info(
            f"Building AAVE borrow: pool={pool_address}, "
            f"token={borrow_token_address}, amount={amount}, "
            f"use_wrapped_gateway={use_wrapped_gateway}"
        )

        if use_wrapped_gateway:
            return await self._build_hyperlend_gateway_borrow(
                amount=amount,
                chain_id=chain_id,
                interest_rate_mode=interest_rate_mode,
            )

        return await self._build_pool_borrow(
            borrow_token_address=borrow_token_address,
            amount=amount,
            pool_address=pool_address,
            chain_id=chain_id,
            interest_rate_mode=interest_rate_mode,
        )

    async def _build_pool_borrow(
        self,
        *,
        borrow_token_address: str,
        amount: int,
        pool_address: str,
        chain_id: int,
        interest_rate_mode: int,
    ) -> EvmTxn:
        strategy_wallet = self.strategy_wallet_address
        token_addr = to_checksum_address(borrow_token_address)
        pool_addr = to_checksum_address(pool_address)

        transaction = await encode_call(
            target=pool_addr,
            abi=AAVE_V3_POOL_ABI,
            fn_name="borrow",
            args=[token_addr, amount, interest_rate_mode, 0, strategy_wallet],
            from_address=strategy_wallet,
            chain_id=chain_id,
        )

        return await self._prepare_txn(transaction, chain_id)

    async def _build_hyperlend_gateway_borrow(
        self,
        *,
        amount: int,
        chain_id: int,
        interest_rate_mode: int,
    ) -> EvmTxn:
        if not self.wrapped_token_gateway:
            raise ValueError("wrapped_token_gateway not configured for native borrow")

        strategy_wallet = self.strategy_wallet_address

        transaction = await encode_call(
            target=self.wrapped_token_gateway,
            abi=AAVE_V3_WRAPPED_GATEWAY_ABI,
            fn_name="borrowETH",
            args=[
                HYPEREVM_WHYPE, # asset address (wHYPE)
                amount,
                interest_rate_mode,  # 2 = variable, 1 = stable (if enabled)
                0,  # referral
            ],
            from_address=strategy_wallet,
            chain_id=chain_id,
        )

        return await self._prepare_txn(transaction, chain_id)

    async def build_aave_repay(
        self,
        *,
        borrow_token_address: str,
        amount: int,
        pool_address: str,
        chain_id: int,
        interest_rate_mode: int = INTEREST_RATE_MODE_VARIABLE,
        use_wrapped_gateway: bool = False,
    ) -> EvmTxn:
        logger.info(
            f"Building AAVE repay: pool={pool_address}, "
            f"token={borrow_token_address}, amount={amount}, "
            f"use_wrapped_gateway={use_wrapped_gateway}"
        )

        if use_wrapped_gateway:
            return await self._build_hyperlend_gateway_repay(
                borrow_token_address=borrow_token_address,
                amount=amount,
                pool_address=pool_address,
                chain_id=chain_id,
                interest_rate_mode=interest_rate_mode,
            )

        return await self._build_pool_repay(
            borrow_token_address=borrow_token_address,
            amount=amount,
            pool_address=pool_address,
            chain_id=chain_id,
            interest_rate_mode=interest_rate_mode,
        )

    async def _build_pool_repay(
        self,
        *,
        borrow_token_address: str,
        amount: int,
        pool_address: str,
        chain_id: int,
        interest_rate_mode: int,
    ) -> EvmTxn:
        strategy_wallet = self.strategy_wallet_address
        token_addr = to_checksum_address(borrow_token_address)
        pool_addr = to_checksum_address(pool_address)

        amount_to_repay = await self._resolve_repay_amount(
            borrow_token=token_addr,
            requested_amount=amount,
            pool_address=pool_addr,
            chain_id=chain_id,
            interest_rate_mode=interest_rate_mode,
        )

        transaction = await encode_call(
            target=pool_addr,
            abi=AAVE_V3_POOL_ABI,
            fn_name="repay",
            args=[token_addr, amount_to_repay, interest_rate_mode, strategy_wallet],
            from_address=strategy_wallet,
            chain_id=chain_id,
        )

        return await self._prepare_txn(transaction, chain_id)

    async def _build_hyperlend_gateway_repay(
        self,
        *,
        borrow_token_address: str,
        amount: int,
        pool_address: str,
        chain_id: int,
        interest_rate_mode: int,
    ) -> EvmTxn:
        if not self.wrapped_token_gateway:
            raise ValueError("wrapped_token_gateway not configured for native repay")

        strategy_wallet = self.strategy_wallet_address
        token_addr = to_checksum_address(borrow_token_address)
        pool_addr = to_checksum_address(pool_address)

        amount_to_repay = await self._resolve_repay_amount(
            borrow_token=token_addr,
            requested_amount=amount,
            pool_address=pool_addr,
            chain_id=chain_id,
            interest_rate_mode=interest_rate_mode,
        )

        pay_value = 0 if amount_to_repay == MAX_UINT256 else amount

        transaction = await encode_call(
            target=self.wrapped_token_gateway,
            abi=AAVE_V3_WRAPPED_GATEWAY_ABI,
            fn_name="repayETH",
            args=[HYPEREVM_WHYPE, amount_to_repay, interest_rate_mode, strategy_wallet],
            from_address=strategy_wallet,
            chain_id=chain_id,
            value=pay_value,
        )

        return await self._prepare_txn(transaction, chain_id)

    async def _resolve_repay_amount(
        self,
        *,
        borrow_token: str,
        requested_amount: int,
        pool_address: str,
        chain_id: int,
        interest_rate_mode: int,
    ) -> int:
        """If the requested repay is >= 99.9% of outstanding debt, use MAX_UINT256
        sentinel so the Aave pool only debits the actual balance (avoids dust)."""
        pool_addr = to_checksum_address(pool_address)
        try:
            async with web3_from_chain_id(chain_id) as w3:
                pool = w3.eth.contract(
                    address=pool_addr, abi=AAVE_V3_POOL_ABI
                )
                reserve_data = await pool.functions.getReserveData(
                    w3.to_checksum_address(borrow_token)
                ).call()

                stable_debt_addr = reserve_data[9]
                variable_debt_addr = reserve_data[10]
                debt_token_addr = (
                    stable_debt_addr
                    if interest_rate_mode == INTEREST_RATE_MODE_STABLE
                    else variable_debt_addr
                )

                debt_token = w3.eth.contract(address=debt_token_addr, abi=ERC20_ABI)
                current_debt = await debt_token.functions.balanceOf(
                    self.strategy_wallet_address
                ).call()

                if current_debt > 0 and requested_amount * 1000 >= current_debt * 999:
                    return MAX_UINT256
        except Exception:
            logger.warning(
                "Could not read debt token balance for MAX_UINT256 sentinel check. Using raw amount"
            )

        return requested_amount
