from __future__ import annotations

from types import SimpleNamespace

import pytest
from eth_utils import keccak

import wayfinder_paths.adapters.aerodrome_adapter.adapter as aerodrome
from wayfinder_paths.adapters.aerodrome_adapter.adapter import AerodromeAdapter


class _FakeCall:
    def __init__(self, value):
        self._value = value

    async def call(self):
        return self._value


class _FakeFactoryFunctions:
    def __init__(self, pool_by_tick_spacing: dict[int, str]):
        self._pool_by_tick_spacing = pool_by_tick_spacing

    def getPool(self, _token_a: str, _token_b: str, tick_spacing: int):
        return _FakeCall(self._pool_by_tick_spacing.get(int(tick_spacing), "0x0"))


class _FakeVoterFunctions:
    def __init__(self, last_voted: int):
        self._last_voted = int(last_voted)

    def lastVoted(self, _token_id: int):
        return _FakeCall(self._last_voted)


class _FakeContract:
    def __init__(self, functions):
        self.functions = functions


class _FakeEth:
    def __init__(
        self,
        *,
        now_ts: int,
        voter_addr: str,
        last_voted: int,
        factory_addr: str,
        pool_by_tick_spacing: dict[int, str],
    ):
        self._now_ts = int(now_ts)
        self._voter_addr = voter_addr.lower()
        self._factory_addr = factory_addr.lower()
        self._last_voted = int(last_voted)
        self._pool_by_tick_spacing = dict(pool_by_tick_spacing)

    async def get_block(self, _tag: str):
        return {"timestamp": int(self._now_ts)}

    def contract(self, *, address: str, abi):  # noqa: ARG002
        addr = str(address).lower()
        if addr == self._voter_addr:
            return _FakeContract(_FakeVoterFunctions(self._last_voted))
        if addr == self._factory_addr:
            return _FakeContract(_FakeFactoryFunctions(self._pool_by_tick_spacing))
        raise AssertionError(f"Unexpected contract address: {address}")


class _FakeWeb3:
    def __init__(self, eth: _FakeEth):
        self.eth = eth


class _FakeWeb3Context:
    def __init__(self, web3: _FakeWeb3):
        self._web3 = web3

    async def __aenter__(self):
        return self._web3

    async def __aexit__(self, exc_type, exc, tb):  # noqa: ARG002
        return False


