from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import wayfinder_paths.adapters.projectx_adapter.adapter as projectx_adapter_module
import wayfinder_paths.adapters.uniswap_adapter.base as uniswap_base_module
from wayfinder_paths.adapters.projectx_adapter.adapter import ProjectXLiquidityAdapter
from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.constants.projectx import THBILL_USDC_POOL


def test_init_requires_strategy_wallet():
    with pytest.raises(ValueError):
        ProjectXLiquidityAdapter({})


def test_classify_range_state():
    assert (
        ProjectXLiquidityAdapter.classify_range_state([0], -10, 10, fallback_tick=None)
        == "in_range"
    )
    assert (
        ProjectXLiquidityAdapter.classify_range_state([-20, 0], -10, 10)
        == "entering_out_of_range"
    )
    assert (
        ProjectXLiquidityAdapter.classify_range_state([-20, 20], -10, 10)
        == "out_of_range"
    )
    assert (
        ProjectXLiquidityAdapter.classify_range_state([], -10, 10, fallback_tick=None)
        == "unknown"
    )
    assert (
        ProjectXLiquidityAdapter.classify_range_state([], -10, 10, fallback_tick=0)
        == "in_range"
    )


class _DummyAsyncContext:
    def __init__(self, obj):
        self._obj = obj

    async def __aenter__(self):
        return self._obj

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeCall:
    def __init__(self, return_value):
        self._return_value = return_value

    async def call(self, block_identifier="latest"):
        return self._return_value


class _FakeFactoryFunctions:
    def __init__(self, pools_by_fee: dict[int, str]):
        self._pools_by_fee = pools_by_fee

    def getPool(self, token_a, token_b, fee):  # noqa: N802 - matches ABI
        return _FakeCall(self._pools_by_fee.get(int(fee), ZERO_ADDRESS))


class _FakeFactoryContract:
    def __init__(self, pools_by_fee: dict[int, str]):
        self.functions = _FakeFactoryFunctions(pools_by_fee)


class _FakeEth:
    def __init__(self, contract):
        self._contract = contract

    def contract(self, address=None, abi=None):  # noqa: ARG002 - unused in fake
        return self._contract


class _FakeWeb3:
    def __init__(self, contract):
        self.eth = _FakeEth(contract)


