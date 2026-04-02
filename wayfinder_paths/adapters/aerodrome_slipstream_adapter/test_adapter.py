import inspect
import math
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import wayfinder_paths.adapters.aerodrome_common as aerodrome_common_module
import wayfinder_paths.adapters.aerodrome_slipstream_adapter.adapter as slipstream_module
from wayfinder_paths.adapters.aerodrome_slipstream_adapter.adapter import (
    AerodromeSlipstreamAdapter,
)
from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.utils.uniswap_v3_math import (
    amounts_for_liq_inrange,
    liq_for_amounts,
    slippage_min,
    sqrt_price_x96_from_tick,
    tick_to_price_decimal,
)

EPOCH_SPECIAL_WINDOW_SECONDS = aerodrome_common_module.EPOCH_SPECIAL_WINDOW_SECONDS
WEEK_SECONDS = aerodrome_common_module.WEEK_SECONDS

FAKE_WALLET = "0x1234567890123456789012345678901234567890"
FAKE_POOL = "0x0000000000000000000000000000000000000001"
FAKE_GAUGE = "0x0000000000000000000000000000000000000002"
FAKE_NPM = "0x0000000000000000000000000000000000000003"


@pytest.fixture
def adapter_with_signer():
    return AerodromeSlipstreamAdapter(
        config={"deployments": ("initial",)},
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
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ("initial",)})
    assert adapter.adapter_type == "AERODROME_SLIPSTREAM"


def test_constructor_is_base_only():
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ("initial",)})
    assert adapter.chain_id == CHAIN_ID_BASE


@pytest.mark.parametrize(
    "method_name",
    [
        "find_pools",
        "get_pool",
        "get_gauge",
        "get_reward_contracts",
        "get_all_markets",
        "mint_position",
        "increase_liquidity",
        "decrease_liquidity",
        "collect_fees",
        "burn_position",
        "stake_position",
        "unstake_position",
        "claim_position_rewards",
        "claim_gauge_rewards",
        "get_pos",
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
        "get_vote_claimables",
        "slipstream_best_pool_for_pair",
        "slipstream_pool_state",
        "slipstream_range_metrics",
        "slipstream_volume_usdc_per_day",
        "slipstream_fee_apr_percent",
        "slipstream_sigma_annual_from_swaps",
        "slipstream_prob_in_range_week",
    ],
)
def test_public_methods_do_not_accept_chain_id(method_name):
    sig = inspect.signature(getattr(AerodromeSlipstreamAdapter, method_name))
    assert "chain_id" not in sig.parameters


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,kwargs",
    [
        (
            "mint_position",
            {
                "token0": "0x0000000000000000000000000000000000000001",
                "token1": "0x0000000000000000000000000000000000000002",
                "tick_spacing": 200,
                "tick_lower": -200,
                "tick_upper": 200,
                "amount0_desired": 1,
                "amount1_desired": 1,
            },
        ),
        (
            "stake_position",
            {
                "gauge": FAKE_GAUGE,
                "token_id": 1,
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
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ("initial",)})
    ok, msg = await getattr(adapter, method)(**kwargs)
    assert ok is False
    assert msg == "wallet address not configured"


@pytest.mark.asyncio
async def test_can_vote_now_rejects_first_hour():
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ("initial",)})
    mock_web3 = MagicMock()
    mock_web3.eth.get_block = AsyncMock(return_value={"timestamp": WEEK_SECONDS + 1})

    with patch.object(
        aerodrome_common_module, "web3_from_chain_id", _web3_ctx(mock_web3)
    ):
        ok, msg = await adapter._can_vote_now()

    assert ok is False
    assert "first hour" in msg.lower()


@pytest.mark.asyncio
async def test_can_vote_now_rejects_last_hour_without_token_id():
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ("initial",)})
    mock_web3 = MagicMock()
    mock_web3.eth.get_block = AsyncMock(
        return_value={"timestamp": (2 * WEEK_SECONDS) - EPOCH_SPECIAL_WINDOW_SECONDS}
    )

    with patch.object(
        aerodrome_common_module, "web3_from_chain_id", _web3_ctx(mock_web3)
    ):
        ok, msg = await adapter._can_vote_now()

    assert ok is False
    assert "token_id required" in msg.lower()