class TestAerodromeAdapter:
    @pytest.fixture
    def adapter(self):
        return AerodromeAdapter(
            config={"strategy_wallet": {"address": "0x" + "11" * 20}}
        )

    def test_init(self, adapter: AerodromeAdapter):
        assert adapter.adapter_type == "AERODROME"
        assert adapter.name == "aerodrome_adapter"

    def test_tick_spacing_helpers(self):
        assert AerodromeAdapter.floor_tick_to_spacing(123, 60) == 120
        assert AerodromeAdapter.ceil_tick_to_spacing(123, 60) == 180
        assert AerodromeAdapter.floor_tick_to_spacing(-123, 60) == -180
        assert AerodromeAdapter.ceil_tick_to_spacing(-123, 60) == -120
        assert AerodromeAdapter.floor_tick_to_spacing(-120, 60) == -120
        assert AerodromeAdapter.ceil_tick_to_spacing(-120, 60) == -120

    def test_q96_to_price_identity(self):
        assert AerodromeAdapter.q96_to_price_token1_per_token0(
            sqrt_price_x96=2**96, decimals0=18, decimals1=18
        ) == pytest.approx(1.0)

    def test_q96_to_price_respects_decimals(self):
        assert AerodromeAdapter.q96_to_price_token1_per_token0(
            sqrt_price_x96=2**96, decimals0=6, decimals1=18
        ) == pytest.approx(1e-12)

    def test_parse_erc721_mint_token_id_from_receipt(self):
        nft = "0x" + "22" * 20
        to_addr = "0x" + "11" * 20
        token_id = 123

        receipt = {
            "logs": [
                {
                    "address": nft,
                    "topics": [
                        keccak(text="Transfer(address,address,uint256)"),
                        bytes(32),
                        bytes.fromhex(("00" * 12) + to_addr[2:]),
                        int(token_id).to_bytes(32, "big"),
                    ],
                }
            ]
        }

        parsed = AerodromeAdapter.parse_erc721_mint_token_id_from_receipt(
            receipt, nft_address=nft, to_address=to_addr
        )
        assert parsed == token_id

    def test_parse_erc721_mint_token_id_from_receipt_raises(self):
        nft = "0x" + "22" * 20
        to_addr = "0x" + "11" * 20
        other = "0x" + "33" * 20

        receipt = {
            "logs": [
                {
                    "address": nft,
                    "topics": [
                        keccak(text="Transfer(address,address,uint256)"),
                        bytes(32),
                        bytes.fromhex(("00" * 12) + other[2:]),
                        (1).to_bytes(32, "big"),
                    ],
                }
            ]
        }

        with pytest.raises(RuntimeError, match="Unable to parse"):
            AerodromeAdapter.parse_erc721_mint_token_id_from_receipt(
                receipt, nft_address=nft, to_address=to_addr
            )

    def test_parse_ve_nft_token_id_from_create_lock_receipt(
        self, adapter: AerodromeAdapter
    ):
        to_addr = "0x" + "11" * 20
        token_id = 7
        receipt = {
            "logs": [
                {
                    "address": adapter.ve,
                    "topics": [
                        keccak(text="Transfer(address,address,uint256)"),
                        bytes(32),
                        bytes.fromhex(("00" * 12) + to_addr[2:]),
                        int(token_id).to_bytes(32, "big"),
                    ],
                }
            ]
        }
        assert (
            adapter.parse_ve_nft_token_id_from_create_lock_receipt(
                receipt, to_address=to_addr
            )
            == token_id
        )

    @pytest.mark.asyncio
    async def test_can_vote_now(self, adapter: AerodromeAdapter, monkeypatch):
        now_ts = 1_700_000_000
        epoch_start = (now_ts // aerodrome.WEEK_S) * aerodrome.WEEK_S
        last_voted = epoch_start - 1

        fake_eth = _FakeEth(
            now_ts=now_ts,
            voter_addr=adapter.voter,
            last_voted=last_voted,
            factory_addr=aerodrome.AERODROME_SLIPSTREAM_FACTORY,
            pool_by_tick_spacing={},
        )
        monkeypatch.setattr(
            aerodrome,
            "web3_from_chain_id",
            lambda _chain_id: _FakeWeb3Context(_FakeWeb3(fake_eth)),
        )

        can_vote, last, epoch, next_epoch = await adapter.can_vote_now(token_id=1)
        assert can_vote is True
        assert last == last_voted
        assert epoch == epoch_start
        assert next_epoch == epoch_start + aerodrome.WEEK_S

    @pytest.mark.asyncio
    async def test_slipstream_best_pool_for_pair(
        self, adapter: AerodromeAdapter, monkeypatch
    ):
        token_a = "0x" + "11" * 20
        token_b = "0x" + "22" * 20
        pool_low = "0x" + "aa" * 20
        pool_high = "0x" + "bb" * 20

        async def _fake_tick_spacings_for_pair(
            *, token_a: str, token_b: str
        ) -> list[int]:  # noqa: ARG001
            return [10, 60]

        async def _fake_pool_state(*, pool: str):
            liq = 100 if pool.lower() == pool_low.lower() else 200
            return SimpleNamespace(liquidity=liq)

        monkeypatch.setattr(
            adapter, "_slipstream_tick_spacings_for_pair", _fake_tick_spacings_for_pair
        )
        monkeypatch.setattr(adapter, "slipstream_pool_state", _fake_pool_state)

        fake_eth = _FakeEth(
            now_ts=1_700_000_000,
            voter_addr=adapter.voter,
            last_voted=0,
            factory_addr=aerodrome.AERODROME_SLIPSTREAM_FACTORY,
            pool_by_tick_spacing={10: pool_low, 60: pool_high},
        )
        monkeypatch.setattr(
            aerodrome,
            "web3_from_chain_id",
            lambda _chain_id: _FakeWeb3Context(_FakeWeb3(fake_eth)),
        )

        best = await adapter.slipstream_best_pool_for_pair(
            token_a=token_a, token_b=token_b
        )
        assert best.lower() == pool_high.lower()
