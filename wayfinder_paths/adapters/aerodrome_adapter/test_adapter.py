import inspect
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import wayfinder_paths.adapters.aerodrome_adapter.adapter as aerodrome_adapter_module
import wayfinder_paths.adapters.aerodrome_common as aerodrome_common_module
from wayfinder_paths.adapters.aerodrome_adapter.adapter import (
    AerodromeAdapter,
    Route,
    SugarEpoch,
    SugarPool,
    SugarReward,
)
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE

EPOCH_SPECIAL_WINDOW_SECONDS = aerodrome_common_module.EPOCH_SPECIAL_WINDOW_SECONDS
WEEK_SECONDS = aerodrome_common_module.WEEK_SECONDS

FAKE_WALLET = "0x1234567890123456789012345678901234567890"
FAKE_POOL = "0x0000000000000000000000000000000000000001"
FAKE_GAUGE = "0x0000000000000000000000000000000000000002"


@pytest.fixture
def adapter_with_signer():
    return AerodromeAdapter(
        sign_callback=AsyncMock(return_value="0xsigned"),
        wallet_address=FAKE_WALLET,
    )


def _mock_call(return_value):
    return MagicMock(call=AsyncMock(return_value=return_value))


def _web3_ctx(web3):
    @asynccontextmanager
    async def _ctx(_chain_id):
        yield web3

    return _ctx


def test_adapter_type():
    adapter = AerodromeAdapter()
    assert adapter.adapter_type == "AERODROME"


def test_constructor_is_base_only():
    adapter = AerodromeAdapter()
    assert adapter.chain_id == CHAIN_ID_BASE


@pytest.mark.parametrize(
    "method_name",
    [
        "get_pool",
        "get_gauge",
        "get_reward_contracts",
        "get_all_markets",
        "quote_add_liquidity",
        "add_liquidity",
        "quote_remove_liquidity",
        "remove_liquidity",
        "claim_pool_fees_unstaked",
        "stake_lp",
        "unstake_lp",
        "claim_gauge_rewards",
        "get_user_ve_nfts",
        "create_lock",
        "create_lock_for",
        "increase_lock_amount",
        "increase_unlock_time",
        "withdraw_lock",
        "lock_permanent",
        "unlock_permanent",
        "vote",
        "reset_vote",
        "claim_fees",
        "claim_bribes",
        "get_rebase_claimable",
        "claim_rebases",
        "claim_rebases_many",
        "get_full_user_state",
    ],
)
def test_public_methods_do_not_accept_chain_id(method_name):
    sig = inspect.signature(getattr(AerodromeAdapter, method_name))
    assert "chain_id" not in sig.parameters


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,kwargs",
    [
        (
            "add_liquidity",
            {
                "tokenA": "0x0000000000000000000000000000000000000001",
                "tokenB": "0x0000000000000000000000000000000000000002",
                "stable": False,
                "amountA_desired": 1,
                "amountB_desired": 1,
            },
        ),
        (
            "stake_lp",
            {
                "gauge": "0x0000000000000000000000000000000000000003",
                "amount": 1,
            },
        ),
        (
            "create_lock",
            {
                "amount": 1,
                "lock_duration": 1,
            },
        ),
    ],
)
async def test_require_wallet_returns_false_when_no_wallet(method, kwargs):
    adapter = AerodromeAdapter()
    ok, msg = await getattr(adapter, method)(**kwargs)
    assert ok is False
    assert msg == "wallet address not configured"


@pytest.mark.asyncio
async def test_can_vote_now_rejects_first_hour():
    adapter = AerodromeAdapter()
    mock_web3 = MagicMock()
    mock_web3.eth.get_block = AsyncMock(return_value={"timestamp": WEEK_SECONDS + 1})

    with patch.object(
        aerodrome_common_module,
        "web3_from_chain_id",
        _web3_ctx(mock_web3),
    ):
        ok, msg = await adapter._can_vote_now()

    assert ok is False
    assert "first hour" in msg.lower()


@pytest.mark.asyncio
async def test_can_vote_now_rejects_last_hour_without_token_id():
    adapter = AerodromeAdapter()
    mock_web3 = MagicMock()
    mock_web3.eth.get_block = AsyncMock(
        return_value={"timestamp": (2 * WEEK_SECONDS) - EPOCH_SPECIAL_WINDOW_SECONDS}
    )

    with patch.object(
        aerodrome_common_module,
        "web3_from_chain_id",
        _web3_ctx(mock_web3),
    ):
        ok, msg = await adapter._can_vote_now()

    assert ok is False
    assert "token_id required" in msg.lower()


@pytest.mark.asyncio
async def test_can_vote_now_allows_whitelisted_nft_in_last_hour():
    adapter = AerodromeAdapter()
    voter = MagicMock()
    voter.functions.isWhitelistedNFT = MagicMock(return_value=_mock_call(True))

    mock_web3 = MagicMock()
    mock_web3.eth.get_block = AsyncMock(
        return_value={"timestamp": (2 * WEEK_SECONDS) - EPOCH_SPECIAL_WINDOW_SECONDS}
    )
    mock_web3.eth.contract = MagicMock(return_value=voter)

    with patch.object(
        aerodrome_common_module,
        "web3_from_chain_id",
        _web3_ctx(mock_web3),
    ):
        ok, msg = await adapter._can_vote_now(token_id=123)

    assert ok is True
    assert msg == ""
    voter.functions.isWhitelistedNFT.assert_called_once_with(123)


