from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wayfinder_paths.adapters.uniswap_adapter.adapter import UniswapAdapter
from wayfinder_paths.core.utils.uniswap_v3_math import tick_to_price, ticks_for_range

OWNER = "0xaAaAaAaaAaAaAaaAaAAAAAAAAaaaAaAaAaaAaaAa"
TOKEN_A = "0x1111111111111111111111111111111111111111"
TOKEN_B = "0x3333333333333333333333333333333333333333"
BASE_MODULE = "wayfinder_paths.adapters.uniswap_adapter.base"

FAKE_POS_RAW = (0, OWNER, TOKEN_A, TOKEN_B, 3000, -120, 120, 5000, 0, 0, 10, 20)


def _make_adapter(chain_id: int = 8453) -> UniswapAdapter:
    return UniswapAdapter(
        {"chain_id": chain_id},
        sign_callback=AsyncMock(return_value=b"signed"),
        wallet_address=OWNER,
    )


class _FakeCall:
    def __init__(self, rv):
        self._rv = rv

    async def call(self, *args, **kwargs):
        return self._rv


class _FakeCollectCall:
    def __init__(self, rv):
        self._rv = rv

    def __call__(self, *args, **kwargs):
        return self

    async def call(self, *args, **kwargs):
        return self._rv


class _FakeNpm:
    def __init__(self, pos_raw=FAKE_POS_RAW, balance=1, token_ids=None):
        self._pos_raw = pos_raw
        self._balance = balance
        self._token_ids = token_ids or [99]
        self._encode_calls: list[tuple[str, list]] = []

    @property
    def functions(self):
        return self

    def positions(self, token_id):
        return _FakeCall(self._pos_raw)

    def balanceOf(self, owner):  # noqa: N802
        return _FakeCall(self._balance)

    def tokenOfOwnerByIndex(self, owner, idx):  # noqa: N802
        return _FakeCall(self._token_ids[idx] if idx < len(self._token_ids) else 0)

    def collect(self, params):
        return _FakeCollectCall((1000, 2000))

    def encode_abi(self, fn_name, args=None):
        self._encode_calls.append((fn_name, list(args or [])))
        return f"0x_{fn_name}"


class _FakeFactory:
    def __init__(self, pool_addr):
        self._pool_addr = pool_addr

    @property
    def functions(self):
        return self

    def getPool(self, token_a, token_b, fee):  # noqa: N802
        return _FakeCall(self._pool_addr)


class _Web3Ctx:
    """Async context manager returning a fake w3 with a specific contract."""

    def __init__(self, contract):
        self._contract = contract

    async def __aenter__(self):
        w3 = MagicMock()
        w3.eth.contract.return_value = self._contract
        return w3

    async def __aexit__(self, *a):
        pass


class TestConstruction:
    def test_default_chain(self):
        a = _make_adapter()
        assert a.chain_id == 8453
        assert a.adapter_type == "UNISWAP"

    def test_custom_chain(self):
        a = _make_adapter(chain_id=1)
        assert a.chain_id == 1

    def test_unsupported_chain(self):
        with pytest.raises(ValueError, match="Unsupported chain_id"):
            _make_adapter(chain_id=999)

    def test_missing_wallet(self):
        with pytest.raises(ValueError, match="wallet_address is required"):
            UniswapAdapter({"chain_id": 8453})


class TestAddLiquidity:
    @pytest.mark.asyncio
    async def test_success(self):
        adapter = _make_adapter()
        with (
            patch(
                f"{BASE_MODULE}.ensure_allowance", new_callable=AsyncMock
            ) as mock_allow,
            patch(
                f"{BASE_MODULE}.encode_call",
                new_callable=AsyncMock,
                return_value={"data": "0x"},
            ) as mock_encode,
            patch(
                f"{BASE_MODULE}.send_transaction",
                new_callable=AsyncMock,
                return_value="0xtxhash",
            ) as mock_send,
        ):
            ok, tx = await adapter.add_liquidity(
                TOKEN_A, TOKEN_B, 3000, -120, 120, 1000, 2000
            )
            assert ok is True
            assert tx == "0xtxhash"
            assert mock_allow.await_count == 2
            mock_encode.assert_awaited_once()
            mock_send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_auto_orders_tokens(self):
        adapter = _make_adapter()
        with (
            patch(f"{BASE_MODULE}.ensure_allowance", new_callable=AsyncMock),
            patch(
                f"{BASE_MODULE}.encode_call",
                new_callable=AsyncMock,
                return_value={"data": "0x"},
            ) as mock_encode,
            patch(
                f"{BASE_MODULE}.send_transaction",
                new_callable=AsyncMock,
                return_value="0xtx",
            ),
        ):
            await adapter.add_liquidity(TOKEN_B, TOKEN_A, 3000, -120, 120, 500, 1000)
            call_kwargs = mock_encode.call_args.kwargs
            params = call_kwargs["args"][0]
            assert int(params[0], 16) < int(params[1], 16)

    @pytest.mark.asyncio
    async def test_tick_rounding(self):
        adapter = _make_adapter()
        with (
            patch(f"{BASE_MODULE}.ensure_allowance", new_callable=AsyncMock),
            patch(
                f"{BASE_MODULE}.encode_call",
                new_callable=AsyncMock,
                return_value={"data": "0x"},
            ) as mock_encode,
            patch(
                f"{BASE_MODULE}.send_transaction",
                new_callable=AsyncMock,
                return_value="0xtx",
            ),
        ):
            # fee=3000 -> spacing=60. tick_lower=-175 rounds to -180, tick_upper=135 rounds to 120
            await adapter.add_liquidity(TOKEN_A, TOKEN_B, 3000, -175, 135, 1000, 2000)
            params = mock_encode.call_args.kwargs["args"][0]
            assert params[3] == -180
            assert params[4] == 120