@pytest.mark.asyncio
async def test_can_vote_now_allows_whitelisted_nft_in_last_hour():
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ("initial",)})
    voter = MagicMock()
    voter.functions.isWhitelistedNFT = MagicMock(return_value=_mock_call(True))

    mock_web3 = MagicMock()
    mock_web3.eth.get_block = AsyncMock(
        return_value={"timestamp": (2 * WEEK_SECONDS) - EPOCH_SPECIAL_WINDOW_SECONDS}
    )
    mock_web3.eth.contract = MagicMock(return_value=voter)

    with patch.object(
        aerodrome_common_module, "web3_from_chain_id", _web3_ctx(mock_web3)
    ):
        ok, msg = await adapter._can_vote_now(token_id=123)

    assert ok is True
    assert msg == ""
    voter.functions.isWhitelistedNFT.assert_called_once_with(123)


@pytest.mark.asyncio
async def test_get_all_markets_empty_result_uses_base_chain():
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ("initial",)})
    factory = MagicMock()
    factory.functions.allPoolsLength = MagicMock(return_value=_mock_call(0))

    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(return_value=factory)

    with patch.object(slipstream_module, "web3_from_chain_id", _web3_ctx(mock_web3)):
        ok, data = await adapter.get_all_markets()

    assert ok is True
    assert data["chain_id"] == CHAIN_ID_BASE
    assert data["chain_name"] == "base"
    assert data["markets"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_token_price_usdc_uses_client_and_cache():
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ("initial",)})
    token = "0x0000000000000000000000000000000000000007"

    with patch.object(
        slipstream_module.TOKEN_CLIENT,
        "get_token_details",
        new=AsyncMock(return_value={"current_price": 1.23}),
    ) as mock_get_token_details:
        price1 = await adapter.token_price_usdc(token)
        price2 = await adapter.token_price_usdc(token)

    assert price1 == pytest.approx(1.23)
    assert price2 == pytest.approx(1.23)
    assert mock_get_token_details.await_count == 1
    assert mock_get_token_details.await_args_list[0].args[0] == f"base_{token}"


@pytest.mark.asyncio
async def test_slipstream_best_pool_for_pair_prefers_highest_liquidity():
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ("initial",)})
    matches = [
        {
            "deployment_variant": "initial",
            "pool": FAKE_POOL,
        },
        {
            "deployment_variant": "initial",
            "pool": "0x0000000000000000000000000000000000000004",
        },
    ]
    mock_web3 = MagicMock()

    with (
        patch.object(slipstream_module, "web3_from_chain_id", _web3_ctx(mock_web3)),
        patch.object(
            adapter,
            "find_pools",
            new=AsyncMock(return_value=(True, matches)),
        ),
        patch.object(
            adapter,
            "_read_market",
            new=AsyncMock(
                side_effect=[
                    {"pool": FAKE_POOL, "liquidity": 100},
                    {
                        "pool": "0x0000000000000000000000000000000000000004",
                        "liquidity": 200,
                    },
                ]
            ),
        ),
    ):
        ok, data = await adapter.slipstream_best_pool_for_pair(
            tokenA="0x0000000000000000000000000000000000000001",
            tokenB="0x0000000000000000000000000000000000000002",
        )

    assert ok is True
    assert data["pool"] == "0x0000000000000000000000000000000000000004"


@pytest.mark.asyncio
async def test_get_gauge_returns_consistent_pool_gauge():
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ("initial",)})
    pool_contract = MagicMock()
    pool_contract.functions.gauge = MagicMock(return_value=_mock_call(FAKE_GAUGE))
    voter = MagicMock()
    voter.functions.gauges = MagicMock(return_value=_mock_call(FAKE_GAUGE))

    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(side_effect=[pool_contract, voter])

    with patch.object(slipstream_module, "web3_from_chain_id", _web3_ctx(mock_web3)):
        ok, gauge = await adapter.get_gauge(pool=FAKE_POOL)

    assert ok is True
    assert gauge == FAKE_GAUGE


