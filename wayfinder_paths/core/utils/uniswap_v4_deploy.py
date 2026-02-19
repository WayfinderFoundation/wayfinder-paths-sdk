from __future__ import annotations

import time
from typing import Any

from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

from wayfinder_paths.core.constants.uniswap_v4_abi import (
    PERMIT2_ABI,
    POOL_MANAGER_ABI,
    POSITION_MANAGER_ABI,
    STATE_VIEW_ABI,
)
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.tokens import ensure_allowance
from wayfinder_paths.core.utils.web3 import web3_from_chain_id


PoolKeyTuple = tuple[str, str, int, int, str]

# Uniswap v4-periphery Actions constants (v4-periphery/src/libraries/Actions.sol)
ACTION_MINT_POSITION = 0x02
ACTION_SETTLE_PAIR = 0x0D


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

def encode_actions_router_params(*, actions: bytes, params: list[bytes]) -> bytes:
    """Encode `unlockData` expected by PositionManager.modifyLiquidities.

    Equivalent to Solidity: `abi.encode(actions, params)`.
    """
    return abi_encode(["bytes", "bytes[]"], [bytes(actions), list(params)])


def encode_mint_position_params(
    *,
    key: PoolKeyTuple,
    tick_lower: int,
    tick_upper: int,
    liquidity: int,
    amount0_max: int,
    amount1_max: int,
    recipient: str,
    hook_data: bytes = b"",
) -> bytes:
    c0, c1, fee, tick_spacing, hooks = key
    pool_key_tuple = (
        to_checksum_address(c0),
        to_checksum_address(c1),
        int(fee),
        int(tick_spacing),
        to_checksum_address(hooks),
    )
    return abi_encode(
        [
            "(address,address,uint24,int24,address)",
            "int24",
            "int24",
            "uint256",
            "uint128",
            "uint128",
            "address",
            "bytes",
        ],
        [
            pool_key_tuple,
            int(tick_lower),
            int(tick_upper),
            int(liquidity),
            int(amount0_max),
            int(amount1_max),
            to_checksum_address(recipient),
            bytes(hook_data),
        ],
    )


def encode_settle_pair_params(*, currency0: str, currency1: str) -> bytes:
    return abi_encode(
        ["address", "address"],
        [to_checksum_address(currency0), to_checksum_address(currency1)],
    )


def build_mint_and_settle_pair_unlock_data(
    *,
    key: PoolKeyTuple,
    tick_lower: int,
    tick_upper: int,
    liquidity: int,
    amount0_max: int,
    amount1_max: int,
    recipient: str,
    hook_data: bytes = b"",
) -> bytes:
    mint_params = encode_mint_position_params(
        key=key,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        liquidity=liquidity,
        amount0_max=amount0_max,
        amount1_max=amount1_max,
        recipient=recipient,
        hook_data=hook_data,
    )
    settle_params = encode_settle_pair_params(currency0=key[0], currency1=key[1])
    actions = bytes([ACTION_MINT_POSITION, ACTION_SETTLE_PAIR])
    return encode_actions_router_params(actions=actions, params=[mint_params, settle_params])


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

async def posm_next_token_id(*, chain_id: int, position_manager_address: str) -> int:
    async with web3_from_chain_id(int(chain_id)) as w3:
        posm = w3.eth.contract(
            address=to_checksum_address(position_manager_address),
            abi=POSITION_MANAGER_ABI,
        )
        return int(await posm.functions.nextTokenId().call(block_identifier="latest"))


async def posm_get_position_liquidity(
    *, chain_id: int, position_manager_address: str, token_id: int
) -> int:
    async with web3_from_chain_id(int(chain_id)) as w3:
        posm = w3.eth.contract(
            address=to_checksum_address(position_manager_address),
            abi=POSITION_MANAGER_ABI,
        )
        return int(
            await posm.functions.getPositionLiquidity(int(token_id)).call(
                block_identifier="latest"
            )
        )


