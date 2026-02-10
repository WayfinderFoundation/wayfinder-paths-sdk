from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

from eth_utils import to_checksum_address

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.constants.base import MAX_UINT256
from wayfinder_paths.core.constants.contracts import (
    UNISWAP_V3_FACTORY,
    UNISWAP_V3_NPM,
    ZERO_ADDRESS,
)
from wayfinder_paths.core.constants.uniswap_v3_abi import (
    NONFUNGIBLE_POSITION_MANAGER_ABI,
    UNISWAP_V3_FACTORY_ABI,
)
from wayfinder_paths.core.utils.tokens import ensure_allowance
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

MAX_UINT128 = 2**128 - 1

TICK_SPACING: dict[int, int] = {
    100: 1,
    500: 10,
    3000: 60,
    10000: 200,
}

SUPPORTED_CHAIN_IDS = frozenset(UNISWAP_V3_NPM.keys())


class UniswapAdapter(BaseAdapter):
    adapter_type = "UNISWAP"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        strategy_wallet_signing_callback: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__("uniswap_adapter", config)
        cfg = config or {}
        strategy_wallet = cfg.get("strategy_wallet") or {}
        self.strategy_wallet_address = to_checksum_address(strategy_wallet["address"])
        self.strategy_wallet_signing_callback = strategy_wallet_signing_callback

    @staticmethod
    def _get_npm_address(chain_id: int) -> str:
        addr = UNISWAP_V3_NPM.get(chain_id)
        if addr is None:
            raise ValueError(f"Uniswap V3 NPM not deployed on chain {chain_id}")
        return addr

    @staticmethod
    def _get_factory_address(chain_id: int) -> str:
        addr = UNISWAP_V3_FACTORY.get(chain_id)
        if addr is None:
            raise ValueError(f"Uniswap V3 Factory not deployed on chain {chain_id}")
        return addr

    @staticmethod
    def _order_tokens(
        token_a: str, token_b: str, amount_a: int, amount_b: int
    ) -> tuple[str, str, int, int]:
        a = to_checksum_address(token_a)
        b = to_checksum_address(token_b)
        if int(a, 16) < int(b, 16):
            return a, b, amount_a, amount_b
        return b, a, amount_b, amount_a

    @staticmethod
    def _deadline(seconds: int) -> int:
        return int(time.time()) + seconds

    async def add_liquidity(
        self,
        *,
        token0: str,
        token1: str,
        fee: int,
        tick_lower: int,
        tick_upper: int,
        amount0_desired: int,
        amount1_desired: int,
        amount0_min: int = 0,
        amount1_min: int = 0,
        chain_id: int,
        deadline_seconds: int = 300,
    ) -> tuple[bool, Any]:
        npm = self._get_npm_address(chain_id)
        t0, t1, a0, a1 = self._order_tokens(
            token0, token1, amount0_desired, amount1_desired
        )
        if int(to_checksum_address(token0), 16) > int(to_checksum_address(token1), 16):
            amount0_min, amount1_min = amount1_min, amount0_min

        spacing = TICK_SPACING.get(fee)
        if spacing:
            tick_lower = self.nearest_usable_tick(tick_lower, spacing)
            tick_upper = self.nearest_usable_tick(tick_upper, spacing)

        wallet = self.strategy_wallet_address

        for token, amount in [(t0, a0), (t1, a1)]:
            if amount > 0:
                approved = await ensure_allowance(
                    token_address=token,
                    owner=wallet,
                    spender=npm,
                    amount=amount,
                    chain_id=chain_id,
                    signing_callback=self.strategy_wallet_signing_callback,
                    approval_amount=MAX_UINT256,
                )
                if not approved[0]:
                    return approved

        mint_params = (
            t0,
            t1,
            fee,
            tick_lower,
            tick_upper,
            a0,
            a1,
            amount0_min,
            amount1_min,
            wallet,
            self._deadline(deadline_seconds),
        )

        transaction = await encode_call(
            target=npm,
            abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
            fn_name="mint",
            args=[mint_params],
            from_address=wallet,
            chain_id=chain_id,
        )

        txn_hash = await send_transaction(
            transaction, self.strategy_wallet_signing_callback
        )
        return True, txn_hash

    async def increase_liquidity(
        self,
        *,
        token_id: int,
        amount0_desired: int,
        amount1_desired: int,
        amount0_min: int = 0,
        amount1_min: int = 0,
        chain_id: int,
        deadline_seconds: int = 300,
    ) -> tuple[bool, Any]:
        npm = self._get_npm_address(chain_id)
        wallet = self.strategy_wallet_address

        ok, pos = await self.get_position(token_id=token_id, chain_id=chain_id)
        if not ok:
            return False, pos

        for token, amount in [
            (pos["token0"], amount0_desired),
            (pos["token1"], amount1_desired),
        ]:
            if amount > 0:
                approved = await ensure_allowance(
                    token_address=token,
                    owner=wallet,
                    spender=npm,
                    amount=amount,
                    chain_id=chain_id,
                    signing_callback=self.strategy_wallet_signing_callback,
                    approval_amount=MAX_UINT256,
                )
                if not approved[0]:
                    return approved

        params = (
            token_id,
            amount0_desired,
            amount1_desired,
            amount0_min,
            amount1_min,
            self._deadline(deadline_seconds),
        )

        transaction = await encode_call(
            target=npm,
            abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
            fn_name="increaseLiquidity",
            args=[params],
            from_address=wallet,
            chain_id=chain_id,
        )

        txn_hash = await send_transaction(
            transaction, self.strategy_wallet_signing_callback
        )
        return True, txn_hash

    async def remove_liquidity(
        self,
        *,
        token_id: int,
        liquidity: int,
        amount0_min: int = 0,
        amount1_min: int = 0,
        chain_id: int,
        deadline_seconds: int = 300,
        collect: bool = True,
        burn: bool = False,
    ) -> tuple[bool, Any]:
        npm = self._get_npm_address(chain_id)
        wallet = self.strategy_wallet_address

        async with web3_from_chain_id(chain_id) as web3:
            contract = web3.eth.contract(
                address=to_checksum_address(npm),
                abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
            )

            calls: list[bytes] = []

            decrease_params = (
                token_id,
                liquidity,
                amount0_min,
                amount1_min,
                self._deadline(deadline_seconds),
            )
            calls.append(contract.encode_abi("decreaseLiquidity", [decrease_params]))

            if collect:
                collect_params = (token_id, wallet, MAX_UINT128, MAX_UINT128)
                calls.append(contract.encode_abi("collect", [collect_params]))

            if burn:
                calls.append(contract.encode_abi("burn", [token_id]))

        transaction = await encode_call(
            target=npm,
            abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
            fn_name="multicall",
            args=[calls],
            from_address=wallet,
            chain_id=chain_id,
        )

        txn_hash = await send_transaction(
            transaction, self.strategy_wallet_signing_callback
        )
        return True, txn_hash

    async def collect_fees(
        self,
        *,
        token_id: int,
        chain_id: int,
    ) -> tuple[bool, Any]:
        npm = self._get_npm_address(chain_id)
        wallet = self.strategy_wallet_address

        collect_params = (token_id, wallet, MAX_UINT128, MAX_UINT128)

        transaction = await encode_call(
            target=npm,
            abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
            fn_name="collect",
            args=[collect_params],
            from_address=wallet,
            chain_id=chain_id,
        )

        txn_hash = await send_transaction(
            transaction, self.strategy_wallet_signing_callback
        )
        return True, txn_hash

    async def get_uncollected_fees(
        self,
        *,
        token_id: int,
        chain_id: int,
    ) -> tuple[bool, Any]:
        npm = self._get_npm_address(chain_id)
        wallet = self.strategy_wallet_address

        ok, pos = await self.get_position(token_id=token_id, chain_id=chain_id)
        if not ok:
            return False, pos

        async with web3_from_chain_id(chain_id) as web3:
            contract = web3.eth.contract(
                address=to_checksum_address(npm),
                abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
            )
            collect_params = (token_id, wallet, MAX_UINT128, MAX_UINT128)
            result = await contract.functions.collect(collect_params).call(
                {"from": wallet}
            )
            fees0, fees1 = result

        return True, {
            "token0": pos["token0"],
            "token1": pos["token1"],
            "fees0": fees0,
            "fees1": fees1,
        }

    async def get_position(
        self,
        *,
        token_id: int,
        chain_id: int,
    ) -> tuple[bool, Any]:
        npm = self._get_npm_address(chain_id)

        async with web3_from_chain_id(chain_id) as web3:
            contract = web3.eth.contract(
                address=to_checksum_address(npm),
                abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
            )
            result = await contract.functions.positions(token_id).call()

        (
            nonce,
            operator,
            token0,
            token1,
            fee,
            tick_lower,
            tick_upper,
            liquidity,
            fee_growth_inside0_last_x128,
            fee_growth_inside1_last_x128,
            tokens_owed0,
            tokens_owed1,
        ) = result

        return True, {
            "token_id": token_id,
            "nonce": nonce,
            "operator": operator,
            "token0": token0,
            "token1": token1,
            "fee": fee,
            "tick_lower": tick_lower,
            "tick_upper": tick_upper,
            "liquidity": liquidity,
            "fee_growth_inside0_last_x128": fee_growth_inside0_last_x128,
            "fee_growth_inside1_last_x128": fee_growth_inside1_last_x128,
            "tokens_owed0": tokens_owed0,
            "tokens_owed1": tokens_owed1,
        }

    async def get_positions(
        self,
        *,
        chain_id: int,
        account: str | None = None,
    ) -> tuple[bool, Any]:
        npm = self._get_npm_address(chain_id)
        owner = to_checksum_address(account or self.strategy_wallet_address)

        async with web3_from_chain_id(chain_id) as web3:
            contract = web3.eth.contract(
                address=to_checksum_address(npm),
                abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
            )
            balance = await contract.functions.balanceOf(owner).call()

            token_ids: list[int] = []
            for i in range(balance):
                tid = await contract.functions.tokenOfOwnerByIndex(owner, i).call()
                token_ids.append(tid)

        positions: list[dict[str, Any]] = []
        for tid in token_ids:
            ok, pos = await self.get_position(token_id=tid, chain_id=chain_id)
            if ok:
                positions.append(pos)

        return True, positions

    async def get_pool(
        self,
        *,
        token0: str,
        token1: str,
        fee: int,
        chain_id: int,
    ) -> tuple[bool, Any]:
        factory = self._get_factory_address(chain_id)
        t0 = to_checksum_address(token0)
        t1 = to_checksum_address(token1)

        async with web3_from_chain_id(chain_id) as web3:
            contract = web3.eth.contract(
                address=to_checksum_address(factory),
                abi=UNISWAP_V3_FACTORY_ABI,
            )
            pool_address = await contract.functions.getPool(t0, t1, fee).call()

        if pool_address == ZERO_ADDRESS:
            return False, f"No pool found for {t0}/{t1} fee={fee}"

        return True, pool_address

    @staticmethod
    def price_to_tick(
        price: float,
        token0_decimals: int,
        token1_decimals: int,
    ) -> int:
        raw_price = price * 10 ** (token0_decimals - token1_decimals)
        return math.floor(math.log(raw_price) / math.log(1.0001))

    @staticmethod
    def tick_to_price(
        tick: int,
        token0_decimals: int,
        token1_decimals: int,
    ) -> float:
        raw_price = 1.0001**tick
        return raw_price / 10 ** (token0_decimals - token1_decimals)

    @staticmethod
    def nearest_usable_tick(tick: int, tick_spacing: int) -> int:
        if tick_spacing <= 0:
            raise ValueError("tick_spacing must be positive")
        rounded = round(tick / tick_spacing) * tick_spacing
        return rounded

    @staticmethod
    def calculate_il(
        price_initial: float,
        price_current: float,
        tick_lower: int,
        tick_upper: int,
        token0_decimals: int,
        token1_decimals: int,
    ) -> dict[str, float]:
        sa = math.sqrt(1.0001**tick_lower)
        sb = math.sqrt(1.0001**tick_upper)

        decimal_adj = 10 ** (token0_decimals - token1_decimals)
        sp_init = math.sqrt(price_initial * decimal_adj)
        sp_curr = math.sqrt(price_current * decimal_adj)

        sp_init_c = max(sa, min(sp_init, sb))
        sp_curr_c = max(sa, min(sp_curr, sb))

        if sp_init_c == sa:
            x0 = 0.0
            y0_per_l = sp_init_c - sa
            if y0_per_l == 0:
                return {"il_percent": 0.0, "value_lp": 1.0, "value_hold": 1.0}
            L = 1.0
            y0 = L * y0_per_l
            v_init = y0
        elif sp_init_c == sb:
            x0_per_l = 1.0 / sp_init_c - 1.0 / sb
            if x0_per_l == 0:
                return {"il_percent": 0.0, "value_lp": 1.0, "value_hold": 1.0}
            L = 1.0
            x0 = L * x0_per_l
            y0 = 0.0
            raw_price_init = sp_init**2
            v_init = x0 * raw_price_init + y0
        else:
            x0_per_l = 1.0 / sp_init_c - 1.0 / sb
            y0_per_l = sp_init_c - sa
            L = 1.0
            x0 = L * x0_per_l
            y0 = L * y0_per_l
            raw_price_init = sp_init**2
            v_init = x0 * raw_price_init + y0

        if v_init == 0:
            return {"il_percent": 0.0, "value_lp": 1.0, "value_hold": 1.0}

        if sp_curr_c <= sa:
            x1 = L * (1.0 / sa - 1.0 / sb)
            y1 = 0.0
        elif sp_curr_c >= sb:
            x1 = 0.0
            y1 = L * (sb - sa)
        else:
            x1 = L * (1.0 / sp_curr_c - 1.0 / sb)
            y1 = L * (sp_curr_c - sa)

        raw_price_curr = sp_curr**2
        v_lp = x1 * raw_price_curr + y1

        raw_price_init = sp_init**2
        v_hold = x0 * raw_price_curr + y0

        value_lp = v_lp / v_init
        value_hold = v_hold / v_init
        il_percent = (value_lp / value_hold - 1.0) if value_hold > 0 else 0.0

        return {
            "il_percent": il_percent,
            "value_lp": value_lp,
            "value_hold": value_hold,
        }