@pytest.mark.asyncio
async def test_slipstream_pool_state_reads_expected_fields():
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ("initial",)})
    deployment = adapter._deployment("initial")
    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(return_value=MagicMock())

    with (
        patch.object(slipstream_module, "web3_from_chain_id", _web3_ctx(mock_web3)),
        patch.object(
            slipstream_module,
            "read_only_calls_multicall_or_gather",
            new=AsyncMock(
                return_value=(
                    "0x0000000000000000000000000000000000000011",
                    "0x0000000000000000000000000000000000000012",
                    deployment["nonfungible_position_manager"],
                    60,
                    (2**96, 0),
                    999,
                    3000,
                    0,
                )
            ),
        ),
        patch.object(
            adapter,
            "_token_decimals",
            new=AsyncMock(side_effect=[6, 6]),
        ),
    ):
        ok, data = await adapter.slipstream_pool_state(pool=FAKE_POOL)

    assert ok is True
    assert data["pool"] == FAKE_POOL
    assert data["deployment_variant"] == "initial"
    assert data["tick_spacing"] == 60
    assert data["liquidity"] == 999
    assert data["fee_pips"] == 3000
    assert data["unstaked_fee_pips"] == 0
    assert data["price_token1_per_token0"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_slipstream_range_metrics_uses_pool_state():
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ("initial",)})
    sqrt_price_x96 = sqrt_price_x96_from_tick(0)
    tick_lower = -120
    tick_upper = 120
    amount0_raw = 1_000_000
    amount1_raw = 1_000_000

    with patch.object(
        adapter,
        "slipstream_pool_state",
        new=AsyncMock(
            return_value=(
                True,
                {
                    "deployment_variant": "initial",
                    "pool": FAKE_POOL,
                    "position_manager": FAKE_NPM,
                    "token0": "0x0000000000000000000000000000000000000011",
                    "token1": "0x0000000000000000000000000000000000000012",
                    "sqrt_price_x96": sqrt_price_x96,
                    "tick": 0,
                    "liquidity": 10_000_000,
                    "fee_pips": 3000,
                    "unstaked_fee_pips": 0,
                    "price_token1_per_token0": 1.0,
                },
            )
        ),
    ):
        ok, metrics = await adapter.slipstream_range_metrics(
            pool=FAKE_POOL,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            amount0_raw=amount0_raw,
            amount1_raw=amount1_raw,
        )

    sqrt_lower = sqrt_price_x96_from_tick(tick_lower)
    sqrt_upper = sqrt_price_x96_from_tick(tick_upper)
    expected_liquidity = liq_for_amounts(
        sqrt_price_x96,
        sqrt_lower,
        sqrt_upper,
        amount0_raw,
        amount1_raw,
    )
    expected0, expected1 = amounts_for_liq_inrange(
        sqrt_price_x96,
        sqrt_lower,
        sqrt_upper,
        expected_liquidity,
    )

    assert ok is True
    assert metrics["in_range"] is True
    assert metrics["liquidity_position"] == expected_liquidity
    assert metrics["amount0_now"] == expected0
    assert metrics["amount1_now"] == expected1
    assert metrics["share_of_active_liquidity"] == pytest.approx(
        expected_liquidity / 10_000_000
    )


@pytest.mark.asyncio
async def test_slipstream_volume_usdc_per_day_uses_price_overrides():
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ("initial",)})
    mock_web3 = MagicMock()
    mock_web3.eth.block_number = AsyncMock(return_value=110)()
    mock_web3.eth.get_block = AsyncMock(
        side_effect=[
            {"timestamp": 1_000},
            {"timestamp": 1_100},
        ]
    )
    mock_web3.codec.decode = MagicMock(
        side_effect=[
            (1_000_000, -2_000_000, 0, 0, 0),
            (-500_000, 1_500_000, 0, 0, 0),
        ]
    )
    logs = [
        {"blockNumber": 100, "logIndex": 0, "data": b"one"},
        {"blockNumber": 101, "logIndex": 0, "data": b"two"},
    ]

    with (
        patch.object(slipstream_module, "web3_from_chain_id", _web3_ctx(mock_web3)),
        patch.object(
            adapter,
            "slipstream_pool_state",
            new=AsyncMock(
                return_value=(
                    True,
                    {
                        "pool": FAKE_POOL,
                        "token0": "0x0000000000000000000000000000000000000011",
                        "token1": "0x0000000000000000000000000000000000000012",
                    },
                )
            ),
        ),
        patch.object(
            adapter,
            "_token_decimals",
            new=AsyncMock(side_effect=[6, 6]),
        ),
        patch.object(
            adapter,
            "_get_logs_bounded",
            new=AsyncMock(return_value=logs),
        ),
    ):
        ok, data = await adapter.slipstream_volume_usdc_per_day(
            pool=FAKE_POOL,
            token0_price_usdc=1.0,
            token1_price_usdc=2.0,
        )

    assert ok is True
    assert data["swap_count"] == 2
    assert data["seconds_covered"] == 100
    assert data["volume_usdc_per_day"] == pytest.approx(6048.0)


