import inspect
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import wayfinder_paths.adapters.aerodrome_adapter.adapter as aerodrome_adapter_module
import wayfinder_paths.adapters.aerodrome_common as aerodrome_common_module
from wayfinder_paths.adapters.aerodrome_adapter.adapter import (
    EPOCH_SPECIAL_WINDOW_SECONDS,
    WEEK_SECONDS,
    AerodromeAdapter,
)
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE

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
