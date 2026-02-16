from __future__ import annotations

from typing import Any

from eth_utils import to_checksum_address

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
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


class UniswapV3BaseAdapter(BaseAdapter):
    def __init__(
        self,
        adapter_name: str,
        config: dict[str, Any],
        *,
        chain_id: int,
        npm_address: str,
        factory_address: str,
        owner: str,
        strategy_wallet_signing_callback=None,
        factory_abi: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(adapter_name, config)
        self.strategy_wallet_signing_callback = strategy_wallet_signing_callback
        self.chain_id = int(chain_id)
        self.npm_address = to_checksum_address(str(npm_address))
        self.factory_address = to_checksum_address(str(factory_address))
        self.owner = to_checksum_address(str(owner))
        self._factory_abi = factory_abi or UNISWAP_V3_FACTORY_ABI

    def _tick_spacing_for_fee(self, fee: int) -> int:
        spacing = TICK_SPACING.get(int(fee))
        if spacing is None:
            raise ValueError(
                f"Unknown fee tier {fee}; expected one of {list(TICK_SPACING)}"
            )
        return int(spacing)

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
        tick_spacing: int | None = None,
    ) -> tuple[bool, Any]:
        try:
            t0 = to_checksum_address(token0)
            t1 = to_checksum_address(token1)
            if int(t0, 16) > int(t1, 16):
                t0, t1 = t1, t0
                amount0_desired, amount1_desired = amount1_desired, amount0_desired
                tick_lower, tick_upper = -tick_upper, -tick_lower

            spacing = int(tick_spacing) if tick_spacing is not None else None
            if spacing is None:
                spacing = self._tick_spacing_for_fee(fee)
            tick_lower = round_tick_to_spacing(int(tick_lower), spacing)
            tick_upper = round_tick_to_spacing(int(tick_upper), spacing)
            if tick_upper <= tick_lower:
                tick_upper = tick_lower + spacing

            await ensure_allowance(
                token_address=t0,
                owner=self.owner,
                spender=to_checksum_address(self.npm_address),
                amount=int(amount0_desired),
                chain_id=self.chain_id,
                signing_callback=self.strategy_wallet_signing_callback,
                approval_amount=int(amount0_desired * 2),
            )
            await ensure_allowance(
                token_address=t1,
                owner=self.owner,
                spender=to_checksum_address(self.npm_address),
                amount=int(amount1_desired),
                chain_id=self.chain_id,
                signing_callback=self.strategy_wallet_signing_callback,
                approval_amount=int(amount1_desired * 2),
            )

            params = (
                t0,
                t1,
                int(fee),
                int(tick_lower),
                int(tick_upper),
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
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def increase_liquidity(
        self,
        token_id: int,
        amount0_desired: int,
        amount1_desired: int,
        *,
        slippage_bps: int = 50,
    ) -> tuple[bool, Any]:
        try:
            async with web3_from_chain_id(self.chain_id) as w3:
                npm = w3.eth.contract(
                    address=to_checksum_address(self.npm_address),
                    abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
                )
                pos = await read_position(npm, int(token_id))

            await ensure_allowance(
                token_address=to_checksum_address(pos["token0"]),
                owner=self.owner,
                spender=to_checksum_address(self.npm_address),
                amount=int(amount0_desired),
                chain_id=self.chain_id,
                signing_callback=self.strategy_wallet_signing_callback,
                approval_amount=int(amount0_desired * 2),
            )
            await ensure_allowance(
                token_address=to_checksum_address(pos["token1"]),
                owner=self.owner,
                spender=to_checksum_address(self.npm_address),
                amount=int(amount1_desired),
                chain_id=self.chain_id,
                signing_callback=self.strategy_wallet_signing_callback,
                approval_amount=int(amount1_desired * 2),
            )

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
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def remove_liquidity(
        self,
        token_id: int,
        *,
        liquidity: int | None = None,
        slippage_bps: int = 50,  # noqa: ARG002 - kept for API compatibility
        collect: bool = True,
        burn: bool = False,
    ) -> tuple[bool, Any]:
        try:
            async with web3_from_chain_id(self.chain_id) as w3:
                npm = w3.eth.contract(
                    address=to_checksum_address(self.npm_address),
                    abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
                )
                pos = await read_position(npm, int(token_id))

                current_liq = int(pos["liquidity"])
                target_liq = min(int(liquidity or current_liq), current_liq)
                if target_liq <= 0 and not collect and not burn:
                    return True, None

                calls: list[bytes] = []

                if target_liq > 0:
                    decrease_params = (
                        int(token_id),
                        int(target_liq),
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
                            "collect", args=[collect_params(int(token_id), self.owner)]
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
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def collect_fees(self, token_id: int) -> tuple[bool, Any]:
        try:
            tx = await encode_call(
                target=self.npm_address,
                abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
                fn_name="collect",
                args=[collect_params(int(token_id), self.owner)],
                from_address=self.owner,
                chain_id=self.chain_id,
            )
            tx_hash = await send_transaction(tx, self.strategy_wallet_signing_callback)
            return True, tx_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_position(self, token_id: int) -> tuple[bool, PositionData | str]:
        try:
            async with web3_from_chain_id(self.chain_id) as w3:
                npm = w3.eth.contract(
                    address=to_checksum_address(self.npm_address),
                    abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
                )
                pos = await read_position(npm, int(token_id))
            result = dict(pos)
            result["token_id"] = int(token_id)
            return True, result
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_positions(
        self, owner: str | None = None
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        try:
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
                d["token_id"] = int(tid)
                results.append(d)
            return True, results
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_full_user_state(
        self,
        *,
        account: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        """Return a best-effort snapshot of a user's Uniswap V3 positions."""
        ok, positions = await self.get_positions(owner=account)
        if not ok:
            return False, str(positions)

        protocol = (self.adapter_type or self.name).lower()
        acct = to_checksum_address(account)
        return (
            True,
            {
                "protocol": protocol,
                "chainId": int(self.chain_id),
                "account": acct,
                "positions": positions,
            },
        )

    async def get_uncollected_fees(
        self, token_id: int
    ) -> tuple[bool, dict[str, int] | str]:
        try:
            async with web3_from_chain_id(self.chain_id) as w3:
                npm = w3.eth.contract(
                    address=to_checksum_address(self.npm_address),
                    abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
                )
                result = await npm.functions.collect(
                    collect_params(int(token_id), self.owner)
                ).call({"from": self.owner})
            return True, {"amount0": int(result[0]), "amount1": int(result[1])}
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_pool(
        self, token_a: str, token_b: str, fee: int
    ) -> tuple[bool, str | None]:
        try:
            async with web3_from_chain_id(self.chain_id) as w3:
                factory = w3.eth.contract(
                    address=to_checksum_address(self.factory_address),
                    abi=self._factory_abi,
                )
                pool_addr = await find_pool(factory, token_a, token_b, int(fee))
            return True, pool_addr
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