@pytest.mark.asyncio
async def test_slipstream_fee_apr_percent_uses_price_overrides():
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ("initial",)})
    metrics = {
        "pool": FAKE_POOL,
        "token0": "0x0000000000000000000000000000000000000011",
        "token1": "0x0000000000000000000000000000000000000012",
        "in_range": True,
        "share_of_active_liquidity": 0.1,
        "amount0_now": 1_000_000,
        "amount1_now": 2_000_000,
        "effective_fee_fraction_for_unstaked": 0.003,
    }

    with patch.object(
        adapter,
        "_token_decimals",
        new=AsyncMock(side_effect=[6, 6, 6, 6]),
    ):
        ok, data = await adapter.slipstream_fee_apr_percent(
            metrics=metrics,
            volume_usdc_per_day=10.0,
            expected_in_range_fraction=0.5,
            token0_price_usdc=1.0,
            token1_price_usdc=2.0,
        )
        ok_out, data_out = await adapter.slipstream_fee_apr_percent(
            metrics={**metrics, "in_range": False},
            volume_usdc_per_day=10.0,
            expected_in_range_fraction=1.0,
            token0_price_usdc=1.0,
            token1_price_usdc=2.0,
        )

    assert ok is True
    assert data["position_value_usdc"] == pytest.approx(5.0)
    assert data["fee_apr_percent"] == pytest.approx(10.95, abs=1e-9)
    assert ok_out is True
    assert data_out["fee_apr_percent"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_get_logs_bounded_reduces_chunk_and_truncates():
    class _FakeEthLogs:
        def __init__(self):
            self.calls: list[dict[str, object]] = []

        async def get_logs(self, params: dict[str, object]):
            self.calls.append(params)
            from_block = int(params["fromBlock"])
            to_block = int(params["toBlock"])
            if (to_block - from_block + 1) > 3:
                from web3.exceptions import Web3RPCError

                raise Web3RPCError("too many results")
            return [
                {"blockNumber": bn, "logIndex": 0}
                for bn in range(from_block, to_block + 1)
            ]

    class _FakeWeb3Logs:
        def __init__(self):
            self.eth = _FakeEthLogs()

    web3 = _FakeWeb3Logs()
    logs = await AerodromeSlipstreamAdapter._get_logs_bounded(
        web3,
        from_block=0,
        to_block=9,
        address="0x0000000000000000000000000000000000000011",
        topics=["0x0"],
        max_logs=5,
        initial_chunk_size=8,
    )

    assert len(logs) == 5
    assert [int(log["blockNumber"]) for log in logs] == [5, 6, 7, 8, 9]
    assert len(web3.eth.calls) >= 2


@pytest.mark.asyncio
async def test_slipstream_sigma_annual_from_swaps_uses_swap_prices():
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ("initial",)})
    mock_web3 = MagicMock()
    mock_web3.eth.block_number = AsyncMock(return_value=500)()

    async def _get_block(block_number):
        return {"timestamp": int(block_number) * 10}

    mock_web3.eth.get_block = AsyncMock(side_effect=_get_block)
    mock_web3.codec.decode = MagicMock(
        side_effect=[
            (0, 0, sqrt_price_x96_from_tick(0), 0, 0),
            (0, 0, sqrt_price_x96_from_tick(10), 0, 0),
            (0, 0, sqrt_price_x96_from_tick(20), 0, 0),
            (0, 0, sqrt_price_x96_from_tick(30), 0, 0),
            (0, 0, sqrt_price_x96_from_tick(40), 0, 0),
        ]
    )
    logs = [
        {"blockNumber": 100, "logIndex": 0, "data": b"1"},
        {"blockNumber": 101, "logIndex": 0, "data": b"2"},
        {"blockNumber": 102, "logIndex": 0, "data": b"3"},
        {"blockNumber": 103, "logIndex": 0, "data": b"4"},
        {"blockNumber": 104, "logIndex": 0, "data": b"5"},
    ]

    with (
        patch.object(slipstream_module, "web3_from_chain_id", _web3_ctx(mock_web3)),
        patch.object(
            adapter,
            "slipstream_pool_state",
            new=AsyncMock(
                return_value=(
                    True,
                    {
                        "pool": FAKE_POOL,
                        "token0": "0x0000000000000000000000000000000000000011",
                        "token1": "0x0000000000000000000000000000000000000012",
                    },
                )
            ),
        ),
        patch.object(
            adapter,
            "_token_decimals",
            new=AsyncMock(side_effect=[6, 6]),
        ),
        patch.object(
            adapter,
            "_get_logs_bounded",
            new=AsyncMock(return_value=logs),
        ),
    ):
        ok, data = await adapter.slipstream_sigma_annual_from_swaps(pool=FAKE_POOL)

    assert ok is True
    assert data["sample_count"] == 5
    assert data["seconds_covered"] == 40
    assert data["sigma_annual"] is not None
    assert data["sigma_annual"] > 0


