"""Uniswap v4 encoding + pool-selection tests.

The high-risk surface is the PoolKey/poolId hashing and the Universal Router
swap encoding — a wrong byte there silently routes to the wrong pool or
reverts. The poolId is asserted against the live Robinhood INDEX/ETH 1% pool
(`0x00dd2df2…46edf3`), reconstructed from its on-chain Initialize event.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from eth_abi import decode as abi_decode

from wayfinder_paths.adapters.uniswap_adapter import v4
from wayfinder_paths.adapters.uniswap_adapter.v4 import (
    NATIVE_ADDRESS,
    PoolKey,
    UniswapV4SwapMixin,
    V4Pool,
    _encode_swap_inputs,
    _sorted_currencies,
    best_pool,
)

# Live Robinhood (4663) INDEX/ETH 1% pool, from its Initialize event.
INDEX = "0x56910d4409f3a0c78c64dd8d0545ff0705389870"
HOOK = "0x2cd91bd228ff4c537031d6b8204782090c84c0cc"
KNOWN_POOL_ID = "0x00dd2df2f17d431cf3a0938f06c9cf9abc5e9643b6cc466ca3f71f3af246edf3"


def test_pool_id_matches_live_index_pool():
    currency0, currency1 = _sorted_currencies(NATIVE_ADDRESS, INDEX)
    key = PoolKey(currency0, currency1, fee=10000, tick_spacing=200, hooks=HOOK)
    assert key.pool_id == KNOWN_POOL_ID


def test_native_sorts_to_currency0():
    # Native (0x0) is numerically smallest, so it is always currency0.
    currency0, currency1 = _sorted_currencies(INDEX, NATIVE_ADDRESS)
    assert currency0 == NATIVE_ADDRESS
    assert currency1.lower() == INDEX


def test_currency_ordering_is_address_sorted():
    low = "0x1111111111111111111111111111111111111111"
    high = "0x9999999999999999999999999999999999999999"
    assert _sorted_currencies(high, low) == (low, high)
    assert _sorted_currencies(low, high) == (low, high)


def test_swap_input_encodes_actions_and_params():
    currency0, currency1 = _sorted_currencies(NATIVE_ADDRESS, INDEX)
    key = PoolKey(currency0, currency1, fee=10000, tick_spacing=200, hooks=HOOK)
    encoded = _encode_swap_inputs(
        key,
        zero_for_one=True,
        amount_in=3 * 10**15,
        min_amount_out=9_000 * 10**18,
        input_currency=NATIVE_ADDRESS,
        output_currency=INDEX,
    )
    actions, params = abi_decode(["bytes", "bytes[]"], encoded)
    # SWAP_EXACT_IN_SINGLE (0x06), SETTLE_ALL (0x0c), TAKE_ALL (0x0f)
    assert actions == bytes([0x06, 0x0C, 0x0F])
    assert len(params) == 3
    # SETTLE_ALL carries (input_currency, amount_in)
    settle_token, settle_amount = abi_decode(["address", "uint256"], params[1])
    assert settle_token == NATIVE_ADDRESS
    assert settle_amount == 3 * 10**15
    # TAKE_ALL carries (output_currency, min_out)
    take_token, take_amount = abi_decode(["address", "uint256"], params[2])
    assert take_token.lower() == INDEX
    assert take_amount == 9_000 * 10**18


@pytest.mark.asyncio
async def test_best_pool_ranks_by_liquidity_not_fee():
    # The deep 1% pool must win over higher-fee dust pools (the traps).
    deep = V4Pool(
        PoolKey(NATIVE_ADDRESS, INDEX, 10000, 200, HOOK), liquidity=44_721 * 10**18
    )
    dust_20pct = V4Pool(
        PoolKey(NATIVE_ADDRESS, INDEX, 200000, 4000, NATIVE_ADDRESS),
        liquidity=250 * 10**18,
    )
    with patch.object(v4, "find_pools", AsyncMock(return_value=[deep, dust_20pct])):
        pool = await best_pool(4663, NATIVE_ADDRESS, INDEX)
    assert pool.key.fee == 10000
    assert pool.liquidity == 44_721 * 10**18


@pytest.mark.asyncio
async def test_v4_quote_uses_best_pool_and_returns_output():
    class _Host(UniswapV4SwapMixin):
        chain_id = 4663
        owner = "0x0000000000000000000000000000000000000001"
        sign_callback = None

    deep = V4Pool(
        PoolKey(NATIVE_ADDRESS, INDEX, 10000, 200, HOOK), liquidity=44_721 * 10**18
    )
    with (
        patch.object(v4, "best_pool", AsyncMock(return_value=deep)),
        patch.object(v4, "quote_exact_in", AsyncMock(return_value=9_549 * 10**18)),
    ):
        ok, result = await _Host().v4_quote(NATIVE_ADDRESS, INDEX, 3 * 10**15)
    assert ok is True
    assert result["amount_out"] == 9_549 * 10**18
    assert result["pool_id"] == KNOWN_POOL_ID
    assert result["fee"] == 10000


@pytest.mark.asyncio
async def test_v4_quote_reports_missing_pool():
    class _Host(UniswapV4SwapMixin):
        chain_id = 4663
        owner = "0x0000000000000000000000000000000000000001"
        sign_callback = None

    with patch.object(v4, "best_pool", AsyncMock(return_value=None)):
        ok, result = await _Host().v4_quote(NATIVE_ADDRESS, INDEX, 3 * 10**15)
    assert ok is False
    assert "No v4 pool" in result


def test_v4_supported_flags_robinhood_only_where_configured():
    assert v4.v4_supported(4663) is True
    assert v4.v4_supported(999999) is False
