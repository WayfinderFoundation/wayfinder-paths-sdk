"""Explicit-ID v4 LP management. No historical log scans or V3 NFT assumptions.

Amounts are raw currency units; liquidity and min/max amounts are explicit so
callers can apply their own range and slippage policy before approving a trade.
Only canonical actions are used, never deprecated MINT/INCREASE_FROM_DELTAS.
"""

from __future__ import annotations

import asyncio
from typing import Any

from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address
from hexbytes import HexBytes

from wayfinder_paths.adapters.uniswap_adapter.v4 import (
    NATIVE_ADDRESS,
    PoolKey,
    UniswapV4SwapMixin,
)
from wayfinder_paths.core.constants.contracts import (
    UNISWAP_PERMIT2,
    UNISWAP_V4_POSITION_MANAGER,
)
from wayfinder_paths.core.utils.tokens import ensure_allowance
from wayfinder_paths.core.utils.transaction import (
    send_transaction,
    wait_for_transaction_receipt,
)
from wayfinder_paths.core.utils.uniswap_v3_math import round_tick_to_spacing
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

POOL_KEY_TYPE = "(address,address,uint24,int24,address)"
TRANSFER_TOPIC = keccak(text="Transfer(address,address,uint256)")


def mint_token_ids(receipt: dict[str, Any], manager: str, owner: str) -> list[int]:
    """Read only mints by this manager to this owner, not another transfer in the tx."""
    ids = []
    for log in receipt.get("logs", []):
        topics = [HexBytes(topic) for topic in log.get("topics", [])]
        if (
            str(log.get("address", "")).lower() == manager.lower()
            and len(topics) == 4
            and topics[0] == TRANSFER_TOPIC
            and int.from_bytes(topics[1]) == 0
            and int.from_bytes(topics[2]) == int(owner, 16)
        ):
            ids.append(int.from_bytes(topics[3]))
    return ids