@pytest.mark.asyncio
async def test_slipstream_prob_in_range_week_matches_gaussian_estimate():
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ("initial",)})
    sigma_annual = 0.8

    with (
        patch.object(
            adapter,
            "slipstream_pool_state",
            new=AsyncMock(
                return_value=(
                    True,
                    {
                        "pool": FAKE_POOL,
                        "token0": "0x0000000000000000000000000000000000000011",
                        "token1": "0x0000000000000000000000000000000000000012",
                        "price_token1_per_token0": 1.0,
                    },
                )
            ),
        ),
        patch.object(
            adapter,
            "_token_decimals",
            new=AsyncMock(side_effect=[6, 6]),
        ),
    ):
        ok, data = await adapter.slipstream_prob_in_range_week(
            pool=FAKE_POOL,
            tick_lower=-60,
            tick_upper=60,
            sigma_annual=sigma_annual,
        )

    price_low = tick_to_price_decimal(-60, 6, 6)
    price_high = tick_to_price_decimal(60, 6, 6)
    denom = sigma_annual * math.sqrt(7.0 / 365.0)
    expected = max(
        0.0,
        min(
            1.0,
            AerodromeSlipstreamAdapter._phi(math.log(price_high) / denom)
            - AerodromeSlipstreamAdapter._phi(math.log(price_low) / denom),
        ),
    )

    assert ok is True
    assert data["prob_in_range_week"] == pytest.approx(expected)


@pytest.mark.asyncio
async def test_stake_position_dead_gauge_returns_clean_error(adapter_with_signer):
    voter = MagicMock()
    voter.functions.isAlive = MagicMock(return_value=_mock_call(False))

    gauge_contract = MagicMock()
    gauge_contract.functions.nft = MagicMock(return_value=_mock_call(FAKE_NPM))

    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(side_effect=[voter, gauge_contract])

    with patch.object(slipstream_module, "web3_from_chain_id", _web3_ctx(mock_web3)):
        ok, msg = await adapter_with_signer.stake_position(gauge=FAKE_GAUGE, token_id=1)

    assert ok is False
    assert "not alive" in msg.lower()


@pytest.mark.asyncio
async def test_ensure_erc721_approval_skips_tx_when_operator_already_approved(
    adapter_with_signer,
):
    nft = MagicMock()
    nft.functions.getApproved = MagicMock(return_value=_mock_call(ZERO_ADDRESS))
    nft.functions.isApprovedForAll = MagicMock(return_value=_mock_call(True))

    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(return_value=nft)

    with (
        patch.object(slipstream_module, "web3_from_chain_id", _web3_ctx(mock_web3)),
        patch.object(slipstream_module, "encode_call", new=AsyncMock()) as mock_encode,
        patch.object(
            slipstream_module, "send_transaction", new=AsyncMock()
        ) as mock_send,
    ):
        ok, result = await adapter_with_signer._ensure_erc721_approval(
            nft_contract=FAKE_NPM,
            token_id=1,
            operator=FAKE_GAUGE,
            owner=FAKE_WALLET,
        )

    assert ok is True
    assert result == {}
    mock_encode.assert_not_awaited()
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_pool_returns_multiple_match_error_without_variant():
    adapter = AerodromeSlipstreamAdapter(
        config={"deployments": ("initial", "gauge_caps")}
    )
    matches = [
        {"deployment_variant": "initial", "pool": FAKE_POOL},
        {
            "deployment_variant": "gauge_caps",
            "pool": "0x0000000000000000000000000000000000000004",
        },
    ]

    with patch.object(
        adapter, "find_pools", new=AsyncMock(return_value=(True, matches))
    ):
        ok, msg = await adapter.get_pool(
            tokenA="0x0000000000000000000000000000000000000001",
            tokenB="0x0000000000000000000000000000000000000002",
            tick_spacing=200,
        )

    assert ok is False
    assert "multiple slipstream pools matched" in msg.lower()


@pytest.mark.asyncio
async def test_get_pos_reads_resolved_position_state():
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ("initial",)})
    mock_web3 = MagicMock()

    with (
        patch.object(slipstream_module, "web3_from_chain_id", _web3_ctx(mock_web3)),
        patch.object(
            adapter,
            "_resolve_token_manager",
            new=AsyncMock(return_value=("initial", {}, FAKE_NPM, FAKE_WALLET)),
        ),
        patch.object(
            adapter,
            "_read_position_state",
            new=AsyncMock(return_value={"token_id": 7, "pool": FAKE_POOL}),
        ) as mock_read,
    ):
        ok, data = await adapter.get_pos(token_id=7, account=FAKE_WALLET)

    assert ok is True
    assert data["token_id"] == 7
    mock_read.assert_awaited_once()


