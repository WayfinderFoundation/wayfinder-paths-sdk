import inspect
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import wayfinder_paths.adapters.aerodrome_common as aerodrome_common_module
import wayfinder_paths.adapters.aerodrome_slipstream_adapter.adapter as slipstream_module
from wayfinder_paths.adapters.aerodrome_slipstream_adapter.adapter import (
    EPOCH_SPECIAL_WINDOW_SECONDS,
    WEEK_SECONDS,
    AerodromeSlipstreamAdapter,
)
from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.utils.uniswap_v3_math import (
    amounts_for_liq_inrange,
    liq_for_amounts,
    slippage_min,
    sqrt_price_x96_from_tick,
)

FAKE_WALLET = "0x1234567890123456789012345678901234567890"
FAKE_POOL = "0x0000000000000000000000000000000000000001"
FAKE_GAUGE = "0x0000000000000000000000000000000000000002"
FAKE_NPM = "0x0000000000000000000000000000000000000003"


@pytest.fixture
def adapter_with_signer():
    return AerodromeSlipstreamAdapter(
        config={"deployments": ["initial"]},
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
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ["initial"]})
    assert adapter.adapter_type == "AERODROME_SLIPSTREAM"


def test_constructor_is_base_only():
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ["initial"]})
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
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ["initial"]})
    ok, msg = await getattr(adapter, method)(**kwargs)
    assert ok is False
    assert msg == "wallet address not configured"


@pytest.mark.asyncio
async def test_can_vote_now_rejects_first_hour():
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ["initial"]})
    mock_web3 = MagicMock()
    mock_web3.eth.get_block = AsyncMock(return_value={"timestamp": WEEK_SECONDS + 1})

    with patch.object(aerodrome_common_module, "web3_from_chain_id", _web3_ctx(mock_web3)):
        ok, msg = await adapter._can_vote_now()

    assert ok is False
    assert "first hour" in msg.lower()


@pytest.mark.asyncio
async def test_can_vote_now_rejects_last_hour_without_token_id():
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ["initial"]})
    mock_web3 = MagicMock()
    mock_web3.eth.get_block = AsyncMock(
        return_value={"timestamp": (2 * WEEK_SECONDS) - EPOCH_SPECIAL_WINDOW_SECONDS}
    )

    with patch.object(aerodrome_common_module, "web3_from_chain_id", _web3_ctx(mock_web3)):
        ok, msg = await adapter._can_vote_now()

    assert ok is False
    assert "token_id required" in msg.lower()


@pytest.mark.asyncio
async def test_can_vote_now_allows_whitelisted_nft_in_last_hour():
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ["initial"]})
    voter = MagicMock()
    voter.functions.isWhitelistedNFT = MagicMock(return_value=_mock_call(True))

    mock_web3 = MagicMock()
    mock_web3.eth.get_block = AsyncMock(
        return_value={"timestamp": (2 * WEEK_SECONDS) - EPOCH_SPECIAL_WINDOW_SECONDS}
    )
    mock_web3.eth.contract = MagicMock(return_value=voter)

    with patch.object(aerodrome_common_module, "web3_from_chain_id", _web3_ctx(mock_web3)):
        ok, msg = await adapter._can_vote_now(token_id=123)

    assert ok is True
    assert msg == ""
    voter.functions.isWhitelistedNFT.assert_called_once_with(123)


@pytest.mark.asyncio
async def test_get_all_markets_empty_result_uses_base_chain():
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ["initial"]})
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
async def test_get_pool_returns_multiple_match_error_without_variant():
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ["initial", "gauge_caps"]})
    matches = [
        {"deployment_variant": "initial", "pool": FAKE_POOL},
        {"deployment_variant": "gauge_caps", "pool": "0x0000000000000000000000000000000000000004"},
    ]

    with patch.object(adapter, "find_pools", new=AsyncMock(return_value=(True, matches))):
        ok, msg = await adapter.get_pool(
            tokenA="0x0000000000000000000000000000000000000001",
            tokenB="0x0000000000000000000000000000000000000002",
            tick_spacing=200,
        )

    assert ok is False
    assert "multiple slipstream pools matched" in msg.lower()


@pytest.mark.asyncio
async def test_get_pos_reads_resolved_position_state():
    adapter = AerodromeSlipstreamAdapter(config={"deployments": ["initial"]})
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
        patch.object(aerodrome_common_module, "web3_from_chain_id", _web3_ctx(mock_web3)),
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
async def test_resolve_position_amount_mins_derives_from_current_price(adapter_with_signer):
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
        amount0_min, amount1_min = await adapter_with_signer._resolve_position_amount_mins(
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
async def test_resolve_liquidity_amount_mins_derives_from_current_price(adapter_with_signer):
    sqrt_price_x96 = sqrt_price_x96_from_tick(0)
    tick_lower = -120
    tick_upper = 120
    liquidity = 100_000

    with patch.object(
        adapter_with_signer,
        "_current_sqrt_price_x96",
        new=AsyncMock(return_value=sqrt_price_x96),
    ):
        amount0_min, amount1_min = await adapter_with_signer._resolve_liquidity_amount_mins(
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