async def ensure_permit2_allowance(
    *,
    token_address: str,
    owner: str,
    permit2_address: str,
    spender: str,
    min_amount: int,
    chain_id: int,
    sign_callback,
    permit2_amount: int | None = None,
    permit2_expiration: int | None = None,
) -> None:
    """Ensure Permit2 can pull `token_address` from `owner` for `spender`."""
    owner = to_checksum_address(owner)
    token_address = to_checksum_address(token_address)
    permit2_address = to_checksum_address(permit2_address)
    spender = to_checksum_address(spender)

    await ensure_allowance(
        token_address=token_address,
        owner=owner,
        spender=permit2_address,
        amount=int(min_amount),
        approval_amount=(2**256 - 1),
        chain_id=int(chain_id),
        signing_callback=sign_callback,
    )

    async with web3_from_chain_id(int(chain_id)) as w3:
        permit2 = w3.eth.contract(address=permit2_address, abi=PERMIT2_ABI)
        cur_amt, cur_exp, _nonce = await permit2.functions.allowance(
            owner, token_address, spender
        ).call(block_identifier="latest")

    now = int(time.time())
    if permit2_amount is None:
        permit2_amount = 2**160 - 1
    if permit2_expiration is None:
        permit2_expiration = 2**48 - 1

    if int(cur_amt) >= int(min_amount) and int(cur_exp) > now:
        return

    approve_tx = await encode_call(
        target=permit2_address,
        abi=PERMIT2_ABI,
        fn_name="approve",
        args=[
            token_address,
            spender,
            int(permit2_amount),
            int(permit2_expiration),
        ],
        from_address=owner,
        chain_id=int(chain_id),
    )
    await send_transaction(approve_tx, sign_callback, wait_for_receipt=True)


async def posm_initialize_and_mint(
    *,
    chain_id: int,
    position_manager_address: str,
    permit2_address: str,
    key: PoolKeyTuple,
    sqrt_price_x96: int,
    tick_lower: int,
    tick_upper: int,
    liquidity: int,
    amount0_max: int,
    amount1_max: int,
    recipient: str,
    from_address: str,
    sign_callback,
    deadline_seconds: int = 1_200,
) -> dict[str, Any]:
    """Initialize a v4 pool (idempotent) and mint+settle a position via PositionManager.multicall."""
    posm_address = to_checksum_address(position_manager_address)
    from_address = to_checksum_address(from_address)
    recipient = to_checksum_address(recipient)
    permit2_address = to_checksum_address(permit2_address)

    await ensure_permit2_allowance(
        token_address=key[0],
        owner=from_address,
        permit2_address=permit2_address,
        spender=posm_address,
        min_amount=int(amount0_max),
        chain_id=int(chain_id),
        sign_callback=sign_callback,
    )
    await ensure_permit2_allowance(
        token_address=key[1],
        owner=from_address,
        permit2_address=permit2_address,
        spender=posm_address,
        min_amount=int(amount1_max),
        chain_id=int(chain_id),
        sign_callback=sign_callback,
    )

    token_id_before = await posm_next_token_id(
        chain_id=int(chain_id),
        position_manager_address=posm_address,
    )

    unlock_data = build_mint_and_settle_pair_unlock_data(
        key=key,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        liquidity=liquidity,
        amount0_max=amount0_max,
        amount1_max=amount1_max,
        recipient=recipient,
        hook_data=b"",
    )
    deadline = int(time.time()) + int(deadline_seconds)

    init_tx = await encode_call(
        target=posm_address,
        abi=POSITION_MANAGER_ABI,
        fn_name="initializePool",
        args=[key, int(sqrt_price_x96)],
        from_address=from_address,
        chain_id=int(chain_id),
    )
    modify_tx = await encode_call(
        target=posm_address,
        abi=POSITION_MANAGER_ABI,
        fn_name="modifyLiquidities",
        args=[unlock_data, int(deadline)],
        from_address=from_address,
        chain_id=int(chain_id),
    )
    multicall_tx = await encode_call(
        target=posm_address,
        abi=POSITION_MANAGER_ABI,
        fn_name="multicall",
        args=[[init_tx["data"], modify_tx["data"]]],
        from_address=from_address,
        chain_id=int(chain_id),
    )

    tx_hash = await send_transaction(multicall_tx, sign_callback, wait_for_receipt=True)

    token_id_after = await posm_next_token_id(
        chain_id=int(chain_id),
        position_manager_address=posm_address,
    )
    minted_token_id = token_id_before if token_id_after > token_id_before else None
    minted_liquidity = (
        await posm_get_position_liquidity(
            chain_id=int(chain_id),
            position_manager_address=posm_address,
            token_id=int(minted_token_id),
        )
        if minted_token_id is not None
        else None
    )

    return {"tx_hash": tx_hash, "token_id": minted_token_id, "liquidity": minted_liquidity}


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