@pytest.mark.asyncio
async def test_find_pool_for_pair_picks_first_nonzero_fee(monkeypatch):
    pools = {
        100: ZERO_ADDRESS,
        500: ZERO_ADDRESS,
        1000: "0x2222222222222222222222222222222222222222",
    }
    fake_web3 = _FakeWeb3(_FakeFactoryContract(pools))
    monkeypatch.setattr(
        projectx_adapter_module,
        "web3_from_chain_id",
        lambda _cid: _DummyAsyncContext(fake_web3),
    )

    adapter = ProjectXLiquidityAdapter(
        {
            "strategy_wallet": {
                "address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            },
            "pool_address": THBILL_USDC_POOL,
        }
    )
    ok, out = await adapter.find_pool_for_pair(
        "0x1111111111111111111111111111111111111111",
        "0x3333333333333333333333333333333333333333",
        prefer_fees=[100, 500, 1000],
    )
    assert ok is True
    assert out["fee"] == 1000
    assert out["pool"].lower() == "0x2222222222222222222222222222222222222222"


class _FakeNpmContract:
    def __init__(self):
        self.calls: list[tuple[str, list]] = []

    def encode_abi(self, fn_name, args=None, **kwargs):
        if args is None:
            args = kwargs.get("args")
        self.calls.append((str(fn_name), list(args or [])))
        return "0xdeadbeef"


@pytest.mark.asyncio
async def test_mint_from_balances_adjusts_ticks_and_uses_int_min_amounts(monkeypatch):
    adapter = ProjectXLiquidityAdapter(
        {
            "strategy_wallet": {
                "address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            },
            "pool_address": THBILL_USDC_POOL,
        },
        strategy_wallet_signing_callback=AsyncMock(return_value="0xsigned"),
    )
    adapter._balance_for_band = AsyncMock(return_value=None)
    adapter._extract_token_id_from_receipt = AsyncMock(return_value=123)
    adapter._token_meta = AsyncMock(return_value={"decimals": 6, "symbol": "TKN"})

    before_bal0 = 1_000_000
    before_bal1 = 2_000_000
    post_bal0 = 900_000
    post_bal1 = 1_500_000
    adapter._balance = AsyncMock(
        side_effect=[before_bal0, before_bal1, post_bal0, post_bal1]
    )

    meta1 = {
        "sqrt_price_x96": 1,
        "tick": 0,
        "tick_spacing": 10,
        "fee": 100,
        "liquidity": 1,
        "token0": "0x1111111111111111111111111111111111111111",
        "token1": "0x3333333333333333333333333333333333333333",
    }
    meta2 = dict(meta1)
    adapter._sync_pool_meta = AsyncMock(side_effect=[meta1, meta2])

    monkeypatch.setattr(projectx_adapter_module, "liq_for_amounts", lambda *a, **k: 123)
    monkeypatch.setattr(
        projectx_adapter_module,
        "amounts_for_liq_inrange",
        lambda *a, **k: (500_000, 700_000),
    )

    fake_web3 = _FakeWeb3(_FakeNpmContract())
    monkeypatch.setattr(
        projectx_adapter_module,
        "web3_from_chain_id",
        lambda _cid: _DummyAsyncContext(fake_web3),
    )
    monkeypatch.setattr(
        uniswap_base_module, "ensure_allowance", AsyncMock(return_value=None)
    )
    mock_encode_call = AsyncMock(return_value={"chainId": 8453, "data": "0x"})
    monkeypatch.setattr(uniswap_base_module, "encode_call", mock_encode_call)
    monkeypatch.setattr(
        uniswap_base_module, "send_transaction", AsyncMock(return_value="0xtxhash")
    )

    token_id, tx_hash, spent = await adapter._mint_from_balances_once(
        tick_lower=-175,
        tick_upper=-135,
        slippage_bps=30,
    )
    assert token_id == 123
    assert tx_hash == "0xtxhash"
    assert spent == {
        "token0_spent": before_bal0 - post_bal0,
        "token1_spent": before_bal1 - post_bal1,
    }

    adapter._balance_for_band.assert_awaited_once_with(-180, -130, slippage_bps=30)
    assert adapter._sync_pool_meta.await_count == 2

    mock_encode_call.assert_awaited_once()
    call_kwargs = mock_encode_call.call_args.kwargs
    assert call_kwargs["fn_name"] == "mint"
    (params,) = call_kwargs["args"]
    assert params[3] == -180
    assert params[4] == -130
    assert params[5] == 500_000
    assert params[6] == 700_000
    assert params[7] == 498_500
    assert params[8] == 697_900


@pytest.mark.asyncio
async def test_burn_position_calls_remove_liquidity():
    adapter = ProjectXLiquidityAdapter(
        {
            "strategy_wallet": {
                "address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            },
            "pool_address": THBILL_USDC_POOL,
        },
        strategy_wallet_signing_callback=AsyncMock(return_value="0xsigned"),
    )
    adapter.remove_liquidity = AsyncMock(return_value=(True, "0xtx_burn"))

    ok, tx_hash = await adapter.burn_position(123)
    assert ok is True
    assert tx_hash == "0xtx_burn"
    adapter.remove_liquidity.assert_awaited_once_with(123, collect=True, burn=True)


@pytest.mark.asyncio
async def test_get_full_user_state_serializes_positions():
    adapter = ProjectXLiquidityAdapter(
        {
            "strategy_wallet": {
                "address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            },
            "pool_address": THBILL_USDC_POOL,
        }
    )
    adapter.pool_overview = AsyncMock(return_value=(True, {"pool": "ok"}))
    adapter.current_balances = AsyncMock(return_value=(True, {"token": 123}))
    adapter.list_positions = AsyncMock(
        return_value=(
            True,
            [
                projectx_adapter_module.PositionSnapshot(
                    token_id=321,
                    liquidity=1,
                    tick_lower=0,
                    tick_upper=0,
                    fee=100,
                    token0="0x1111111111111111111111111111111111111111",
                    token1="0x3333333333333333333333333333333333333333",
                )
            ],
        )
    )
    adapter.fetch_prjx_points = AsyncMock(return_value=(True, {"points": 42}))

    ok, state = await adapter.get_full_user_state(
        account="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    )
    assert ok is True
    assert state["protocol"] == "projectx"
    assert state["account"].lower() == "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert state["pool"] == THBILL_USDC_POOL
    assert state["points"] == {"points": 42}
    assert state["poolOverview"] == {"pool": "ok"}
    assert state["balances"] == {"token": 123}

    assert isinstance(state["positions"], list)
    assert state["positions"][0]["token_id"] == 321
    assert isinstance(state["positions"][0], dict)


@pytest.mark.asyncio
async def test_get_full_user_state_returns_false_when_everything_fails():
    adapter = ProjectXLiquidityAdapter(
        {
            "strategy_wallet": {
                "address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            },
            "pool_address": THBILL_USDC_POOL,
        }
    )
    adapter.pool_overview = AsyncMock(return_value=(False, "no overview"))
    adapter.current_balances = AsyncMock(return_value=(False, "no balances"))
    adapter.list_positions = AsyncMock(return_value=(False, "no positions"))
    adapter.fetch_prjx_points = AsyncMock(return_value=(False, "no points"))

    ok, state = await adapter.get_full_user_state()
    assert ok is False
    assert state["errors"] == {
        "poolOverview": "no overview",
        "balances": "no balances",
        "positions": "no positions",
        "points": "no points",
    }


@pytest.mark.asyncio
async def test_poll_for_any_position_id_destructures_list_positions_tuple(monkeypatch):
    monkeypatch.setattr(projectx_adapter_module, "MINT_POLL_ATTEMPTS", 1)
    monkeypatch.setattr(
        projectx_adapter_module.asyncio, "sleep", AsyncMock(return_value=None)
    )

    adapter = ProjectXLiquidityAdapter(
        {
            "strategy_wallet": {
                "address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            },
            "pool_address": THBILL_USDC_POOL,
        }
    )
    position = projectx_adapter_module.PositionSnapshot(
        token_id=321,
        liquidity=1,
        tick_lower=0,
        tick_upper=0,
        fee=100,
        token0="0x1111111111111111111111111111111111111111",
        token1="0x3333333333333333333333333333333333333333",
    )
    adapter.list_positions = AsyncMock(return_value=(True, [position]))

    token_id = await adapter._poll_for_any_position_id()
    assert token_id == 321
    adapter.list_positions.assert_awaited_once()


@pytest.mark.asyncio
async def test_swap_once_to_band_ratio_returns_false_on_no_prjx_route(monkeypatch):
    monkeypatch.setattr(
        projectx_adapter_module,
        "web3_from_chain_id",
        lambda _cid: _DummyAsyncContext(object()),
    )
    monkeypatch.setattr(
        projectx_adapter_module, "_target_ratio_need0_over_need1", lambda *a, **k: 1.0
    )
    monkeypatch.setattr(
        projectx_adapter_module, "sqrt_price_x96_to_price", lambda *a: 1.0
    )

    adapter = ProjectXLiquidityAdapter(
        {
            "strategy_wallet": {
                "address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            },
            "pool_address": THBILL_USDC_POOL,
        }
    )
    adapter._pool_meta = AsyncMock(
        return_value={
            "token0": "0x1111111111111111111111111111111111111111",
            "token1": "0x3333333333333333333333333333333333333333",
        }
    )
    adapter._balances_for_tokens = AsyncMock(return_value=(2_000_000, 1_000_000))
    adapter._token_meta = AsyncMock(return_value={"decimals": 6, "symbol": "TKN"})
    adapter.swap_exact_in = AsyncMock(return_value=(False, "No PRJX route for pair"))

    swapped = await adapter._swap_once_to_band_ratio(1, 1, 1, slippage_bps=30)
    assert swapped is False


@pytest.mark.asyncio
async def test_swap_once_to_band_ratio_raises_on_swap_failure(monkeypatch):
    monkeypatch.setattr(
        projectx_adapter_module,
        "web3_from_chain_id",
        lambda _cid: _DummyAsyncContext(object()),
    )
    monkeypatch.setattr(
        projectx_adapter_module, "_target_ratio_need0_over_need1", lambda *a, **k: 1.0
    )
    monkeypatch.setattr(
        projectx_adapter_module, "sqrt_price_x96_to_price", lambda *a: 1.0
    )

    adapter = ProjectXLiquidityAdapter(
        {
            "strategy_wallet": {
                "address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            },
            "pool_address": THBILL_USDC_POOL,
        }
    )
    adapter._pool_meta = AsyncMock(
        return_value={
            "token0": "0x1111111111111111111111111111111111111111",
            "token1": "0x3333333333333333333333333333333333333333",
        }
    )
    adapter._balances_for_tokens = AsyncMock(return_value=(2_000_000, 1_000_000))
    adapter._token_meta = AsyncMock(return_value={"decimals": 6, "symbol": "TKN"})
    adapter.swap_exact_in = AsyncMock(return_value=(False, "Swap failed"))

    with pytest.raises(RuntimeError, match="Swap failed"):
        await adapter._swap_once_to_band_ratio(1, 1, 1, slippage_bps=30)
