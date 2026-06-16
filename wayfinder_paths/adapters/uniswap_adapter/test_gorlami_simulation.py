from __future__ import annotations

import httpx
import pytest
from eth_account import Account

from wayfinder_paths.adapters.uniswap_adapter.adapter import UniswapAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.contracts import BASE_USDC, BASE_WETH
from wayfinder_paths.core.utils import web3 as web3_utils
from wayfinder_paths.core.utils.uniswap_v3_math import get_pool_slot0, ticks_for_range
from wayfinder_paths.testing.gorlami import gorlami_configured

pytestmark = pytest.mark.skipif(
    not gorlami_configured(),
    reason="api_key not configured (needed for gorlami fork proxy)",
)

CHAIN_ID = CHAIN_ID_BASE
FEE = 500
TICK_SPACING = 10


async def _ensure_base_fork(gorlami) -> str:
    try:
        async with web3_utils.web3_from_chain_id(CHAIN_ID) as web3:
            assert await web3.eth.chain_id == int(CHAIN_ID)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status is not None and status >= 500:
            pytest.skip(
                f"gorlami could not create fork for chain_id={CHAIN_ID} (HTTP {status})"
            )
        raise
    except Exception as exc:
        pytest.skip(f"gorlami RPC unreachable for chain_id={CHAIN_ID}: {exc}")

    fork_info = gorlami.forks.get(str(CHAIN_ID))
    assert fork_info is not None
    return fork_info["fork_id"]


def _make_adapter(acct: Account) -> UniswapAdapter:
    async def sign_cb(tx: dict) -> bytes:
        signed = acct.sign_transaction(tx)
        return signed.raw_transaction

    return UniswapAdapter(
        {"chain_id": CHAIN_ID},
        sign_callback=sign_cb,
        wallet_address=acct.address,
    )


@pytest.mark.asyncio
async def test_gorlami_uniswap_v3_mints_base_weth_usdc_position(gorlami):
    fork_id = await _ensure_base_fork(gorlami)
    acct = Account.create()
    adapter = _make_adapter(acct)

    await gorlami.set_native_balance(fork_id, acct.address, 2 * 10**18)
    await gorlami.set_erc20_balance(fork_id, BASE_WETH, acct.address, 10**16)
    await gorlami.set_erc20_balance(fork_id, BASE_USDC, acct.address, 100 * 10**6)

    ok, pool_address = await adapter.get_pool(BASE_WETH, BASE_USDC, FEE)
    assert ok is True, pool_address
    if pool_address is None:
        pytest.skip("Base WETH/USDC 0.05% Uniswap V3 pool not found on fork")

    slot0 = await get_pool_slot0(pool_address, CHAIN_ID, 18, 6)
    tick_lower, tick_upper = ticks_for_range(
        int(slot0["tick"]), bps=1_000, spacing=TICK_SPACING
    )

    weth_amount = 10**15
    usdc_amount = max(1_000_000, int((weth_amount / 10**18) * slot0["price"] * 10**6))

    ok, tx_hash = await adapter.add_liquidity(
        token0=BASE_WETH,
        token1=BASE_USDC,
        fee=FEE,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        amount0_desired=weth_amount,
        amount1_desired=usdc_amount,
        slippage_bps=3_000,
    )
    assert ok is True, tx_hash
    assert isinstance(tx_hash, str) and tx_hash.startswith("0x")

    ok, positions = await adapter.get_positions()
    assert ok is True, positions
    assert any(
        str(pos["token0"]).lower() == BASE_WETH.lower()
        and str(pos["token1"]).lower() == BASE_USDC.lower()
        and int(pos["fee"]) == FEE
        and int(pos["liquidity"]) > 0
        for pos in positions
    )