class TestIncreaseLiquidity:
    @pytest.mark.asyncio
    async def test_success(self):
        adapter = _make_adapter()
        npm = _FakeNpm()

        with (
            patch(
                f"{BASE_MODULE}.web3_from_chain_id",
                return_value=_Web3Ctx(npm),
            ),
            patch(
                f"{BASE_MODULE}.ensure_allowance", new_callable=AsyncMock
            ) as mock_allow,
            patch(
                f"{BASE_MODULE}.encode_call",
                new_callable=AsyncMock,
                return_value={"data": "0x"},
            ),
            patch(
                f"{BASE_MODULE}.send_transaction",
                new_callable=AsyncMock,
                return_value="0xtx_inc",
            ),
        ):
            ok, tx = await adapter.increase_liquidity(42, 100, 200)
            assert ok is True
            assert tx == "0xtx_inc"
            assert mock_allow.await_count == 2


class TestRemoveLiquidity:
    @pytest.mark.asyncio
    async def test_multicall(self):
        adapter = _make_adapter()
        npm = _FakeNpm()

        with (
            patch(
                f"{BASE_MODULE}.web3_from_chain_id",
                return_value=_Web3Ctx(npm),
            ),
            patch(
                f"{BASE_MODULE}.encode_call",
                new_callable=AsyncMock,
                return_value={"data": "0x"},
            ) as mock_encode,
            patch(
                f"{BASE_MODULE}.send_transaction",
                new_callable=AsyncMock,
                return_value="0xtx_rm",
            ),
        ):
            ok, tx = await adapter.remove_liquidity(42, collect=True, burn=True)
            assert ok is True
            assert tx == "0xtx_rm"
            call_kwargs = mock_encode.call_args.kwargs
            assert call_kwargs["fn_name"] == "multicall"
            assert len(call_kwargs["args"][0]) == 3


class TestCollectFees:
    @pytest.mark.asyncio
    async def test_success(self):
        adapter = _make_adapter()
        with (
            patch(
                f"{BASE_MODULE}.encode_call",
                new_callable=AsyncMock,
                return_value={"data": "0x"},
            ) as mock_encode,
            patch(
                f"{BASE_MODULE}.send_transaction",
                new_callable=AsyncMock,
                return_value="0xtx_col",
            ),
        ):
            ok, tx = await adapter.collect_fees(42)
            assert ok is True
            assert tx == "0xtx_col"
            assert mock_encode.call_args.kwargs["fn_name"] == "collect"


class TestGetPosition:
    @pytest.mark.asyncio
    async def test_returns_position_with_token_id(self):
        adapter = _make_adapter()
        npm = _FakeNpm()

        with patch(f"{BASE_MODULE}.web3_from_chain_id", return_value=_Web3Ctx(npm)):
            ok, pos = await adapter.get_position(42)
            assert ok is True
            assert pos["token_id"] == 42
            assert pos["liquidity"] == 5000
            assert pos["fee"] == 3000


class TestGetPositions:
    @pytest.mark.asyncio
    async def test_returns_list(self):
        adapter = _make_adapter()
        npm = _FakeNpm(balance=1, token_ids=[99])

        with patch(f"{BASE_MODULE}.web3_from_chain_id", return_value=_Web3Ctx(npm)):
            ok, positions = await adapter.get_positions()
            assert ok is True
            assert len(positions) == 1
            assert positions[0]["token_id"] == 99


class TestGetUncollectedFees:
    @pytest.mark.asyncio
    async def test_simulates_collect(self):
        adapter = _make_adapter()
        npm = _FakeNpm()

        with patch(f"{BASE_MODULE}.web3_from_chain_id", return_value=_Web3Ctx(npm)):
            ok, fees = await adapter.get_uncollected_fees(42)
            assert ok is True
            assert fees["amount0"] == 1000
            assert fees["amount1"] == 2000


class TestGetPool:
    @pytest.mark.asyncio
    async def test_returns_pool_address(self):
        adapter = _make_adapter()
        pool_addr = "0x4444444444444444444444444444444444444444"
        factory = _FakeFactory(pool_addr)

        with patch(f"{BASE_MODULE}.web3_from_chain_id", return_value=_Web3Ctx(factory)):
            ok, result = await adapter.get_pool(TOKEN_A, TOKEN_B, 3000)
            assert ok is True
            assert result.lower() == pool_addr.lower()


class TestTickSpacing:
    def test_known_fees(self):
        adapter = _make_adapter()
        assert adapter._tick_spacing_for_fee(100) == 1
        assert adapter._tick_spacing_for_fee(500) == 10
        assert adapter._tick_spacing_for_fee(3000) == 60
        assert adapter._tick_spacing_for_fee(10000) == 200

    def test_unknown_fee(self):
        adapter = _make_adapter()
        with pytest.raises(ValueError, match="Unknown fee tier"):
            adapter._tick_spacing_for_fee(999)


class TestTicksForRange:
    def test_symmetric_range(self):
        lo, hi = ticks_for_range(current_tick=-200200, bps=500, spacing=10)
        assert lo < -200200 < hi
        assert lo % 10 == 0
        assert hi % 10 == 0

    def test_range_width_matches_bps(self):
        lo, hi = ticks_for_range(current_tick=0, bps=500, spacing=1)
        price_lo = tick_to_price(lo)
        price_hi = tick_to_price(hi)
        # ±5% → ratio should be ~1.05/0.95 ≈ 1.105
        assert 1.09 < price_hi / price_lo < 1.11

    def test_respects_spacing(self):
        lo, hi = ticks_for_range(current_tick=-200200, bps=500, spacing=60)
        assert lo % 60 == 0
        assert hi % 60 == 0
