from unittest.mock import AsyncMock, Mock, patch

import pytest
from eth_abi import decode, encode
from eth_utils import keccak

from wayfinder_paths.adapters.uniswap_adapter import UniswapAdapter
from wayfinder_paths.adapters.uniswap_adapter.v4 import NATIVE_ADDRESS, PoolKey
from wayfinder_paths.adapters.uniswap_adapter.v4_positions import mint_token_ids
from wayfinder_paths.core.constants.chains import ARC_USDC_ADDRESS, CHAIN_ID_ARC
from wayfinder_paths.core.constants.contracts import UNISWAP_V4_POSITION_MANAGER

OWNER = "0x1111111111111111111111111111111111111111"
WETH = "0x128cC466B61f542da60c70e3aA11c10e19B84EDB"
KEY = PoolKey(WETH, ARC_USDC_ADDRESS, 500, 10, NATIVE_ADDRESS)
MODULE = "wayfinder_paths.adapters.uniswap_adapter.v4_positions"


@pytest.fixture
def adapter():
    return UniswapAdapter(
        {"chain_id": CHAIN_ID_ARC}, wallet_address=OWNER, sign_callback=Mock()
    )


def test_v4_only_chain_rejects_v3_without_fabricated_contract(adapter):
    assert adapter.v4_position_manager == UNISWAP_V4_POSITION_MANAGER[CHAIN_ID_ARC]
    with pytest.raises(ValueError, match="V3 is not deployed"):
        _ = adapter.npm_address


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_mint_uses_canonical_actions_limits_currency_units_and_refund(
    adapter, native
):
    key = PoolKey(NATIVE_ADDRESS, WETH, 500, 10, NATIVE_ADDRESS) if native else KEY
    with (
        patch(
            f"{MODULE}.ensure_allowance", AsyncMock(return_value=(True, None))
        ) as approve,
        patch.object(adapter, "_permit2_approve", AsyncMock()),
        patch.object(adapter, "_chain_deadline", AsyncMock(return_value=12345)),
        patch(f"{MODULE}.send_transaction", AsyncMock(return_value="0xabc")) as send,
        patch(
            f"{MODULE}.wait_for_transaction_receipt",
            AsyncMock(return_value={"logs": []}),
        ),
    ):
        ok, result = await adapter.v4_mint_position(
            pool_key=key,
            tick_lower=-100,
            tick_upper=100,
            liquidity=1000,
            amount0_max=10**18,
            amount1_max=10**6,
        )
    assert ok, result
    tx = send.await_args.args[0]
    assert tx["to"] == adapter.v4_position_manager
    assert tx["value"] == (10**18 if native else 0)
    unlock, deadline = decode(["bytes", "uint256"], bytes.fromhex(tx["data"][10:]))
    actions, params = decode(["bytes", "bytes[]"], unlock)
    assert list(actions) == ([2, 0x12, 0x12, 0x14] if native else [2, 0x12, 0x12])
    assert deadline == 12345
    mint = decode(
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
        params[0],
    )
    assert mint[1:6] == (-100, 100, 1000, 10**18, 10**6)
    assert approve.await_count == (1 if native else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,kwargs,action",
    [
        (
            "v4_decrease_liquidity",
            {"liquidity": 5, "amount0_min": 3, "amount1_min": 2},
            1,
        ),
        ("v4_collect_fees", {}, 1),
        ("v4_close_position", {"amount0_min": 3, "amount1_min": 2}, 3),
    ],
)
async def test_exit_preserves_minima_and_takes_pair(adapter, method, kwargs, action):
    with (
        patch.object(
            adapter,
            "v4_get_position",
            AsyncMock(
                return_value=(True, {"owner": OWNER, "pool_key": KEY, "liquidity": 10})
            ),
        ),
        patch.object(adapter, "_chain_deadline", AsyncMock(return_value=123)),
        patch(f"{MODULE}.send_transaction", AsyncMock(return_value="0xabc")) as send,
        patch(f"{MODULE}.ensure_allowance", AsyncMock()) as approve,
    ):
        ok, result = await getattr(adapter, method)(42, **kwargs)
    assert ok, result
    approve.assert_not_awaited()
    unlock, _ = decode(
        ["bytes", "uint256"], bytes.fromhex(send.await_args.args[0]["data"][10:])
    )
    actions, params = decode(["bytes", "bytes[]"], unlock)
    assert list(actions) == [action, 0x11]
    assert decode(["address", "address", "address"], params[1])[2] == OWNER
    fields = (
        ["uint256", "uint128", "uint128", "bytes"]
        if action == 3
        else ["uint256", "uint256", "uint128", "uint128", "bytes"]
    )
    parsed = decode(fields, params[0])
    assert parsed[-3:-1] == (kwargs.get("amount0_min", 0), kwargs.get("amount1_min", 0))


@pytest.mark.asyncio
async def test_rejects_foreign_owner_before_sending(adapter):
    with (
        patch.object(
            adapter, "v4_get_position", AsyncMock(return_value=(True, {"owner": WETH}))
        ),
        patch(f"{MODULE}.send_transaction", AsyncMock()) as send,
    ):
        ok, error = await adapter.v4_collect_fees(3)
    assert not ok and "not owned" in error
    send.assert_not_awaited()


def test_receipt_mints_ignore_other_managers_and_transfers():
    manager = UNISWAP_V4_POSITION_MANAGER[CHAIN_ID_ARC]
    log = {
        "address": manager,
        "topics": [
            keccak(text="Transfer(address,address,uint256)"),
            bytes(32),
            encode(["address"], [OWNER]),
            encode(["uint256"], [42]),
        ],
    }
    assert mint_token_ids(
        {"logs": [{**log, "address": WETH}, log]}, manager, OWNER
    ) == [42]


@pytest.mark.asyncio
async def test_invalid_tick_or_slippage_amount_never_approves(adapter):
    with patch(f"{MODULE}.ensure_allowance", AsyncMock()) as approve:
        ok, error = await adapter.v4_mint_position(
            pool_key=KEY,
            tick_lower=-101,
            tick_upper=100,
            liquidity=1,
            amount0_max=1,
            amount1_max=1,
        )
    assert not ok and "Ticks" in error
    approve.assert_not_awaited()
