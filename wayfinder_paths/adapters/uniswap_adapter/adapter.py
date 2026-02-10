from __future__ import annotations

from typing import Any

from eth_utils import to_checksum_address

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.constants.contracts import UNISWAP_V3_FACTORY, UNISWAP_V3_NPM
from wayfinder_paths.core.constants.uniswap_v3_abi import (
    NONFUNGIBLE_POSITION_MANAGER_ABI,
    UNISWAP_V3_FACTORY_ABI,
)
from wayfinder_paths.core.utils.tokens import ensure_allowance
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.uniswap_v3_math import (
    PositionData,
    collect_params,
    deadline,
    find_pool,
    read_all_positions,
    read_position,
    round_tick_to_spacing,
    slippage_min,
)
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

TICK_SPACING: dict[int, int] = {100: 1, 500: 10, 3000: 60, 10000: 200}

SUPPORTED_CHAIN_IDS = set(UNISWAP_V3_NPM.keys())


class UniswapAdapter(BaseAdapter):
    adapter_type = "UNISWAP"

    def __init__(
        self,
        config: dict[str, Any],
        *,
        strategy_wallet_signing_callback=None,
    ) -> None:
        super().__init__("uniswap_adapter", config)

        self.strategy_wallet_signing_callback = strategy_wallet_signing_callback
        self.chain_id: int = int(config.get("chain_id", 8453))

        if self.chain_id not in SUPPORTED_CHAIN_IDS:
            raise ValueError(
                f"Unsupported chain_id {self.chain_id} for Uniswap V3. "
                f"Supported: {sorted(SUPPORTED_CHAIN_IDS)}"
            )

        self.npm_address: str = UNISWAP_V3_NPM[self.chain_id]
        self.factory_address: str = UNISWAP_V3_FACTORY[self.chain_id]

        wallet = (config or {}).get("strategy_wallet") or {}
        addr = wallet.get("address")
        if not addr:
            raise ValueError("strategy_wallet.address is required for UniswapAdapter")
        self.owner: str = to_checksum_address(str(addr))

    def _tick_spacing_for_fee(self, fee: int) -> int:
        spacing = TICK_SPACING.get(fee)
        if spacing is None:
            raise ValueError(
                f"Unknown fee tier {fee}; expected one of {list(TICK_SPACING)}"
            )
        return spacing

    async def add_liquidity(
        self,
        token0: str,
        token1: str,
        fee: int,
        tick_lower: int,
        tick_upper: int,
        amount0_desired: int,
        amount1_desired: int,
        *,
        slippage_bps: int = 50,
    ) -> tuple[bool, Any]:
        t0 = to_checksum_address(token0)
        t1 = to_checksum_address(token1)
        if int(t0, 16) > int(t1, 16):
            t0, t1 = t1, t0
            amount0_desired, amount1_desired = amount1_desired, amount0_desired
            tick_lower, tick_upper = -tick_upper, -tick_lower

        spacing = self._tick_spacing_for_fee(fee)
        tick_lower = round_tick_to_spacing(tick_lower, spacing)
        tick_upper = round_tick_to_spacing(tick_upper, spacing)
        if tick_upper <= tick_lower:
            tick_upper = tick_lower + spacing

        await self._ensure_allowance(t0, self.npm_address, amount0_desired)
        await self._ensure_allowance(t1, self.npm_address, amount1_desired)

        params = (
            t0,
            t1,
            int(fee),
            tick_lower,
            tick_upper,
            int(amount0_desired),
            int(amount1_desired),
            slippage_min(amount0_desired, slippage_bps),
            slippage_min(amount1_desired, slippage_bps),
            self.owner,
            deadline(),
        )

        tx = await encode_call(
            target=self.npm_address,
            abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
            fn_name="mint",
            args=[params],
            from_address=self.owner,
            chain_id=self.chain_id,
        )
        tx_hash = await send_transaction(tx, self.strategy_wallet_signing_callback)
        return True, tx_hash

    async def increase_liquidity(
        self,
        token_id: int,
        amount0_desired: int,
        amount1_desired: int,
        *,
        slippage_bps: int = 50,
    ) -> tuple[bool, Any]:
        async with web3_from_chain_id(self.chain_id) as w3:
            npm = w3.eth.contract(
                address=to_checksum_address(self.npm_address),
                abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
            )
            pos = await read_position(npm, token_id)

        await self._ensure_allowance(pos["token0"], self.npm_address, amount0_desired)
        await self._ensure_allowance(pos["token1"], self.npm_address, amount1_desired)

        params = (
            int(token_id),
            int(amount0_desired),
            int(amount1_desired),
            slippage_min(amount0_desired, slippage_bps),
            slippage_min(amount1_desired, slippage_bps),
            deadline(),
        )

        tx = await encode_call(
            target=self.npm_address,
            abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
            fn_name="increaseLiquidity",
            args=[params],
            from_address=self.owner,
            chain_id=self.chain_id,
        )
        tx_hash = await send_transaction(tx, self.strategy_wallet_signing_callback)
        return True, tx_hash

    async def remove_liquidity(
        self,
        token_id: int,
        *,
        liquidity: int | None = None,
        slippage_bps: int = 50,
        collect: bool = True,
        burn: bool = False,
    ) -> tuple[bool, Any]:
        async with web3_from_chain_id(self.chain_id) as w3:
            npm = w3.eth.contract(
                address=to_checksum_address(self.npm_address),
                abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
            )
            pos = await read_position(npm, token_id)

            current_liq = pos["liquidity"]
            target_liq = min(int(liquidity or current_liq), current_liq)
            if target_liq <= 0 and not collect:
                return True, None

            calls: list[bytes] = []

            if target_liq > 0:
                decrease_params = (
                    int(token_id),
                    target_liq,
                    0,
                    0,
                    deadline(),
                )
                calls.append(
                    npm.encode_abi("decreaseLiquidity", args=[decrease_params])
                )

            if collect:
                calls.append(
                    npm.encode_abi(
                        "collect", args=[collect_params(token_id, self.owner)]
                    )
                )

            if burn:
                calls.append(npm.encode_abi("burn", args=[int(token_id)]))

        tx = await encode_call(
            target=self.npm_address,
            abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
            fn_name="multicall",
            args=[calls],
            from_address=self.owner,
            chain_id=self.chain_id,
        )
        tx_hash = await send_transaction(tx, self.strategy_wallet_signing_callback)
        return True, tx_hash

    async def collect_fees(self, token_id: int) -> tuple[bool, Any]:
        tx = await encode_call(
            target=self.npm_address,
            abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
            fn_name="collect",
            args=[collect_params(token_id, self.owner)],
            from_address=self.owner,
            chain_id=self.chain_id,
        )
        tx_hash = await send_transaction(tx, self.strategy_wallet_signing_callback)
        return True, tx_hash

    async def get_position(self, token_id: int) -> tuple[bool, PositionData]:
        async with web3_from_chain_id(self.chain_id) as w3:
            npm = w3.eth.contract(
                address=to_checksum_address(self.npm_address),
                abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
            )
            pos = await read_position(npm, token_id)
        result = dict(pos)
        result["token_id"] = token_id
        return True, result

    async def get_positions(
        self, owner: str | None = None
    ) -> tuple[bool, list[dict[str, Any]]]:
        target = to_checksum_address(owner) if owner else self.owner
        async with web3_from_chain_id(self.chain_id) as w3:
            npm = w3.eth.contract(
                address=to_checksum_address(self.npm_address),
                abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
            )
            raw = await read_all_positions(npm, target)
        results = []
        for tid, pos in raw:
            d = dict(pos)
            d["token_id"] = tid
            results.append(d)
        return True, results

    async def get_uncollected_fees(self, token_id: int) -> tuple[bool, dict[str, int]]:
        async with web3_from_chain_id(self.chain_id) as w3:
            npm = w3.eth.contract(
                address=to_checksum_address(self.npm_address),
                abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
            )
            result = await npm.functions.collect(
                collect_params(token_id, self.owner)
            ).call({"from": self.owner})
        return True, {"amount0": int(result[0]), "amount1": int(result[1])}

    async def get_pool(
        self, token_a: str, token_b: str, fee: int
    ) -> tuple[bool, str | None]:
        async with web3_from_chain_id(self.chain_id) as w3:
            factory = w3.eth.contract(
                address=to_checksum_address(self.factory_address),
                abi=UNISWAP_V3_FACTORY_ABI,
            )
            pool_addr = await find_pool(factory, token_a, token_b, fee)
        return True, pool_addr

    async def _ensure_allowance(
        self, token_address: str, spender: str, needed: int
    ) -> None:
        if needed <= 0:
            return
        await ensure_allowance(
            token_address=to_checksum_address(token_address),
            owner=self.owner,
            spender=to_checksum_address(spender),
            amount=int(needed),
            chain_id=self.chain_id,
            signing_callback=self.strategy_wallet_signing_callback,
            approval_amount=int(needed * 2),
        )