@pytest.mark.asyncio
async def test_get_all_markets_empty_result_uses_base_chain():
    adapter = AerodromeAdapter()
    voter = MagicMock()
    voter.functions.length = MagicMock(return_value=_mock_call(0))

    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(return_value=voter)

    with patch.object(
        aerodrome_adapter_module,
        "web3_from_chain_id",
        _web3_ctx(mock_web3),
    ):
        ok, data = await adapter.get_all_markets()

    assert ok is True
    assert data["chain_id"] == CHAIN_ID_BASE
    assert data["markets"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_stake_lp_dead_gauge_returns_clean_error(adapter_with_signer):
    voter = MagicMock()
    voter.functions.isAlive = MagicMock(return_value=_mock_call(False))

    gauge_contract = MagicMock()
    gauge_contract.functions.stakingToken = MagicMock(
        side_effect=AssertionError("stakingToken should not be read for dead gauge")
    )

    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(side_effect=[voter, gauge_contract])

    with patch.object(
        aerodrome_adapter_module,
        "web3_from_chain_id",
        _web3_ctx(mock_web3),
    ):
        ok, msg = await adapter_with_signer.stake_lp(gauge=FAKE_GAUGE, amount=1)

    assert ok is False
    assert "not alive" in msg.lower()
    gauge_contract.functions.stakingToken.assert_not_called()


@pytest.mark.asyncio
async def test_claim_pool_fees_unstaked_reads_pending_claimables(adapter_with_signer):
    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(return_value=MagicMock())

    with (
        patch.object(
            aerodrome_adapter_module,
            "web3_from_chain_id",
            _web3_ctx(mock_web3),
        ),
        patch.object(
            aerodrome_adapter_module,
            "read_only_calls_multicall_or_gather",
            new=AsyncMock(return_value=(11, 22)),
        ) as mock_read,
        patch.object(
            aerodrome_adapter_module,
            "encode_call",
            new=AsyncMock(return_value={"chainId": CHAIN_ID_BASE}),
        ) as mock_encode,
        patch.object(
            aerodrome_adapter_module,
            "send_transaction",
            new=AsyncMock(return_value="0xtxhash"),
        ),
    ):
        ok, data = await adapter_with_signer.claim_pool_fees_unstaked(pool=FAKE_POOL)

    assert ok is True
    assert data == {"tx": "0xtxhash", "claimable0": 11, "claimable1": 22}
    assert mock_read.await_args.kwargs["block_identifier"] == "pending"
    assert mock_read.await_args.kwargs["chain_id"] == CHAIN_ID_BASE
    assert mock_encode.await_args.kwargs["chain_id"] == CHAIN_ID_BASE


def test_parse_sugar_epoch():
    token0 = "0x" + "11" * 20
    token1 = "0x" + "22" * 20
    lp = "0x" + "33" * 20

    epoch = AerodromeAdapter._parse_sugar_epoch(
        [
            123,
            lp,
            10,
            0,
            [(token0, 5), (token1, 7)],
            [(token1, 1)],
        ]
    )

    assert epoch.ts == 123
    assert epoch.lp.lower() == lp.lower()
    assert epoch.votes == 10
    assert epoch.emissions == 0
    assert epoch.bribes == [
        SugarReward(token=token0, amount=5),
        SugarReward(token=token1, amount=7),
    ]
    assert epoch.fees == [SugarReward(token=token1, amount=1)]


def test_parse_sugar_pool():
    token0 = "0x" + "11" * 20
    token1 = "0x" + "22" * 20
    lp = "0x" + "33" * 20
    gauge = "0x" + "44" * 20
    fee = "0x" + "55" * 20
    bribe = "0x" + "66" * 20
    factory = "0x" + "77" * 20
    emissions_token = "0x" + "88" * 20

    pool = AerodromeAdapter._parse_sugar_pool(
        [
            lp,
            "AERO/USDC",
            18,
            1000,
            0,
            0,
            0,
            token0,
            10,
            2,
            token1,
            20,
            4,
            gauge,
            500,
            True,
            fee,
            bribe,
            factory,
            123,
            emissions_token,
            30,
            5,
            7,
            11,
            999,
        ]
    )

    assert pool.lp.lower() == lp.lower()
    assert pool.symbol == "AERO/USDC"
    assert pool.gauge.lower() == gauge.lower()
    assert pool.emissions_token.lower() == emissions_token.lower()
    assert pool.is_v2 is True
    assert pool.is_cl is False
    assert pool.stable is True


@pytest.mark.asyncio
async def test_quote_best_route_picks_best_candidate(monkeypatch):
    adapter = AerodromeAdapter()
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
        amount_in=1,
        token_in=token_in,
        token_out=token_out,
        intermediates=[mid],
    )

    assert out == 100
    assert len(routes) == 2
    assert routes[0].to_token.lower() == mid.lower()
    assert routes[1].to_token.lower() == token_out.lower()


