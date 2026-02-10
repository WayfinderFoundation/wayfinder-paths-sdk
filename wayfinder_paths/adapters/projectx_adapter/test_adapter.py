from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import wayfinder_paths.adapters.projectx_adapter.adapter as projectx_adapter_module
from wayfinder_paths.adapters.projectx_adapter.adapter import ProjectXLiquidityAdapter
from wayfinder_paths.core.constants import ZERO_ADDRESS


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
        {"strategy_wallet": {"address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}
    )
    out = await adapter.find_pool_for_pair(
        "0x1111111111111111111111111111111111111111",
        "0x3333333333333333333333333333333333333333",
        prefer_fees=[100, 500, 1000],
    )
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
        {"strategy_wallet": {"address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}},
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

    fake_npm = _FakeNpmContract()
    fake_web3 = _FakeWeb3(fake_npm)
    monkeypatch.setattr(
        projectx_adapter_module,
        "web3_from_chain_id",
        lambda _cid: _DummyAsyncContext(fake_web3),
    )
    monkeypatch.setattr(
        projectx_adapter_module,
        "ensure_allowance",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        projectx_adapter_module,
        "send_transaction",
        AsyncMock(return_value="0xtxhash"),
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

    assert fake_npm.calls and fake_npm.calls[0][0] == "mint"
    (params,) = fake_npm.calls[0][1]
    assert params[3] == -180
    assert params[4] == -130
    assert params[5] == 500_000
    assert params[6] == 700_000
    assert params[7] == 498_500
    assert params[8] == 697_900


@pytest.mark.asyncio
async def test_burn_position_decreases_collects_then_burns(monkeypatch):
    adapter = ProjectXLiquidityAdapter(
        {"strategy_wallet": {"address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}},
        strategy_wallet_signing_callback=AsyncMock(return_value="0xsigned"),
    )
    adapter._read_position_struct = AsyncMock(
        return_value={"liquidity": 42, "tokens_owed0": 0, "tokens_owed1": 0}
    )
    adapter.decrease_liquidity = AsyncMock(return_value="0xtx_decrease")
    adapter.collect_fees = AsyncMock(return_value=("0xtx_collect", {}))

    fake_npm = _FakeNpmContract()
    fake_web3 = _FakeWeb3(fake_npm)
    monkeypatch.setattr(
        projectx_adapter_module,
        "web3_from_chain_id",
        lambda _cid: _DummyAsyncContext(fake_web3),
    )
    monkeypatch.setattr(
        projectx_adapter_module,
        "send_transaction",
        AsyncMock(return_value="0xtx_burn"),
    )

    tx_hash = await adapter.burn_position(123)
    assert tx_hash == "0xtx_burn"
    adapter.decrease_liquidity.assert_awaited_once_with(123, liquidity=42)
    adapter.collect_fees.assert_awaited_once_with(123)
    assert fake_npm.calls and fake_npm.calls[0][0] == "burn"
    assert fake_npm.calls[0][1] == [123]
