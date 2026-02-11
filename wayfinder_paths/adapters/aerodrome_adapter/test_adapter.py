from __future__ import annotations

import math
from types import SimpleNamespace

import pytest
from eth_utils import keccak
from web3.exceptions import Web3RPCError

import wayfinder_paths.adapters.aerodrome_adapter.adapter as aerodrome
from wayfinder_paths.adapters.aerodrome_adapter.adapter import (
    AerodromeAdapter,
    Route,
    SlipstreamRangeMetrics,
    SugarEpoch,
    SugarPool,
    SugarReward,
)


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
    def __init__(self, last_voted: int, gauges: dict[str, str] | None = None):
        self._last_voted = int(last_voted)
        self._gauges = {k.lower(): v for k, v in (gauges or {}).items()}

    def lastVoted(self, _token_id: int):
        return _FakeCall(self._last_voted)

    def gauges(self, pool: str):
        return _FakeCall(self._gauges.get(str(pool).lower(), "0x0"))


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

    def test_parse_sugar_epoch(self):
        token0 = "0x" + "11" * 20
        token1 = "0x" + "22" * 20
        lp = "0x" + "33" * 20
        row = [
            123,
            lp,
            10,
            0,
            [(token0, 5), (token1, 7)],
            [(token1, 1)],
        ]

        ep = AerodromeAdapter._parse_sugar_epoch(row)
        assert ep.ts == 123
        assert ep.lp == lp
        assert ep.votes == 10
        assert ep.emissions == 0
        assert ep.bribes == [
            SugarReward(token=token0, amount=5),
            SugarReward(token=token1, amount=7),
        ]
        assert ep.fees == [SugarReward(token=token1, amount=1)]

    @pytest.mark.asyncio
    async def test_token_amount_usdc(self, adapter: AerodromeAdapter, monkeypatch):
        token = "0x" + "44" * 20

        async def _fake_decimals(_token: str) -> int:  # noqa: ARG001
            return 6

        async def _fake_price(_token: str) -> float:  # noqa: ARG001
            return 2.0

        monkeypatch.setattr(adapter, "token_decimals", _fake_decimals)
        monkeypatch.setattr(adapter, "token_price_usdc", _fake_price)

        assert await adapter.token_amount_usdc(token=token, amount_raw=0) == 0.0
        assert await adapter.token_amount_usdc(token=token, amount_raw=-1) is None
        assert await adapter.token_amount_usdc(token=token, amount_raw=1_500_000) == (
            pytest.approx(3.0)
        )

    @pytest.mark.asyncio
    async def test_epoch_total_incentives_usdc(
        self, adapter: AerodromeAdapter, monkeypatch
    ):
        token_ok = "0x" + "11" * 20
        token_bad = "0x" + "22" * 20
        ep = SugarEpoch(
            ts=0,
            lp="0x" + "33" * 20,
            votes=1,
            emissions=0,
            bribes=[
                SugarReward(token=token_ok, amount=1),
                SugarReward(token=token_bad, amount=1),
            ],
            fees=[],
        )

        async def _fake_token_amount_usdc(
            *, token: str, amount_raw: int
        ) -> float | None:  # noqa: ARG001
            if token.lower() == token_ok.lower():
                return 1.0
            return None

        monkeypatch.setattr(adapter, "token_amount_usdc", _fake_token_amount_usdc)

        assert (
            await adapter.epoch_total_incentives_usdc(ep, require_all_prices=True)
            is None
        )
        assert await adapter.epoch_total_incentives_usdc(
            ep, require_all_prices=False
        ) == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_rank_pools_by_usdc_per_ve(
        self, adapter: AerodromeAdapter, monkeypatch
    ):
        lp_a = "0x" + "11" * 20
        lp_b = "0x" + "22" * 20

        ep_a_latest = SugarEpoch(
            ts=100, lp=lp_a, votes=10, emissions=0, bribes=[], fees=[]
        )
        ep_a_old = SugarEpoch(ts=50, lp=lp_a, votes=10, emissions=0, bribes=[], fees=[])
        ep_b_latest = SugarEpoch(
            ts=100, lp=lp_b, votes=20, emissions=0, bribes=[], fees=[]
        )

        async def _fake_epochs_latest(*, limit: int, offset: int) -> list[SugarEpoch]:  # noqa: ARG001
            return [ep_a_latest, ep_a_old, ep_b_latest]

        async def _fake_total_usdc(
            epoch: SugarEpoch, *, require_all_prices: bool
        ) -> float | None:  # noqa: ARG002
            if epoch.lp.lower() == lp_a.lower():
                return 100.0
            if epoch.lp.lower() == lp_b.lower():
                return 50.0
            return None

        monkeypatch.setattr(adapter, "sugar_epochs_latest", _fake_epochs_latest)
        monkeypatch.setattr(adapter, "epoch_total_incentives_usdc", _fake_total_usdc)

        ranked = await adapter.rank_pools_by_usdc_per_ve(top_n=10, limit=1000)
        assert len(ranked) == 2
        assert ranked[0][1].lp.lower() == lp_a.lower()
        assert ranked[1][1].lp.lower() == lp_b.lower()

        usdc_per_ve_a = ranked[0][0]
        usdc_per_ve_b = ranked[1][0]
        assert usdc_per_ve_a > usdc_per_ve_b

    @pytest.mark.asyncio
    async def test_estimate_votes_for_lock(self, adapter: AerodromeAdapter):
        assert (
            await adapter.estimate_votes_for_lock(aero_amount_raw=0, lock_duration_s=1)
            == 0
        )
        assert (
            await adapter.estimate_votes_for_lock(aero_amount_raw=1, lock_duration_s=0)
            == 0
        )
        assert (
            await adapter.estimate_votes_for_lock(
                aero_amount_raw=123, lock_duration_s=aerodrome.VE_MAXTIME_S
            )
            == 123
        )

    @pytest.mark.asyncio
    async def test_estimate_ve_apr_percent(
        self, adapter: AerodromeAdapter, monkeypatch
    ):
        async def _fake_price(token: str) -> float:  # noqa: ARG001
            return 2.0

        async def _fake_decimals(token: str) -> int:  # noqa: ARG001
            return 18

        monkeypatch.setattr(adapter, "token_price_usdc", _fake_price)
        monkeypatch.setattr(adapter, "token_decimals", _fake_decimals)

        apr = await adapter.estimate_ve_apr_percent(
            usdc_per_ve=2.0,
            votes_raw=int(1e18),
            aero_locked_raw=int(10e18),
        )
        assert apr == pytest.approx(520.0)

        assert (
            await adapter.estimate_ve_apr_percent(
                usdc_per_ve=2.0,
                votes_raw=0,
                aero_locked_raw=int(10e18),
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_quote_best_route_same_token(self, adapter: AerodromeAdapter):
        token = "0x" + "11" * 20
        routes, out = await adapter.quote_best_route(
            amount_in=123, token_in=token, token_out=token
        )
        assert routes == []
        assert out == 123

    @pytest.mark.asyncio
    async def test_choose_best_single_hop_route_prefers_stable(
        self, adapter: AerodromeAdapter, monkeypatch
    ):
        token_in = "0x" + "11" * 20
        token_out = "0x" + "22" * 20

        async def _fake_amounts_out(_amount_in: int, routes: list[Route]) -> list[int]:
            out = 200 if routes[0].stable else 100
            return [0, out]

        monkeypatch.setattr(adapter, "get_amounts_out", _fake_amounts_out)

        best = await adapter.choose_best_single_hop_route(1, token_in, token_out)
        assert isinstance(best, Route)
        assert best.stable is True

    @pytest.mark.asyncio
    async def test_quote_best_route_picks_best_candidate(
        self, adapter: AerodromeAdapter, monkeypatch
    ):
        token_in = "0x" + "11" * 20
        token_out = "0x" + "22" * 20
        mid = "0x" + "33" * 20

        async def _fake_amounts_out(amount_in: int, routes: list[Route]) -> list[int]:
            if len(routes) == 1 and routes[0].stable:
                return [amount_in, 50]
            if len(routes) == 1 and not routes[0].stable:
                return [amount_in, 40]
            if len(routes) == 2 and routes[0].stable and routes[1].stable:
                return [amount_in, 100]
            return [amount_in, 60]

        monkeypatch.setattr(adapter, "get_amounts_out", _fake_amounts_out)

        routes, out = await adapter.quote_best_route(
            amount_in=1, token_in=token_in, token_out=token_out, intermediates=[mid]
        )
        assert out == 100
        assert len(routes) == 2
        assert routes[0].to_token.lower() == mid.lower()

    @pytest.mark.asyncio
    async def test_token_price_usdc_fallback_and_caching(
        self, adapter: AerodromeAdapter, monkeypatch
    ):
        token = "0x" + "11" * 20

        async def _fake_decimals(_token: str) -> int:  # noqa: ARG001
            return 18

        async def _fake_quote_best_route(**_kwargs):  # noqa: ANN001
            raise RuntimeError("no route")

        async def _fake_slipstream_quote_to_usdc(
            *, amount_in: int, token: str
        ) -> int | None:  # noqa: ARG001
            return 1_500_000

        monkeypatch.setattr(adapter, "token_decimals", _fake_decimals)
        monkeypatch.setattr(adapter, "quote_best_route", _fake_quote_best_route)
        monkeypatch.setattr(
            adapter, "_slipstream_quote_to_usdc", _fake_slipstream_quote_to_usdc
        )

        px1 = await adapter.token_price_usdc(token)
        assert px1 == pytest.approx(1.5)

        async def _should_not_be_called(*, amount_in: int, token: str) -> int | None:  # noqa: ARG001
            raise AssertionError("unexpected cache miss")

        monkeypatch.setattr(adapter, "_slipstream_quote_to_usdc", _should_not_be_called)
        px2 = await adapter.token_price_usdc(token)
        assert px2 == pytest.approx(1.5)

        token_bad = "0x" + "22" * 20

        async def _no_slipstream(*, amount_in: int, token: str) -> int | None:  # noqa: ARG001
            return None

        monkeypatch.setattr(adapter, "_slipstream_quote_to_usdc", _no_slipstream)
        px_bad = await adapter.token_price_usdc(token_bad)
        assert math.isnan(px_bad)

    @pytest.mark.asyncio
    async def test_list_pools_paginates(self, adapter: AerodromeAdapter, monkeypatch):
        pool1 = SugarPool(
            lp="0x" + "11" * 20,
            symbol="P1",
            lp_decimals=18,
            lp_total_supply=1,
            pool_type=-1,
            tick=0,
            sqrt_ratio=0,
            token0="0x" + "01" * 20,
            reserve0=1,
            staked0=0,
            token1="0x" + "02" * 20,
            reserve1=1,
            staked1=0,
            gauge="0x" + "03" * 20,
            gauge_liquidity=1,
            gauge_alive=True,
            fee="0x" + "04" * 20,
            bribe="0x" + "05" * 20,
            factory="0x" + "06" * 20,
            emissions_per_sec=1,
            emissions_token="0x" + "07" * 20,
            pool_fee_pips=0,
            unstaked_fee_pips=0,
            token0_fees=0,
            token1_fees=0,
            created_at=0,
        )
        pool2 = SugarPool(**{**pool1.__dict__, "lp": "0x" + "22" * 20, "symbol": "P2"})
        pool3 = SugarPool(**{**pool1.__dict__, "lp": "0x" + "33" * 20, "symbol": "P3"})

        calls: list[tuple[int, int]] = []

        async def _fake_sugar_all(*, limit: int, offset: int) -> list[SugarPool]:
            calls.append((int(limit), int(offset)))
            if offset == 0:
                return [pool1, pool2]
            if offset == 2:
                return [pool3]
            raise RuntimeError("revert")

        monkeypatch.setattr(adapter, "sugar_all", _fake_sugar_all)

        pools = await adapter.list_pools(page_size=2)
        assert pools == [pool1, pool2, pool3]
        assert calls[:2] == [(2, 0), (2, 2)]

        pools_limited = await adapter.list_pools(page_size=2, max_pools=2)
        assert pools_limited == [pool1, pool2]

    @pytest.mark.asyncio
    async def test_list_pools_raises_unexpected_error(
        self, adapter: AerodromeAdapter, monkeypatch
    ):
        async def _fake_sugar_all(*, limit: int, offset: int) -> list[SugarPool]:  # noqa: ARG001
            raise RuntimeError("provider timeout")

        monkeypatch.setattr(adapter, "sugar_all", _fake_sugar_all)
        with pytest.raises(RuntimeError, match="provider timeout"):
            await adapter.list_pools(page_size=2)

    @pytest.mark.asyncio
    async def test_rank_v2_pools_by_emissions_apr(
        self, adapter: AerodromeAdapter, monkeypatch
    ):
        def _make_pool(*, lp: str, emissions_per_sec: int) -> SugarPool:
            return SugarPool(
                lp=lp,
                symbol="P",
                lp_decimals=18,
                lp_total_supply=100,
                pool_type=-1,
                tick=0,
                sqrt_ratio=0,
                token0="0x" + "01" * 20,
                reserve0=100,
                staked0=0,
                token1="0x" + "02" * 20,
                reserve1=100,
                staked1=0,
                gauge="0x" + "03" * 20,
                gauge_liquidity=10,
                gauge_alive=True,
                fee="0x" + "04" * 20,
                bribe="0x" + "05" * 20,
                factory="0x" + "06" * 20,
                emissions_per_sec=int(emissions_per_sec),
                emissions_token="0x" + "07" * 20,
                pool_fee_pips=0,
                unstaked_fee_pips=0,
                token0_fees=0,
                token1_fees=0,
                created_at=0,
            )

        pool_a = _make_pool(lp="0x" + "11" * 20, emissions_per_sec=1000)
        pool_b = _make_pool(lp="0x" + "22" * 20, emissions_per_sec=500)
        pool_c = _make_pool(lp="0x" + "33" * 20, emissions_per_sec=100)
        pool_invalid = SugarPool(
            **{**pool_a.__dict__, "lp": "0x" + "44" * 20, "gauge_alive": False}
        )

        async def _fake_list_pools(*, page_size: int) -> list[SugarPool]:  # noqa: ARG001
            return [pool_c, pool_invalid, pool_b, pool_a]

        async def _fake_apr(pool: SugarPool) -> float | None:
            if pool.lp.lower() == pool_a.lp.lower():
                return 0.1
            if pool.lp.lower() == pool_b.lp.lower():
                return 0.2
            if pool.lp.lower() == pool_c.lp.lower():
                return 100.0
            return None

        monkeypatch.setattr(adapter, "list_pools", _fake_list_pools)
        monkeypatch.setattr(adapter, "v2_emissions_apr", _fake_apr)

        ranked = await adapter.rank_v2_pools_by_emissions_apr(
            top_n=10, candidate_count=2, page_size=500
        )
        # candidate_count=2 should ignore pool_c even though its APR is huge.
        assert [p.lp.lower() for _apr, p in ranked] == [
            pool_b.lp.lower(),
            pool_a.lp.lower(),
        ]

    @pytest.mark.asyncio
    async def test_slipstream_fee_apr_percent(
        self, adapter: AerodromeAdapter, monkeypatch
    ):
        token0 = "0x" + "11" * 20
        token1 = "0x" + "22" * 20
        metrics = SlipstreamRangeMetrics(
            pool="0x" + "33" * 20,
            token0=token0,
            token1=token1,
            tick_lower=-60,
            tick_upper=60,
            current_tick=0,
            in_range=True,
            sqrt_price_x96=2**96,
            price_token1_per_token0=1.0,
            liquidity_total=1,
            liquidity_position=1,
            share_of_active_liquidity=0.1,
            amount0_now=1_000_000,
            amount1_now=2_000_000,
            fee_pips=3000,
            unstaked_fee_pips=0,
            effective_fee_fraction_for_unstaked=0.003,
        )

        async def _fake_price(token: str) -> float:
            return 1.0 if token.lower() == token0.lower() else 2.0

        async def _fake_decimals(_token: str) -> int:  # noqa: ARG001
            return 6

        monkeypatch.setattr(adapter, "token_price_usdc", _fake_price)
        monkeypatch.setattr(adapter, "token_decimals", _fake_decimals)

        apr = await adapter.slipstream_fee_apr_percent(
            metrics=metrics,
            volume_usdc_per_day=10.0,
            expected_in_range_fraction=0.5,
        )
        assert apr == pytest.approx(10.95, abs=1e-9)

        apr_out = await adapter.slipstream_fee_apr_percent(
            metrics=SlipstreamRangeMetrics(**{**metrics.__dict__, "in_range": False}),
            volume_usdc_per_day=10.0,
            expected_in_range_fraction=1.0,
        )
        assert apr_out == 0.0

    @pytest.mark.asyncio
    async def test_get_logs_bounded_reduces_chunk_and_truncates(self):
        class _FakeEthLogs:
            def __init__(self):
                self.calls: list[dict[str, object]] = []

            async def get_logs(self, params: dict[str, object]):  # noqa: ANN001
                self.calls.append(params)
                from_block = int(params["fromBlock"])  # type: ignore[arg-type]
                to_block = int(params["toBlock"])  # type: ignore[arg-type]
                span = to_block - from_block + 1
                if span > 3:
                    raise Web3RPCError("too many results")
                return [
                    {"blockNumber": bn, "logIndex": 0}
                    for bn in range(from_block, to_block + 1)
                ]

        class _FakeWeb3Logs:
            def __init__(self):
                self.eth = _FakeEthLogs()

        web3 = _FakeWeb3Logs()
        logs = await AerodromeAdapter._get_logs_bounded(
            web3,
            from_block=0,
            to_block=9,
            address="0x" + "11" * 20,
            topics=["0x0"],
            max_logs=5,
            initial_chunk_size=8,
        )
        assert len(logs) == 5
        block_numbers = sorted(int(lg["blockNumber"]) for lg in logs)
        assert block_numbers == [5, 6, 7, 8, 9]
        assert len(web3.eth.calls) >= 2

    @pytest.mark.asyncio
    async def test_read_helpers_use_web3(self, adapter: AerodromeAdapter, monkeypatch):
        token_a = "0x" + "aa" * 20
        token_b = "0x" + "bb" * 20
        factory_addr = "0x" + "99" * 20
        pool_addr = "0x" + "88" * 20
        gauge_addr = "0x" + "77" * 20

        amount_in = 123
        route = Route(token_a, token_b, stable=False)
        expected_routes = [route.as_tuple()]

        class _RouterFunctions:
            def defaultFactory(self):
                return _FakeCall(factory_addr)

            def getAmountsOut(self, amount: int, routes):  # noqa: ANN001
                assert int(amount) == amount_in
                assert routes == expected_routes
                return _FakeCall([0, 1000])

        class _PoolFactoryFunctions:
            def getPool(self, _a: str, _b: str, stable: bool):
                assert stable is True
                return _FakeCall(pool_addr)

        voter_functions = _FakeVoterFunctions(
            last_voted=0,
            gauges={pool_addr: gauge_addr},
        )

        contracts = {
            str(adapter.router).lower(): _FakeContract(_RouterFunctions()),
            str(factory_addr).lower(): _FakeContract(_PoolFactoryFunctions()),
            str(adapter.voter).lower(): _FakeContract(voter_functions),
        }

        class _FakeEthRead:
            def contract(self, *, address: str, abi):  # noqa: ARG002
                addr = str(address).lower()
                if addr not in contracts:
                    raise AssertionError(f"Unexpected contract address: {address}")
                return contracts[addr]

        monkeypatch.setattr(
            aerodrome,
            "web3_from_chain_id",
            lambda _chain_id: _FakeWeb3Context(_FakeWeb3(_FakeEthRead())),
        )

        factory = await adapter.default_factory()
        assert factory.lower() == factory_addr.lower()

        pool = await adapter.get_pool(token_a, token_b, stable=True)
        assert pool.lower() == pool_addr.lower()

        gauge = await adapter.gauge_for_pool(pool_addr)
        assert gauge.lower() == gauge_addr.lower()

        amounts = await adapter.get_amounts_out(amount_in, [route])
        assert amounts == [0, 1000]

    @pytest.mark.asyncio
    async def test_token_decimals_and_symbol_cache(
        self, adapter: AerodromeAdapter, monkeypatch
    ):
        token = "0x" + "11" * 20

        class _TokenFunctions:
            def decimals(self):
                return _FakeCall(6)

            def symbol(self):
                return _FakeCall("TKN")

        contracts = {str(token).lower(): _FakeContract(_TokenFunctions())}

        class _FakeEthToken:
            def contract(self, *, address: str, abi):  # noqa: ARG002
                addr = str(address).lower()
                if addr not in contracts:
                    raise AssertionError(f"Unexpected contract address: {address}")
                return contracts[addr]

        monkeypatch.setattr(
            aerodrome,
            "web3_from_chain_id",
            lambda _chain_id: _FakeWeb3Context(_FakeWeb3(_FakeEthToken())),
        )
        d1 = await adapter.token_decimals(token)
        assert d1 == 6

        monkeypatch.setattr(
            aerodrome,
            "web3_from_chain_id",
            lambda _chain_id: (_ for _ in ()).throw(AssertionError("cache miss")),
        )
        d2 = await adapter.token_decimals(token)
        assert d2 == 6

        # Re-enable for symbol first call.
        monkeypatch.setattr(
            aerodrome,
            "web3_from_chain_id",
            lambda _chain_id: _FakeWeb3Context(_FakeWeb3(_FakeEthToken())),
        )
        s1 = await adapter.token_symbol(token)
        assert s1 == "TKN"

        monkeypatch.setattr(
            aerodrome,
            "web3_from_chain_id",
            lambda _chain_id: (_ for _ in ()).throw(AssertionError("cache miss")),
        )
        s2 = await adapter.token_symbol(token)
        assert s2 == "TKN"

    def test_liquidity_math_branches(self):
        q96 = 2**96
        a = 1 * q96
        b = 2 * q96
        amount0 = 1_000
        amount1 = 2_000

        l_x_le_a = AerodromeAdapter._liquidity_for_amounts(
            sqrt_ratio_x96=a - 1,
            sqrt_ratio_a_x96=a,
            sqrt_ratio_b_x96=b,
            amount0=amount0,
            amount1=amount1,
        )
        assert l_x_le_a == AerodromeAdapter._liquidity_for_amount0(
            sqrt_ratio_a_x96=a,
            sqrt_ratio_b_x96=b,
            amount0=amount0,
        )

        l_x_ge_b = AerodromeAdapter._liquidity_for_amounts(
            sqrt_ratio_x96=b + 1,
            sqrt_ratio_a_x96=a,
            sqrt_ratio_b_x96=b,
            amount0=amount0,
            amount1=amount1,
        )
        assert l_x_ge_b == AerodromeAdapter._liquidity_for_amount1(
            sqrt_ratio_a_x96=a,
            sqrt_ratio_b_x96=b,
            amount1=amount1,
        )

        l_between = AerodromeAdapter._liquidity_for_amounts(
            sqrt_ratio_x96=(a + b) // 2,
            sqrt_ratio_a_x96=a,
            sqrt_ratio_b_x96=b,
            amount0=amount0,
            amount1=amount1,
        )
        l0 = AerodromeAdapter._liquidity_for_amount0(
            sqrt_ratio_a_x96=(a + b) // 2,
            sqrt_ratio_b_x96=b,
            amount0=amount0,
        )
        l1 = AerodromeAdapter._liquidity_for_amount1(
            sqrt_ratio_a_x96=a,
            sqrt_ratio_b_x96=(a + b) // 2,
            amount1=amount1,
        )
        assert l_between == min(l0, l1)

        amt0, amt1 = AerodromeAdapter._amounts_for_liquidity(
            sqrt_ratio_x96=a - 1,
            sqrt_ratio_a_x96=a,
            sqrt_ratio_b_x96=b,
            liquidity=1_000,
        )
        assert amt0 > 0
        assert amt1 == 0

        amt0, amt1 = AerodromeAdapter._amounts_for_liquidity(
            sqrt_ratio_x96=b + 1,
            sqrt_ratio_a_x96=a,
            sqrt_ratio_b_x96=b,
            liquidity=1_000,
        )
        assert amt0 == 0
        assert amt1 > 0

        amt0, amt1 = AerodromeAdapter._amounts_for_liquidity(
            sqrt_ratio_x96=(a + b) // 2,
            sqrt_ratio_a_x96=a,
            sqrt_ratio_b_x96=b,
            liquidity=1_000,
        )
        assert amt0 > 0
        assert amt1 > 0

        with pytest.raises(ZeroDivisionError, match="denominator is zero"):
            AerodromeAdapter._mul_div(1, 2, 0)

    @pytest.mark.asyncio
    async def test_tx_helpers_build_expected_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async def _sign(_tx: dict) -> bytes:
            return b""

        wallet = "0x" + "11" * 20
        adapter = AerodromeAdapter(
            config={"strategy_wallet": {"address": wallet}},
            strategy_wallet_signing_callback=_sign,
        )

        calls: dict[str, list[object]] = {"allowances": [], "encode": [], "send": []}

        async def _fake_ensure_allowance(**kwargs):  # noqa: ANN001
            calls["allowances"].append(kwargs)
            return True, {}

        async def _fake_encode_call(**kwargs):  # noqa: ANN001
            calls["encode"].append(kwargs)
            return {"tx": True, **kwargs}

        async def _fake_send_transaction(tx: dict, cb, wait_for_receipt: bool = True):  # noqa: ARG002
            calls["send"].append((tx, wait_for_receipt))
            return "0x" + "aa" * 32

        async def _fake_wait_for_receipt(_chain_id: int, _tx_hash: str) -> dict:
            return {"status": 1, "logs": []}

        monkeypatch.setattr(aerodrome, "ensure_allowance", _fake_ensure_allowance)
        monkeypatch.setattr(aerodrome, "encode_call", _fake_encode_call)
        monkeypatch.setattr(aerodrome, "send_transaction", _fake_send_transaction)
        monkeypatch.setattr(
            aerodrome, "wait_for_transaction_receipt", _fake_wait_for_receipt
        )

        token_in = "0x" + "22" * 20
        token_out = "0x" + "33" * 20
        pool = "0x" + "44" * 20
        gauge = "0x" + "55" * 20

        route = Route(token_in, token_out, stable=True)

        async def _fake_choose_best(*_args, **_kwargs):  # noqa: ANN001
            return route

        async def _fake_amounts_out(_amount_in: int, _routes: list[Route]) -> list[int]:
            return [0, 1000]

        monkeypatch.setattr(adapter, "choose_best_single_hop_route", _fake_choose_best)
        monkeypatch.setattr(adapter, "get_amounts_out", _fake_amounts_out)

        tx_hash, used_route, out_min = await adapter.swap_exact_tokens_for_tokens(
            token_in=token_in,
            token_out=token_out,
            amount_in=10,
            slippage_bps=100,
            deadline=123,
        )
        assert tx_hash
        assert used_route == route
        assert out_min == 990

        assert calls["allowances"]
        assert calls["encode"]
        assert calls["send"]

        calls = {"allowances": [], "encode": [], "send": []}

        async def _fake_quote_best(**_kwargs):  # noqa: ANN001
            return ([route], 2000)

        monkeypatch.setattr(adapter, "quote_best_route", _fake_quote_best)

        (
            tx_hash,
            routes,
            out_min,
        ) = await adapter.swap_exact_tokens_for_tokens_best_route(
            token_in=token_in,
            token_out=token_out,
            amount_in=10,
            slippage_bps=100,
            deadline=123,
        )
        assert tx_hash
        assert routes == [route]
        assert out_min == 1980

        tx_hash = await adapter.add_liquidity(
            token_a=token_in,
            token_b=token_out,
            stable=False,
            amount_a_desired=1,
            amount_b_desired=2,
            deadline=123,
        )
        assert tx_hash

        tx_hash, receipt = await adapter.create_lock(
            aero_token=token_in,
            amount=1,
            lock_duration_s=60,
            wait_for_receipt=True,
        )
        assert tx_hash
        assert receipt == {"status": 1, "logs": []}

        tx_hash, receipt = await adapter.create_lock(
            aero_token=token_in,
            amount=1,
            lock_duration_s=60,
            wait_for_receipt=False,
        )
        assert tx_hash
        assert receipt is None

        vote_tx = await adapter.vote(token_id=1, pools=[pool], weights=[10_000])
        assert vote_tx

        dep_tx = await adapter.deposit_gauge(gauge=gauge, lp_token=pool, amount=1)
        assert dep_tx

        async def _fake_get_token_balance(**kwargs):  # noqa: ANN001
            assert kwargs["wallet_address"].lower() == wallet.lower()
            return 123

        monkeypatch.setattr(aerodrome, "get_token_balance", _fake_get_token_balance)
        assert await adapter.lp_balance(pool) == 123

    def test_validate_slippage_bps(self):
        assert AerodromeAdapter._validate_slippage_bps(0) == 0
        assert AerodromeAdapter._validate_slippage_bps(9999) == 9999
        with pytest.raises(ValueError, match="slippage_bps"):
            AerodromeAdapter._validate_slippage_bps(-1)
        with pytest.raises(ValueError, match="slippage_bps"):
            AerodromeAdapter._validate_slippage_bps(10_000)

    @pytest.mark.asyncio
    async def test_get_full_user_state_account_validation(self):
        adapter = AerodromeAdapter(config={})
        ok, err = await adapter.get_full_user_state()
        assert ok is False
        assert "account is required" in str(err)

        ok, err = await adapter.get_full_user_state(account="not-an-address")
        assert ok is False
        assert "invalid account address" in str(err)

    @pytest.mark.asyncio
    async def test_slipstream_tx_helpers(self, monkeypatch: pytest.MonkeyPatch):
        async def _sign(_tx: dict) -> bytes:
            return b""

        wallet = "0x" + "11" * 20
        adapter = AerodromeAdapter(
            config={"strategy_wallet": {"address": wallet}},
            strategy_wallet_signing_callback=_sign,
        )

        token0 = "0x" + "22" * 20
        token1 = "0x" + "33" * 20
        pool = "0x" + "44" * 20
        gauge = "0x" + "55" * 20
        recipient = "0x" + "66" * 20

        async def _fake_pool_state(*, pool: str):  # noqa: ARG001
            return SimpleNamespace(token0=token0, token1=token1, tick_spacing=60)

        async def _fake_ensure_allowance(**_kwargs):  # noqa: ANN001
            return True, {}

        async def _fake_encode_call(**_kwargs):  # noqa: ANN001
            return {"tx": True}

        async def _fake_send_transaction(_tx: dict, _cb, wait_for_receipt: bool = True):  # noqa: ARG002
            return "0x" + "aa" * 32

        token_id = 7
        receipt = {
            "logs": [
                {
                    "address": aerodrome.AERODROME_SLIPSTREAM_NFPM,
                    "topics": [
                        keccak(text="Transfer(address,address,uint256)"),
                        bytes(32),
                        bytes.fromhex(("00" * 12) + recipient[2:]),
                        int(token_id).to_bytes(32, "big"),
                    ],
                }
            ]
        }

        async def _fake_wait_for_receipt(_chain_id: int, _tx_hash: str) -> dict:
            return receipt

        monkeypatch.setattr(adapter, "slipstream_pool_state", _fake_pool_state)
        monkeypatch.setattr(aerodrome, "ensure_allowance", _fake_ensure_allowance)
        monkeypatch.setattr(aerodrome, "encode_call", _fake_encode_call)
        monkeypatch.setattr(aerodrome, "send_transaction", _fake_send_transaction)
        monkeypatch.setattr(
            aerodrome, "wait_for_transaction_receipt", _fake_wait_for_receipt
        )

        tx_hash, minted_id, minted_receipt = await adapter.slipstream_mint_position(
            pool=pool,
            tick_lower=-120,
            tick_upper=120,
            amount0_desired=1,
            amount1_desired=2,
            recipient=recipient,
            deadline=123,
            wait_for_receipt=True,
        )
        assert tx_hash
        assert minted_id == token_id
        assert minted_receipt == receipt

        tx_hash, minted_id, minted_receipt = await adapter.slipstream_mint_position(
            pool=pool,
            tick_lower=-120,
            tick_upper=120,
            amount0_desired=1,
            amount1_desired=2,
            recipient=recipient,
            deadline=123,
            wait_for_receipt=False,
        )
        assert tx_hash
        assert minted_id is None
        assert minted_receipt is None

        approve_tx = await adapter.slipstream_approve_position(
            spender=gauge, token_id=token_id
        )
        assert approve_tx

        seen: list[tuple[str, int]] = []

        async def _fake_approve(*, spender: str, token_id: int) -> str:
            seen.append((spender, token_id))
            return "0x" + "bb" * 32

        monkeypatch.setattr(adapter, "slipstream_approve_position", _fake_approve)
        dep_tx = await adapter.slipstream_gauge_deposit(
            gauge=gauge, token_id=token_id, approve=True
        )
        assert dep_tx
        assert seen == [(aerodrome.to_checksum_address(gauge), token_id)]

        seen.clear()
        dep_tx = await adapter.slipstream_gauge_deposit(
            gauge=gauge, token_id=token_id, approve=False
        )
        assert dep_tx
        assert seen == []

    def test_require_wallet_errors(self):
        a1 = AerodromeAdapter(config={})
        with pytest.raises(ValueError, match="strategy_wallet.address"):
            a1._require_wallet()

        a2 = AerodromeAdapter(config={"strategy_wallet": {"address": "0x" + "11" * 20}})
        with pytest.raises(ValueError, match="strategy_wallet_signing_callback"):
            a2._require_wallet()

    @pytest.mark.asyncio
    async def test_slipstream_volume_and_prob_validation(
        self, adapter: AerodromeAdapter
    ):
        with pytest.raises(ValueError, match="lookback_blocks must be > 0"):
            await adapter.slipstream_volume_usdc_per_day(
                pool="0x" + "11" * 20, lookback_blocks=0
            )

        with pytest.raises(ValueError, match="max_logs must be > 0"):
            await adapter.slipstream_volume_usdc_per_day(
                pool="0x" + "11" * 20, max_logs=0
            )

        assert (
            await adapter.slipstream_prob_in_range_week(
                pool="0x" + "11" * 20,
                tick_lower=-10,
                tick_upper=10,
                sigma_annual=0.0,
            )
            is None
        )