@pytest.mark.asyncio
async def test_collect_fees_rejects_non_owned_position(adapter_with_signer):
    with patch.object(
        adapter_with_signer,
        "_resolve_token_manager",
        new=AsyncMock(return_value=("initial", {}, FAKE_NPM, FAKE_GAUGE)),
    ):
        ok, msg = await adapter_with_signer.collect_fees(token_id=42)

    assert ok is False
    assert "does not currently own token_id" in msg.lower()


@pytest.mark.asyncio
async def test_claim_fees_auto_discovers_reward_tokens(adapter_with_signer):
    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(return_value=MagicMock())

    with (
        patch.object(
            aerodrome_common_module, "web3_from_chain_id", _web3_ctx(mock_web3)
        ),
        patch.object(
            adapter_with_signer,
            "_reward_tokens",
            new=AsyncMock(side_effect=[["0x0000000000000000000000000000000000000005"]]),
        ),
        patch.object(
            aerodrome_common_module,
            "encode_call",
            new=AsyncMock(return_value={"chainId": CHAIN_ID_BASE}),
        ) as mock_encode,
        patch.object(
            aerodrome_common_module,
            "send_transaction",
            new=AsyncMock(return_value="0xtxhash"),
        ),
    ):
        ok, tx = await adapter_with_signer.claim_fees(
            token_id=1,
            fee_reward_contracts=["0x0000000000000000000000000000000000000006"],
        )

    assert ok is True
    assert tx == "0xtxhash"
    args = mock_encode.await_args.kwargs["args"]
    assert args[2] == 1
    assert args[1] == [["0x0000000000000000000000000000000000000005"]]


@pytest.mark.asyncio
async def test_get_vote_claimables_resolves_gauge_reward_contracts():
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ("initial",)})
    voter = MagicMock()
    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(return_value=voter)

    pool = "0x" + "11" * 20
    gauge = "0x" + "22" * 20
    fee_reward = "0x" + "33" * 20
    bribe_reward = "0x" + "44" * 20

    with (
        patch.object(
            adapter,
            "_enumerate_all_pools",
            new=AsyncMock(
                return_value=[
                    {
                        "pool": pool,
                        "deployment_variant": "initial",
                        "position_manager": FAKE_NPM,
                    }
                ]
            ),
        ),
        patch.object(
            slipstream_module,
            "web3_from_chain_id",
            _web3_ctx(mock_web3),
        ),
        patch.object(
            slipstream_module,
            "read_only_calls_multicall_or_gather",
            new=AsyncMock(
                side_effect=[
                    [gauge],
                    [fee_reward, bribe_reward],
                ]
            ),
        ) as mock_read,
        patch.object(
            adapter,
            "_get_vote_claimables",
            new=AsyncMock(
                return_value=[
                    {
                        "pool": pool,
                        "claimableFees": [],
                        "claimableBribes": [],
                    }
                ]
            ),
        ) as mock_get_vote_claimables,
    ):
        ok, data = await adapter.get_vote_claimables(
            token_id=7,
            deployments=("initial",),
            include_zero_positions=True,
            include_usd_values=True,
        )

    assert ok is True
    assert data == {
        "protocol": "aerodrome_slipstream",
        "chain_id": CHAIN_ID_BASE,
        "chain_name": "base",
        "deployments": ["initial"],
        "tokenId": 7,
        "votes": [
            {
                "pool": pool,
                "claimableFees": [],
                "claimableBribes": [],
            }
        ],
    }
    assert mock_read.await_count == 2
    assert mock_get_vote_claimables.await_args.kwargs["token_id"] == 7
    assert mock_get_vote_claimables.await_args.kwargs["pool_metadata_by_address"] == {
        pool.lower(): {
            "feeReward": fee_reward,
            "bribeReward": bribe_reward,
        }
    }
    assert mock_get_vote_claimables.await_args.kwargs["web3"] is mock_web3
    assert mock_get_vote_claimables.await_args.kwargs["voter_contract"] is voter
    assert mock_get_vote_claimables.await_args.kwargs["include_zero_positions"] is True
    assert mock_get_vote_claimables.await_args.kwargs["include_usd_values"] is True


