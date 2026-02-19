from __future__ import annotations

from typing import Any

from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

from wayfinder_paths.core.constants.uniswap_v4_abi import POOL_MANAGER_ABI, STATE_VIEW_ABI
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id


PoolKeyTuple = tuple[str, str, int, int, str]


def sort_currencies(currency_a: str, currency_b: str) -> tuple[str, str]:
    a = to_checksum_address(currency_a)
    b = to_checksum_address(currency_b)
    return (a, b) if int(a, 16) < int(b, 16) else (b, a)


def build_pool_key(
    *,
    currency_a: str,
    currency_b: str,
    fee: int,
    tick_spacing: int,
    hooks: str,
) -> PoolKeyTuple:
    c0, c1 = sort_currencies(currency_a, currency_b)
    return (c0, c1, int(fee), int(tick_spacing), to_checksum_address(hooks))


def pool_id(key: PoolKeyTuple) -> str:
    c0, c1, fee, tick_spacing, hooks = key
    encoded = abi_encode(
        ["address", "address", "uint24", "int24", "address"],
        [
            to_checksum_address(c0),
            to_checksum_address(c1),
            int(fee),
            int(tick_spacing),
            to_checksum_address(hooks),
        ],
    )
    return "0x" + keccak(encoded).hex()


async def initialize_pool(
    *,
    chain_id: int,
    pool_manager_address: str,
    key: PoolKeyTuple,
    sqrt_price_x96: int,
    from_address: str,
    sign_callback,
) -> str:
    tx = await encode_call(
        target=to_checksum_address(pool_manager_address),
        abi=POOL_MANAGER_ABI,
        fn_name="initialize",
        args=[key, int(sqrt_price_x96)],
        from_address=to_checksum_address(from_address),
        chain_id=int(chain_id),
    )
    return await send_transaction(tx, sign_callback, wait_for_receipt=True)


async def get_slot0(
    *,
    chain_id: int,
    state_view_address: str,
    pool_id_: str,
) -> dict[str, Any]:
    async with web3_from_chain_id(chain_id) as w3:
        view = w3.eth.contract(
            address=to_checksum_address(state_view_address),
            abi=STATE_VIEW_ABI,
        )
        sqrt_price_x96, tick, protocol_fee, lp_fee = await view.functions.getSlot0(
            pool_id_
        ).call(block_identifier="latest")
        return {
            "sqrtPriceX96": int(sqrt_price_x96),
            "tick": int(tick),
            "protocolFee": int(protocol_fee),
            "lpFee": int(lp_fee),
        }