class UniswapV4PositionsMixin(UniswapV4SwapMixin):
    @property
    def v4_position_manager(self) -> str:
        manager = UNISWAP_V4_POSITION_MANAGER.get(self.chain_id)
        if not manager:
            raise ValueError(
                f"Uniswap v4 positions not configured for chain {self.chain_id}"
            )
        return manager

    async def v4_get_position(self, token_id: int) -> tuple[bool, Any]:
        try:
            manager = self.v4_position_manager
            encoded_id = encode(["uint256"], [token_id])
            async with web3_from_chain_id(self.chain_id) as web3:
                raw_key, raw_liquidity, raw_owner = await asyncio.gather(
                    *(
                        web3.eth.call(
                            {
                                "to": manager,
                                "data": keccak(text=signature)[:4] + encoded_id,
                            }
                        )
                        for signature in (
                            "getPoolAndPositionInfo(uint256)",
                            "getPositionLiquidity(uint256)",
                            "ownerOf(uint256)",
                        )
                    )
                )
            key, info = decode([POOL_KEY_TYPE, "uint256"], raw_key)
            ticks = []
            for offset in (8, 32):
                tick = (info >> offset) & 0xFFFFFF
                ticks.append(tick - (1 << 24) if tick & (1 << 23) else tick)
            pool_key = PoolKey(*key)
            return True, {
                "token_id": token_id,
                "pool_key": pool_key,
                "pool_id": pool_key.pool_id,
                "tick_lower": ticks[0],
                "tick_upper": ticks[1],
                "liquidity": decode(["uint128"], raw_liquidity)[0],
                "owner": to_checksum_address(decode(["address"], raw_owner)[0]),
            }
        except Exception as exc:
            return False, str(exc)

    async def v4_get_positions(self, token_ids: list[int]) -> tuple[bool, Any]:
        if len(token_ids) > 100:
            return False, "At most 100 explicit position IDs per request"
        positions = []
        for token_id in dict.fromkeys(token_ids):
            ok, position = await self.v4_get_position(token_id)
            if not ok:
                return False, position
            positions.append(position)
        return True, positions

    async def _v4_owned_position(self, token_id: int) -> dict[str, Any]:
        ok, position = await self.v4_get_position(token_id)
        if not ok:
            raise ValueError(position)
        if position["owner"].lower() != self.owner.lower():
            raise ValueError("Position is not owned by the selected wallet")
        return position

    async def _v4_modify_position(
        self,
        key: PoolKey,
        action: int,
        params: bytes,
        *,
        amount0_max: int = 0,
        amount1_max: int = 0,
        deadline_seconds: int = 600,
    ) -> dict[str, Any]:
        manager = self.v4_position_manager
        if self.sign_callback is None:
            raise ValueError("sign_callback is required")
        if not 0 < deadline_seconds <= 3600:
            raise ValueError("deadline_seconds must be between 1 and 3600")
        adding = action in (0x00, 0x02)
        value = 0
        if adding:
            for token, amount in (
                (key.currency0, amount0_max),
                (key.currency1, amount1_max),
            ):
                if token.lower() == NATIVE_ADDRESS:
                    value = amount
                elif amount:
                    ok, result = await ensure_allowance(
                        token_address=token,
                        owner=self.owner,
                        spender=UNISWAP_PERMIT2,
                        amount=amount,
                        approval_amount=amount,
                        chain_id=self.chain_id,
                        signing_callback=self.sign_callback,
                    )
                    if not ok:
                        raise ValueError(result)
                    await self._permit2_approve(token, manager)
        # CLOSE_CURRENCY handles either owed principal or credited fees on increase.
        # On exits TAKE_PAIR pays both currencies to the selected owner.
        actions = [action]
        arguments = [params]
        if adding:
            actions.extend([0x12, 0x12])
            arguments.extend(
                encode(["address"], [token]) for token in (key.currency0, key.currency1)
            )
        else:
            actions.append(0x11)
            arguments.append(
                encode(
                    ["address", "address", "address"],
                    [key.currency0, key.currency1, self.owner],
                )
            )
        if value:
            actions.append(0x14)
            arguments.append(
                encode(["address", "address"], [NATIVE_ADDRESS, self.owner])
            )
        unlock_data = encode(["bytes", "bytes[]"], [bytes(actions), arguments])
        deadline = await self._chain_deadline(self.chain_id, deadline_seconds)
        tx = {
            "chainId": self.chain_id,
            "from": self.owner,
            "to": manager,
            "value": value,
            "data": "0x"
            + (
                keccak(text="modifyLiquidities(bytes,uint256)")[:4]
                + encode(["bytes", "uint256"], [unlock_data, deadline])
            ).hex(),
        }
        tx_hash = await send_transaction(tx, self.sign_callback)
        result: dict[str, Any] = {"tx_hash": tx_hash, "pool_id": key.pool_id}
        if action == 0x02:
            receipt = await wait_for_transaction_receipt(
                self.chain_id, tx_hash, confirmations=0
            )
            result["token_ids"] = mint_token_ids(receipt, manager, self.owner)
        return result

    async def v4_mint_position(
        self,
        *,
        pool_key: PoolKey,
        tick_lower: int,
        tick_upper: int,
        liquidity: int,
        amount0_max: int,
        amount1_max: int,
        deadline_seconds: int = 600,
    ) -> tuple[bool, Any]:
        try:
            key = pool_key
            if int(key.currency0, 16) >= int(key.currency1, 16):
                raise ValueError("Pool currencies must be in canonical address order")
            if key.hooks.lower() != NATIVE_ADDRESS:
                raise ValueError("LP creation supports hookless pools only")
            if not 0 < key.tick_spacing <= 32767 or not 0 <= key.fee <= 1_000_000:
                raise ValueError("Invalid pool fee or tick spacing")
            if not -887272 <= tick_lower < tick_upper <= 887272 or any(
                round_tick_to_spacing(tick, key.tick_spacing) != tick
                for tick in (tick_lower, tick_upper)
            ):
                raise ValueError(
                    "Ticks must be ordered, in range and aligned to pool spacing"
                )
            if liquidity <= 0:
                raise ValueError("liquidity must be positive")
            params = encode(
                [
                    POOL_KEY_TYPE,
                    "int24",
                    "int24",
                    "uint256",
                    "uint128",
                    "uint128",
                    "address",
                    "bytes",
                ],
                [
                    key.as_tuple(),
                    tick_lower,
                    tick_upper,
                    liquidity,
                    amount0_max,
                    amount1_max,
                    self.owner,
                    b"",
                ],
            )
            return True, await self._v4_modify_position(
                key,
                0x02,
                params,
                amount0_max=amount0_max,
                amount1_max=amount1_max,
                deadline_seconds=deadline_seconds,
            )
        except Exception as exc:
            return False, str(exc)

    async def v4_increase_liquidity(
        self,
        token_id: int,
        *,
        liquidity: int,
        amount0_max: int,
        amount1_max: int,
    ) -> tuple[bool, Any]:
        try:
            position = await self._v4_owned_position(token_id)
            if liquidity <= 0:
                raise ValueError("liquidity must be positive")
            if position["pool_key"].hooks.lower() != NATIVE_ADDRESS:
                raise ValueError("LP increases support hookless pools only")
            params = encode(
                ["uint256", "uint256", "uint128", "uint128", "bytes"],
                [token_id, liquidity, amount0_max, amount1_max, b""],
            )
            return True, await self._v4_modify_position(
                position["pool_key"],
                0x00,
                params,
                amount0_max=amount0_max,
                amount1_max=amount1_max,
            )
        except Exception as exc:
            return False, str(exc)

    async def v4_decrease_liquidity(
        self,
        token_id: int,
        *,
        liquidity: int,
        amount0_min: int,
        amount1_min: int,
    ) -> tuple[bool, Any]:
        try:
            position = await self._v4_owned_position(token_id)
            if not 0 <= liquidity <= position["liquidity"]:
                raise ValueError("liquidity exceeds the position or is negative")
            params = encode(
                ["uint256", "uint256", "uint128", "uint128", "bytes"],
                [token_id, liquidity, amount0_min, amount1_min, b""],
            )
            return True, await self._v4_modify_position(
                position["pool_key"], 0x01, params
            )
        except Exception as exc:
            return False, str(exc)

    async def v4_collect_fees(self, token_id: int) -> tuple[bool, Any]:
        return await self.v4_decrease_liquidity(
            token_id, liquidity=0, amount0_min=0, amount1_min=0
        )

    async def v4_close_position(
        self, token_id: int, *, amount0_min: int, amount1_min: int
    ) -> tuple[bool, Any]:
        try:
            position = await self._v4_owned_position(token_id)
            params = encode(
                ["uint256", "uint128", "uint128", "bytes"],
                [token_id, amount0_min, amount1_min, b""],
            )
            return True, await self._v4_modify_position(
                position["pool_key"], 0x03, params
            )
        except Exception as exc:
            return False, str(exc)