@pytest.mark.asyncio
async def test_resolve_position_amount_mins_derives_from_current_price(
    adapter_with_signer,
):
    sqrt_price_x96 = sqrt_price_x96_from_tick(0)
    tick_lower = -120
    tick_upper = 120
    amount0_desired = 1_000_000
    amount1_desired = 1_000_000

    with patch.object(
        adapter_with_signer,
        "_current_sqrt_price_x96",
        new=AsyncMock(return_value=sqrt_price_x96),
    ):
        (
            amount0_min,
            amount1_min,
        ) = await adapter_with_signer._resolve_position_amount_mins(
            deployment=adapter_with_signer._deployment("initial"),
            token0="0x0000000000000000000000000000000000000001",
            token1="0x0000000000000000000000000000000000000002",
            tick_spacing=60,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            amount0_desired=amount0_desired,
            amount1_desired=amount1_desired,
            amount0_min=None,
            amount1_min=None,
            slippage_bps=50,
        )

    sqrt_lower = sqrt_price_x96_from_tick(tick_lower)
    sqrt_upper = sqrt_price_x96_from_tick(tick_upper)
    liquidity = liq_for_amounts(
        sqrt_price_x96,
        sqrt_lower,
        sqrt_upper,
        amount0_desired,
        amount1_desired,
    )
    expected0, expected1 = amounts_for_liq_inrange(
        sqrt_price_x96,
        sqrt_lower,
        sqrt_upper,
        liquidity,
    )
    assert amount0_min == slippage_min(expected0, 50)
    assert amount1_min == slippage_min(expected1, 50)
    assert amount0_min > 0
    assert amount1_min > 0


@pytest.mark.asyncio
async def test_resolve_liquidity_amount_mins_derives_from_current_price(
    adapter_with_signer,
):
    sqrt_price_x96 = sqrt_price_x96_from_tick(0)
    tick_lower = -120
    tick_upper = 120
    liquidity = 100_000

    with patch.object(
        adapter_with_signer,
        "_current_sqrt_price_x96",
        new=AsyncMock(return_value=sqrt_price_x96),
    ):
        (
            amount0_min,
            amount1_min,
        ) = await adapter_with_signer._resolve_liquidity_amount_mins(
            deployment=adapter_with_signer._deployment("initial"),
            token0="0x0000000000000000000000000000000000000001",
            token1="0x0000000000000000000000000000000000000002",
            tick_spacing=60,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            liquidity=liquidity,
            amount0_min=None,
            amount1_min=None,
            slippage_bps=50,
        )

    sqrt_lower = sqrt_price_x96_from_tick(tick_lower)
    sqrt_upper = sqrt_price_x96_from_tick(tick_upper)
    expected0, expected1 = amounts_for_liq_inrange(
        sqrt_price_x96,
        sqrt_lower,
        sqrt_upper,
        liquidity,
    )
    assert amount0_min == slippage_min(expected0, 50)
    assert amount1_min == slippage_min(expected1, 50)
    assert amount0_min > 0
    assert amount1_min > 0


@pytest.mark.asyncio
async def test_mint_position_uses_derived_mins_when_omitted(adapter_with_signer):
    with (
        patch.object(
            adapter_with_signer,
            "_resolve_position_amount_mins",
            new=AsyncMock(return_value=(111, 222)),
        ),
        patch.object(
            slipstream_module,
            "ensure_allowance",
            new=AsyncMock(return_value=(True, {})),
        ),
        patch.object(
            slipstream_module,
            "encode_call",
            new=AsyncMock(return_value={"chainId": CHAIN_ID_BASE}),
        ) as mock_encode,
        patch.object(
            slipstream_module,
            "send_transaction",
            new=AsyncMock(return_value="0xtxhash"),
        ),
        patch.object(
            adapter_with_signer,
            "_minted_erc721_token_id",
            new=AsyncMock(return_value=7),
        ),
    ):
        ok, data = await adapter_with_signer.mint_position(
            token0="0x0000000000000000000000000000000000000001",
            token1="0x0000000000000000000000000000000000000002",
            tick_spacing=60,
            tick_lower=-120,
            tick_upper=120,
            amount0_desired=1_000,
            amount1_desired=2_000,
        )

    assert ok is True
    params = mock_encode.await_args.kwargs["args"][0]
    assert params[7] == 111
    assert params[8] == 222
    assert data["token_id"] == 7