@pytest.mark.asyncio
async def test_token_price_usdc_caches_none_on_failed_quote(monkeypatch):
    adapter = AerodromeAdapter()
    token = "0x" + "11" * 20

    async def _fake_decimals(_token: str) -> int:
        return 18

    async def _fake_quote_best_route(**_kwargs):
        raise RuntimeError("no route")

    monkeypatch.setattr(adapter, "token_decimals", _fake_decimals)
    monkeypatch.setattr(adapter, "quote_best_route", _fake_quote_best_route)

    assert await adapter.token_price_usdc(token) is None

    async def _should_not_be_called(**_kwargs):
        raise AssertionError("unexpected cache miss")

    monkeypatch.setattr(adapter, "quote_best_route", _should_not_be_called)
    assert await adapter.token_price_usdc(token) is None


@pytest.mark.asyncio
async def test_list_pools_stops_on_known_pagination_revert(monkeypatch):
    adapter = AerodromeAdapter()

    pool = SugarPool(
        lp="0x" + "11" * 20,
        symbol="A/B",
        lp_decimals=18,
        lp_total_supply=100,
        pool_type=0,
        tick=0,
        sqrt_ratio=0,
        token0="0x" + "22" * 20,
        reserve0=1,
        staked0=1,
        token1="0x" + "33" * 20,
        reserve1=1,
        staked1=1,
        gauge="0x" + "44" * 20,
        gauge_liquidity=1,
        gauge_alive=True,
        fee="0x" + "55" * 20,
        bribe="0x" + "66" * 20,
        factory="0x" + "77" * 20,
        emissions_per_sec=0,
        emissions_token="0x" + "88" * 20,
        pool_fee_pips=30,
        unstaked_fee_pips=5,
        token0_fees=0,
        token1_fees=0,
        created_at=1,
    )

    calls = 0

    async def _fake_sugar_all(*, limit: int, offset: int) -> list[SugarPool]:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert limit == 500
            assert offset == 0
            return [pool]
        raise RuntimeError("execution reverted: out of bounds")

    monkeypatch.setattr(adapter, "sugar_all", _fake_sugar_all)

    pools = await adapter.list_pools()
    assert pools == [pool]
    assert calls == 2


@pytest.mark.asyncio
async def test_token_amount_usdc(monkeypatch):
    adapter = AerodromeAdapter()
    token = "0x" + "44" * 20

    async def _fake_decimals(_token: str) -> int:
        return 6

    async def _fake_price(_token: str) -> float | None:
        return 2.0

    monkeypatch.setattr(adapter, "token_decimals", _fake_decimals)
    monkeypatch.setattr(adapter, "token_price_usdc", _fake_price)

    assert await adapter.token_amount_usdc(token=token, amount_raw=0) == 0.0
    assert await adapter.token_amount_usdc(token=token, amount_raw=-1) is None
    assert await adapter.token_amount_usdc(token=token, amount_raw=1_500_000) == (
        pytest.approx(3.0)
    )


@pytest.mark.asyncio
async def test_epoch_total_incentives_usdc(monkeypatch):
    adapter = AerodromeAdapter()
    token_ok = "0x" + "11" * 20
    token_bad = "0x" + "22" * 20
    epoch = SugarEpoch(
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
        *,
        token: str,
        amount_raw: int,
    ) -> float | None:
        if token.lower() == token_ok.lower():
            return 1.0
        return None

    monkeypatch.setattr(adapter, "token_amount_usdc", _fake_token_amount_usdc)

    assert (
        await adapter.epoch_total_incentives_usdc(epoch, require_all_prices=True)
        is None
    )
    assert await adapter.epoch_total_incentives_usdc(
        epoch,
        require_all_prices=False,
    ) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_rank_pools_by_usdc_per_ve(monkeypatch):
    adapter = AerodromeAdapter()
    lp_a = "0x" + "11" * 20
    lp_b = "0x" + "22" * 20

    epoch_a_latest = SugarEpoch(
        ts=100,
        lp=lp_a,
        votes=10,
        emissions=0,
        bribes=[],
        fees=[],
    )
    epoch_a_old = SugarEpoch(
        ts=50,
        lp=lp_a,
        votes=10,
        emissions=0,
        bribes=[],
        fees=[],
    )
    epoch_b_latest = SugarEpoch(
        ts=100,
        lp=lp_b,
        votes=20,
        emissions=0,
        bribes=[],
        fees=[],
    )

    async def _fake_epochs_latest(*, limit: int, offset: int) -> list[SugarEpoch]:
        assert limit == 1000
        assert offset == 0
        return [epoch_a_latest, epoch_a_old, epoch_b_latest]

    async def _fake_total_usdc(
        epoch: SugarEpoch,
        *,
        require_all_prices: bool,
    ) -> float | None:
        assert require_all_prices is True
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
    assert ranked[0][0] > ranked[1][0]