@pytest.mark.asyncio
async def test_increase_liquidity_uses_derived_mins_when_omitted(adapter_with_signer):
    positions_call = _mock_call(
        (
            0,
            ZERO_ADDRESS,
            "0x0000000000000000000000000000000000000001",
            "0x0000000000000000000000000000000000000002",
            60,
            -120,
            120,
            123,
            0,
            0,
            0,
            0,
        )
    )
    npm = MagicMock()
    npm.functions.positions = MagicMock(return_value=positions_call)
    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(return_value=npm)

    with (
        patch.object(slipstream_module, "web3_from_chain_id", _web3_ctx(mock_web3)),
        patch.object(
            adapter_with_signer,
            "_resolve_token_manager",
            new=AsyncMock(return_value=("initial", {}, FAKE_NPM, FAKE_WALLET)),
        ),
        patch.object(
            adapter_with_signer,
            "_resolve_position_amount_mins",
            new=AsyncMock(return_value=(333, 444)),
        ),
        patch.object(
            slipstream_module,
            "ensure_allowance",
            new=AsyncMock(return_value=(True, {})),
        ),
        patch.object(
            slipstream_module,
            "encode_call",
            new=AsyncMock(return_value={"chainId": CHAIN_ID_BASE}),
        ) as mock_encode,
        patch.object(
            slipstream_module,
            "send_transaction",
            new=AsyncMock(return_value="0xtxhash"),
        ),
    ):
        ok, _ = await adapter_with_signer.increase_liquidity(
            token_id=42,
            amount0_desired=1_000,
            amount1_desired=2_000,
        )

    assert ok is True
    params = mock_encode.await_args.kwargs["args"][0]
    assert params[3] == 333
    assert params[4] == 444


@pytest.mark.asyncio
async def test_decrease_liquidity_uses_derived_mins_when_omitted(adapter_with_signer):
    positions_call = _mock_call(
        (
            0,
            ZERO_ADDRESS,
            "0x0000000000000000000000000000000000000001",
            "0x0000000000000000000000000000000000000002",
            60,
            -120,
            120,
            123,
            0,
            0,
            0,
            0,
        )
    )
    npm = MagicMock()
    npm.functions.positions = MagicMock(return_value=positions_call)
    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(return_value=npm)

    with (
        patch.object(slipstream_module, "web3_from_chain_id", _web3_ctx(mock_web3)),
        patch.object(
            adapter_with_signer,
            "_resolve_token_manager",
            new=AsyncMock(return_value=("initial", {}, FAKE_NPM, FAKE_WALLET)),
        ),
        patch.object(
            adapter_with_signer,
            "_resolve_liquidity_amount_mins",
            new=AsyncMock(return_value=(555, 666)),
        ),
        patch.object(
            slipstream_module,
            "encode_call",
            new=AsyncMock(return_value={"chainId": CHAIN_ID_BASE}),
        ) as mock_encode,
        patch.object(
            slipstream_module,
            "send_transaction",
            new=AsyncMock(return_value="0xtxhash"),
        ),
    ):
        ok, _ = await adapter_with_signer.decrease_liquidity(
            token_id=42,
            liquidity=50,
        )

    assert ok is True
    params = mock_encode.await_args.kwargs["args"][0]
    assert params[2] == 555
    assert params[3] == 666


@pytest.mark.asyncio
async def test_mint_position_preserves_explicit_zero_mins(adapter_with_signer):
    with (
        patch.object(
            adapter_with_signer,
            "_resolve_position_amount_mins",
            new=AsyncMock(return_value=(0, 0)),
        ) as mock_resolve,
        patch.object(
            slipstream_module,
            "ensure_allowance",
            new=AsyncMock(return_value=(True, {})),
        ),
        patch.object(
            slipstream_module,
            "encode_call",
            new=AsyncMock(return_value={"chainId": CHAIN_ID_BASE}),
        ) as mock_encode,
        patch.object(
            slipstream_module,
            "send_transaction",
            new=AsyncMock(return_value="0xtxhash"),
        ),
        patch.object(
            adapter_with_signer,
            "_minted_erc721_token_id",
            new=AsyncMock(return_value=7),
        ),
    ):
        ok, _ = await adapter_with_signer.mint_position(
            token0="0x0000000000000000000000000000000000000001",
            token1="0x0000000000000000000000000000000000000002",
            tick_spacing=60,
            tick_lower=-120,
            tick_upper=120,
            amount0_desired=1_000,
            amount1_desired=2_000,
            amount0_min=0,
            amount1_min=0,
        )

    assert ok is True
    params = mock_encode.await_args.kwargs["args"][0]
    assert params[7] == 0
    assert params[8] == 0
    mock_resolve.assert_awaited_once()
