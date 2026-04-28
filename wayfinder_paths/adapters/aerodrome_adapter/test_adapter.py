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
from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.constants.base import SECONDS_PER_YEAR
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
        "v2_pool_tvl_usdc",
        "v2_staked_tvl_usdc",
        "v2_emissions_apr",
        "rank_v2_pools_by_emissions_apr",
        "quote_add_liquidity",
        "add_liquidity",
        "quote_remove_liquidity",
        "remove_liquidity",
        "claim_pool_fees_unstaked",
        "lp_balance",
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
        "get_vote_claimables",
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
async def test_lp_balance_reads_wallet_token_balance(adapter_with_signer):
    with patch.object(
        aerodrome_adapter_module,
        "get_token_balance",
        new=AsyncMock(return_value=1234),
    ) as mock_get_token_balance:
        balance = await adapter_with_signer.lp_balance(FAKE_POOL)

    assert balance == 1234
    mock_get_token_balance.assert_awaited_once_with(
        token_address=FAKE_POOL,
        chain_id=CHAIN_ID_BASE,
        wallet_address=FAKE_WALLET,
    )


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
async def test_can_vote_now_returns_false_when_already_voted_this_epoch():
    adapter = AerodromeAdapter()
    now = WEEK_SECONDS + 500

    voter = MagicMock()
    voter.functions.lastVoted = MagicMock(return_value=_mock_call(WEEK_SECONDS))

    mock_web3 = MagicMock()
    mock_web3.eth.get_block = AsyncMock(return_value={"timestamp": now})
    mock_web3.eth.contract = MagicMock(return_value=voter)

    with patch.object(
        aerodrome_common_module,
        "web3_from_chain_id",
        _web3_ctx(mock_web3),
    ):
        ok, data = await adapter.can_vote_now(token_id=7)

    assert ok is True
    assert data["can_vote"] is False
    assert data["last_voted"] == WEEK_SECONDS
    assert data["epoch_start"] == WEEK_SECONDS
    assert data["next_epoch_start"] == 2 * WEEK_SECONDS


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
async def test_get_all_markets_batches_related_reads():
    adapter = AerodromeAdapter()
    pool0 = "0x" + "11" * 20
    pool1 = "0x" + "22" * 20
    gauge0 = "0x" + "33" * 20
    gauge1 = "0x" + "44" * 20
    token0 = "0x" + "55" * 20
    token1 = "0x" + "66" * 20
    token2 = "0x" + "77" * 20
    token3 = "0x" + "88" * 20
    fee0 = "0x" + "99" * 20
    fee1 = "0x" + "aa" * 20
    bribe0 = "0x" + "bb" * 20
    bribe1 = "0x" + "cc" * 20
    reward0 = "0x" + "dd" * 20
    reward1 = "0x" + "ee" * 20

    voter = MagicMock()
    voter.functions.length = MagicMock(return_value=_mock_call(2))

    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(
        side_effect=[
            voter,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
        ]
    )

    with (
        patch.object(
            aerodrome_adapter_module,
            "web3_from_chain_id",
            _web3_ctx(mock_web3),
        ),
        patch.object(
            aerodrome_adapter_module,
            "read_only_calls_multicall_or_gather",
            new=AsyncMock(
                side_effect=[
                    [pool0, pool1],
                    [
                        (18, 6, 100, 200, False, token0, token1),
                        gauge0,
                        (6, 18, 300, 400, True, token2, token3),
                        gauge1,
                    ],
                    [
                        fee0,
                        bribe0,
                        reward0,
                        11,
                        12,
                        13,
                        fee1,
                        bribe1,
                        reward1,
                        21,
                        22,
                        23,
                    ],
                ]
            ),
        ) as mock_read,
    ):
        ok, data = await adapter.get_all_markets(limit=2)

    assert ok is True
    assert mock_read.await_count == 3
    assert len(data["markets"]) == 2
    assert data["markets"][0]["pool"].lower() == pool0.lower()
    assert data["markets"][0]["gauge"].lower() == gauge0.lower()
    assert data["markets"][0]["fees_reward"].lower() == fee0.lower()
    assert data["markets"][0]["gauge_reward_rate"] == 11
    assert data["markets"][1]["pool"].lower() == pool1.lower()
    assert data["markets"][1]["gauge"].lower() == gauge1.lower()
    assert data["markets"][1]["bribe_reward"].lower() == bribe1.lower()
    assert data["markets"][1]["gauge_total_supply"] == 22


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


@pytest.mark.asyncio
async def test_minted_erc721_token_id_reads_matching_transfer():
    adapter = AerodromeAdapter()
    nft_contract = "0x" + "12" * 20
    mock_web3 = MagicMock()
    mock_web3.codec = object()
    mock_web3.eth.get_transaction_receipt = AsyncMock(
        return_value={
            "logs": [
                {
                    "address": nft_contract,
                    "topics": [aerodrome_common_module._ERC721_TRANSFER_TOPIC0],
                }
            ]
        }
    )

    with (
        patch.object(
            aerodrome_common_module,
            "web3_from_chain_id",
            _web3_ctx(mock_web3),
        ),
        patch.object(
            aerodrome_common_module,
            "get_event_data",
            return_value={
                "args": {
                    "from": ZERO_ADDRESS,
                    "to": FAKE_WALLET,
                    "tokenId": 77,
                }
            },
        ),
    ):
        token_id = await adapter._minted_erc721_token_id(
            nft_contract=nft_contract,
            tx_hash="0xtxhash",
            expected_to=FAKE_WALLET,
        )

    assert token_id == 77


@pytest.mark.asyncio
async def test_minted_erc721_token_id_returns_none_for_non_matching_logs():
    adapter = AerodromeAdapter()
    nft_contract = "0x" + "12" * 20
    other_contract = "0x" + "34" * 20
    other_wallet = "0x" + "ab" * 20
    mock_web3 = MagicMock()
    mock_web3.codec = object()
    mock_web3.eth.get_transaction_receipt = AsyncMock(
        return_value={
            "logs": [
                {
                    "address": other_contract,
                    "topics": [aerodrome_common_module._ERC721_TRANSFER_TOPIC0],
                },
                {
                    "address": nft_contract,
                    "topics": [b"wrong-topic"],
                },
                {
                    "address": nft_contract,
                    "topics": [aerodrome_common_module._ERC721_TRANSFER_TOPIC0],
                },
                {
                    "address": nft_contract,
                    "topics": [aerodrome_common_module._ERC721_TRANSFER_TOPIC0],
                },
                {
                    "address": nft_contract,
                    "topics": [aerodrome_common_module._ERC721_TRANSFER_TOPIC0],
                },
                {
                    "address": nft_contract,
                    "topics": [aerodrome_common_module._ERC721_TRANSFER_TOPIC0],
                },
            ]
        }
    )

    with (
        patch.object(
            aerodrome_common_module,
            "web3_from_chain_id",
            _web3_ctx(mock_web3),
        ),
        patch.object(
            aerodrome_common_module,
            "get_event_data",
            side_effect=[
                {"args": {"from": ZERO_ADDRESS, "to": FAKE_WALLET}},
                {"args": {"from": FAKE_WALLET, "to": FAKE_WALLET, "tokenId": 1}},
                {"args": {"from": ZERO_ADDRESS, "to": other_wallet, "tokenId": 2}},
                ValueError("bad log"),
            ],
        ) as mock_get_event_data,
    ):
        token_id = await adapter._minted_erc721_token_id(
            nft_contract=nft_contract,
            tx_hash="0xtxhash",
            expected_to=FAKE_WALLET,
        )

    assert token_id is None
    assert mock_get_event_data.call_count == 4


@pytest.mark.asyncio
async def test_get_reward_contracts_reads_fee_and_bribe_contracts():
    adapter = AerodromeAdapter()
    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(return_value=MagicMock())

    with (
        patch.object(
            aerodrome_common_module,
            "web3_from_chain_id",
            _web3_ctx(mock_web3),
        ),
        patch.object(
            aerodrome_common_module,
            "read_only_calls_multicall_or_gather",
            new=AsyncMock(
                return_value=(
                    "0x0000000000000000000000000000000000000005",
                    "0x0000000000000000000000000000000000000006",
                )
            ),
        ) as mock_read,
    ):
        ok, data = await adapter.get_reward_contracts(gauge=FAKE_GAUGE)

    assert ok is True
    assert data == {
        "fees": "0x0000000000000000000000000000000000000005",
        "bribes": "0x0000000000000000000000000000000000000006",
    }
    assert mock_read.await_args.kwargs["block_identifier"] == "latest"
    assert mock_read.await_args.kwargs["chain_id"] == CHAIN_ID_BASE


@pytest.mark.asyncio
async def test_claim_gauge_rewards_validates_inputs(adapter_with_signer):
    adapter = AerodromeAdapter(wallet_address=FAKE_WALLET)

    ok_missing_signer, msg_missing_signer = await adapter.claim_gauge_rewards(
        gauges=[FAKE_GAUGE]
    )
    ok_empty, msg_empty = await adapter_with_signer.claim_gauge_rewards(gauges=[])

    assert ok_missing_signer is False
    assert msg_missing_signer == "sign_callback is required"
    assert ok_empty is False
    assert msg_empty == "gauges cannot be empty"


@pytest.mark.asyncio
async def test_ve_balance_of_nft_reads_balance():
    adapter = AerodromeAdapter()
    ve = MagicMock()
    ve.functions.balanceOfNFT = MagicMock(return_value=_mock_call(123456))

    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(return_value=ve)

    with patch.object(
        aerodrome_common_module,
        "web3_from_chain_id",
        _web3_ctx(mock_web3),
    ):
        ok, balance = await adapter.ve_balance_of_nft(
            token_id=7,
            block_identifier="pending",
        )

    assert ok is True
    assert balance == 123456
    ve.functions.balanceOfNFT.assert_called_once_with(7)


@pytest.mark.asyncio
async def test_ve_balance_of_nft_returns_clean_error_on_exception():
    adapter = AerodromeAdapter()
    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(side_effect=RuntimeError("boom"))

    with patch.object(
        aerodrome_common_module,
        "web3_from_chain_id",
        _web3_ctx(mock_web3),
    ):
        ok, msg = await adapter.ve_balance_of_nft(token_id=7)

    assert ok is False
    assert "boom" in msg


@pytest.mark.asyncio
async def test_get_user_ve_nfts_requires_address_when_no_wallet():
    adapter = AerodromeAdapter()

    ok, msg = await adapter.get_user_ve_nfts()

    assert ok is False
    assert "address is required" in msg.lower()


@pytest.mark.asyncio
async def test_get_user_ve_nfts_reads_token_ids_from_balance(adapter_with_signer):
    ve = MagicMock()
    ve.functions.balanceOf = MagicMock(return_value=_mock_call(2))
    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(return_value=ve)

    with (
        patch.object(
            aerodrome_common_module,
            "web3_from_chain_id",
            _web3_ctx(mock_web3),
        ),
        patch.object(
            aerodrome_common_module,
            "read_only_calls_multicall_or_gather",
            new=AsyncMock(return_value=[101, 202]),
        ) as mock_read,
    ):
        ok, token_ids = await adapter_with_signer.get_user_ve_nfts(
            block_identifier="pending"
        )

    assert ok is True
    assert token_ids == [101, 202]
    ve.functions.balanceOf.assert_called_once_with(FAKE_WALLET)
    assert mock_read.await_args.kwargs["block_identifier"] == "pending"
    assert mock_read.await_args.kwargs["chain_id"] == CHAIN_ID_BASE


@pytest.mark.asyncio
async def test_create_lock_for_uses_receiver_for_minted_token_lookup(
    adapter_with_signer,
):
    receiver = "0x00000000000000000000000000000000000000AA"

    with (
        patch.object(
            aerodrome_common_module,
            "ensure_allowance",
            new=AsyncMock(return_value=(True, {})),
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
        patch.object(
            adapter_with_signer,
            "_minted_erc721_token_id",
            new=AsyncMock(return_value=9),
        ) as mock_minted,
    ):
        ok, data = await adapter_with_signer.create_lock_for(
            amount=123,
            lock_duration=456,
            receiver=receiver,
        )

    assert ok is True
    assert data == {"tx": "0xtxhash", "token_id": 9}
    assert mock_encode.await_args.kwargs["fn_name"] == "createLockFor"
    assert mock_encode.await_args.kwargs["args"] == [
        123,
        456,
        "0x00000000000000000000000000000000000000AA",
    ]
    assert mock_minted.await_args.kwargs["expected_to"] == receiver


@pytest.mark.asyncio
async def test_increase_lock_amount_returns_allowance_failure(adapter_with_signer):
    with patch.object(
        aerodrome_common_module,
        "ensure_allowance",
        new=AsyncMock(return_value=(False, "approval failed")),
    ):
        ok, msg = await adapter_with_signer.increase_lock_amount(token_id=7, amount=11)

    assert ok is False
    assert msg == "approval failed"


@pytest.mark.asyncio
async def test_increase_unlock_time_encodes_expected_call(adapter_with_signer):
    with (
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
        ok, tx = await adapter_with_signer.increase_unlock_time(
            token_id=7,
            lock_duration=999,
        )

    assert ok is True
    assert tx == "0xtxhash"
    assert mock_encode.await_args.kwargs["fn_name"] == "increaseUnlockTime"
    assert mock_encode.await_args.kwargs["args"] == [7, 999]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "fn_name"),
    [
        ("withdraw_lock", "withdraw"),
        ("lock_permanent", "lockPermanent"),
        ("unlock_permanent", "unlockPermanent"),
    ],
)
async def test_lock_management_methods_encode_expected_call(
    adapter_with_signer,
    method_name,
    fn_name,
):
    with (
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
        ok, tx = await getattr(adapter_with_signer, method_name)(token_id=7)

    assert ok is True
    assert tx == "0xtxhash"
    assert mock_encode.await_args.kwargs["fn_name"] == fn_name
    assert mock_encode.await_args.kwargs["args"] == [7]


@pytest.mark.asyncio
async def test_vote_rejects_pools_weights_length_mismatch(adapter_with_signer):
    ok, msg = await adapter_with_signer.vote(
        token_id=1,
        pools=[FAKE_POOL],
        weights=[1, 2],
    )

    assert ok is False
    assert "length mismatch" in msg.lower()


@pytest.mark.asyncio
async def test_vote_skips_window_check_when_disabled(adapter_with_signer):
    with (
        patch.object(
            adapter_with_signer,
            "_can_vote_now",
            new=AsyncMock(side_effect=AssertionError("should not be called")),
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
        ok, tx = await adapter_with_signer.vote(
            token_id=5,
            pools=[FAKE_POOL],
            weights=[123],
            check_window=False,
        )

    assert ok is True
    assert tx == "0xtxhash"
    assert mock_encode.await_args.kwargs["fn_name"] == "vote"
    assert mock_encode.await_args.kwargs["args"] == [5, [FAKE_POOL], [123]]


@pytest.mark.asyncio
async def test_reset_vote_returns_window_restriction(adapter_with_signer):
    with patch.object(
        adapter_with_signer,
        "_can_vote_now",
        new=AsyncMock(return_value=(False, "window closed")),
    ):
        ok, msg = await adapter_with_signer.reset_vote(token_id=5)

    assert ok is False
    assert msg == "window closed"


@pytest.mark.asyncio
async def test_claim_bribes_auto_discovers_reward_tokens(adapter_with_signer):
    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(return_value=MagicMock())

    with (
        patch.object(
            aerodrome_common_module,
            "web3_from_chain_id",
            _web3_ctx(mock_web3),
        ),
        patch.object(
            adapter_with_signer,
            "_reward_tokens",
            new=AsyncMock(side_effect=[["0x0000000000000000000000000000000000000007"]]),
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
        ok, tx = await adapter_with_signer.claim_bribes(
            token_id=1,
            bribe_reward_contracts=["0x0000000000000000000000000000000000000006"],
        )

    assert ok is True
    assert tx == "0xtxhash"
    assert mock_encode.await_args.kwargs["fn_name"] == "claimBribes"
    assert mock_encode.await_args.kwargs["args"] == [
        ["0x0000000000000000000000000000000000000006"],
        [["0x0000000000000000000000000000000000000007"]],
        1,
    ]


@pytest.mark.asyncio
async def test_claim_fees_rejects_token_list_length_mismatch(adapter_with_signer):
    ok, msg = await adapter_with_signer.claim_fees(
        token_id=1,
        fee_reward_contracts=["0x0000000000000000000000000000000000000006"],
        token_lists=[],
    )

    assert ok is False
    assert "length mismatch" in msg.lower()


@pytest.mark.asyncio
async def test_claim_fees_uses_explicit_token_lists_without_lookup(adapter_with_signer):
    with (
        patch.object(
            adapter_with_signer,
            "_reward_tokens",
            new=AsyncMock(side_effect=AssertionError("should not be called")),
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
            token_lists=[["0x0000000000000000000000000000000000000007"]],
        )

    assert ok is True
    assert tx == "0xtxhash"
    assert mock_encode.await_args.kwargs["fn_name"] == "claimFees"
    assert mock_encode.await_args.kwargs["args"] == [
        ["0x0000000000000000000000000000000000000006"],
        [["0x0000000000000000000000000000000000000007"]],
        1,
    ]


@pytest.mark.asyncio
async def test_claim_rebases_skip_if_zero_returns_without_tx(adapter_with_signer):
    with (
        patch.object(
            adapter_with_signer,
            "get_rebase_claimable",
            new=AsyncMock(return_value=(True, 0)),
        ),
        patch.object(
            aerodrome_common_module,
            "encode_call",
            new=AsyncMock(side_effect=AssertionError("should not be called")),
        ),
    ):
        ok, data = await adapter_with_signer.claim_rebases(token_id=1)

    assert ok is True
    assert data == {"tx": None, "claimable": 0}


@pytest.mark.asyncio
async def test_claim_rebases_many_rejects_empty_token_ids(adapter_with_signer):
    ok, msg = await adapter_with_signer.claim_rebases_many(token_ids=[])

    assert ok is False
    assert "cannot be empty" in msg.lower()


@pytest.mark.asyncio
async def test_get_pool_returns_clean_error_for_zero_address():
    adapter = AerodromeAdapter()
    factory = MagicMock()
    factory.functions.getPool = MagicMock(return_value=_mock_call(ZERO_ADDRESS))

    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(return_value=factory)

    with patch.object(
        aerodrome_adapter_module,
        "web3_from_chain_id",
        _web3_ctx(mock_web3),
    ):
        ok, msg = await adapter.get_pool(
            tokenA="0x0000000000000000000000000000000000000001",
            tokenB="0x0000000000000000000000000000000000000002",
            stable=False,
        )

    assert ok is False
    assert msg == "Pool does not exist"


@pytest.mark.asyncio
async def test_get_gauge_returns_clean_error_for_zero_address():
    adapter = AerodromeAdapter()
    voter = MagicMock()
    voter.functions.gauges = MagicMock(return_value=_mock_call(ZERO_ADDRESS))

    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(return_value=voter)

    with patch.object(
        aerodrome_adapter_module,
        "web3_from_chain_id",
        _web3_ctx(mock_web3),
    ):
        ok, msg = await adapter.get_gauge(pool=FAKE_POOL)

    assert ok is False
    assert msg == "Gauge not found for pool"


@pytest.mark.asyncio
async def test_get_full_user_state_batches_related_reads():
    adapter = AerodromeAdapter()
    pool0 = "0x" + "11" * 20
    pool1 = "0x" + "22" * 20
    gauge0 = "0x" + "33" * 20
    gauge1 = "0x" + "44" * 20

    voter = MagicMock()
    voter.functions.length = MagicMock(return_value=_mock_call(2))

    ve = MagicMock()
    ve.functions.balanceOf = MagicMock(return_value=_mock_call(2))

    rd = MagicMock()

    gauge_contract0 = MagicMock()
    gauge_contract0.address = gauge0
    gauge_contract1 = MagicMock()
    gauge_contract1.address = gauge1

    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(
        side_effect=[
            voter,
            ve,
            rd,
            MagicMock(),
            MagicMock(),
            gauge_contract0,
            gauge_contract1,
        ]
    )

    with (
        patch.object(
            aerodrome_adapter_module,
            "web3_from_chain_id",
            _web3_ctx(mock_web3),
        ),
        patch.object(
            adapter,
            "get_user_ve_nfts",
            new=AsyncMock(side_effect=AssertionError("unexpected helper call")),
        ),
        patch.object(
            aerodrome_adapter_module,
            "read_only_calls_multicall_or_gather",
            new=AsyncMock(
                side_effect=[
                    [pool0, pool1],
                    [101, 202],
                    [50, gauge0, 60, gauge1],
                    [500, 5, 600, 6],
                    [700, True, 70, 800, False, 80],
                ]
            ),
        ) as mock_read,
    ):
        ok, data = await adapter.get_full_user_state(
            account=FAKE_WALLET,
            limit=2,
            include_votes=False,
        )

    assert ok is True
    assert mock_read.await_count == 5
    assert data["markets_scan"]["total"] == 2
    assert data["lp_positions"] == [
        {
            "pool": pool0,
            "wallet_lp_balance": 50,
            "gauge": gauge0,
            "gauge_staked_balance": 500,
            "gauge_earned": 5,
        },
        {
            "pool": pool1,
            "wallet_lp_balance": 60,
            "gauge": gauge1,
            "gauge_staked_balance": 600,
            "gauge_earned": 6,
        },
    ]
    assert data["ve_nfts"] == [
        {
            "token_id": 101,
            "voting_power": 700,
            "voted": True,
            "rebase_claimable": 70,
        },
        {
            "token_id": 202,
            "voting_power": 800,
            "voted": False,
            "rebase_claimable": 80,
        },
    ]


@pytest.mark.asyncio
async def test_get_full_user_state_includes_vote_claimables_flag():
    adapter = AerodromeAdapter()
    pool0 = "0x" + "11" * 20
    gauge0 = "0x" + "33" * 20

    voter = MagicMock()
    voter.functions.length = MagicMock(return_value=_mock_call(1))

    ve = MagicMock()
    ve.functions.balanceOf = MagicMock(return_value=_mock_call(1))

    rd = MagicMock()

    gauge_contract0 = MagicMock()
    gauge_contract0.address = gauge0

    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(
        side_effect=[
            voter,
            ve,
            rd,
            MagicMock(),
            gauge_contract0,
        ]
    )

    with (
        patch.object(
            aerodrome_adapter_module,
            "web3_from_chain_id",
            _web3_ctx(mock_web3),
        ),
        patch.object(
            adapter,
            "get_vote_claimables",
            new=AsyncMock(
                return_value=(
                    True,
                    {
                        "votes": [
                            {
                                "pool": pool0,
                                "claimableFees": [],
                                "claimableBribes": [],
                            }
                        ]
                    },
                )
            ),
        ) as mock_get_vote_claimables,
        patch.object(
            aerodrome_adapter_module,
            "read_only_calls_multicall_or_gather",
            new=AsyncMock(
                side_effect=[
                    [pool0],
                    [101],
                    [50, gauge0],
                    [500, 5],
                    [700, True, 70],
                ]
            ),
        ),
    ):
        ok, data = await adapter.get_full_user_state(
            account=FAKE_WALLET,
            limit=1,
            include_vote_claimables=True,
        )

    assert ok is True
    assert data["ve_nfts"] == [
        {
            "token_id": 101,
            "voting_power": 700,
            "voted": True,
            "rebase_claimable": 70,
            "vote_claimables": [
                {
                    "pool": pool0,
                    "claimableFees": [],
                    "claimableBribes": [],
                }
            ],
        }
    ]
    mock_get_vote_claimables.assert_awaited_once_with(
        token_id=101,
        block_identifier="latest",
    )


@pytest.mark.asyncio
async def test_get_vote_claimables_uses_pool_metadata_and_shared_helper():
    adapter = AerodromeAdapter()
    voter = MagicMock()
    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(return_value=voter)

    pool = SugarPool(
        lp="0x" + "11" * 20,
        symbol="AERO/USDC",
        lp_decimals=18,
        lp_total_supply=0,
        pool_type=0,
        tick=0,
        sqrt_ratio=0,
        token0="0x" + "12" * 20,
        reserve0=0,
        staked0=0,
        token1="0x" + "13" * 20,
        reserve1=0,
        staked1=0,
        gauge="0x" + "14" * 20,
        gauge_liquidity=0,
        gauge_alive=True,
        fee="0x" + "15" * 20,
        bribe="0x" + "16" * 20,
        factory="0x" + "17" * 20,
        emissions_per_sec=0,
        emissions_token="0x" + "18" * 20,
        pool_fee_pips=0,
        unstaked_fee_pips=0,
        token0_fees=0,
        token1_fees=0,
        created_at=0,
    )

    with (
        patch.object(
            adapter,
            "pools_by_lp",
            new=AsyncMock(return_value={pool.lp: pool}),
        ),
        patch.object(
            adapter,
            "_get_vote_claimables",
            new=AsyncMock(
                return_value=[
                    {
                        "pool": pool.lp,
                        "claimableFees": [],
                        "claimableBribes": [],
                    }
                ]
            ),
        ) as mock_get_vote_claimables,
        patch.object(
            aerodrome_adapter_module,
            "web3_from_chain_id",
            _web3_ctx(mock_web3),
        ),
    ):
        ok, data = await adapter.get_vote_claimables(
            token_id=123,
            include_zero_positions=True,
            include_usd_values=True,
        )

    assert ok is True
    assert data == {
        "protocol": "aerodrome",
        "chain_id": CHAIN_ID_BASE,
        "tokenId": 123,
        "votes": [
            {
                "pool": pool.lp,
                "claimableFees": [],
                "claimableBribes": [],
            }
        ],
    }
    assert mock_get_vote_claimables.await_args.kwargs["token_id"] == 123
    assert mock_get_vote_claimables.await_args.kwargs["pool_metadata_by_address"] == {
        pool.lp.lower(): {
            "symbol": "AERO/USDC",
            "feeReward": pool.fee,
            "bribeReward": pool.bribe,
        }
    }
    assert mock_get_vote_claimables.await_args.kwargs["web3"] is mock_web3
    assert mock_get_vote_claimables.await_args.kwargs["voter_contract"] is voter
    assert mock_get_vote_claimables.await_args.kwargs["include_zero_positions"] is True
    assert mock_get_vote_claimables.await_args.kwargs["include_usd_values"] is True


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
async def test_can_vote_now_returns_epoch_metadata():
    adapter = AerodromeAdapter()
    last_voted = 123
    now = WEEK_SECONDS + 500

    voter = MagicMock()
    voter.functions.lastVoted = MagicMock(return_value=_mock_call(last_voted))

    mock_web3 = MagicMock()
    mock_web3.eth.get_block = AsyncMock(return_value={"timestamp": now})
    mock_web3.eth.contract = MagicMock(return_value=voter)

    with patch.object(
        aerodrome_common_module,
        "web3_from_chain_id",
        _web3_ctx(mock_web3),
    ):
        ok, data = await adapter.can_vote_now(token_id=7)

    assert ok is True
    assert data["can_vote"] is True
    assert data["last_voted"] == last_voted
    assert data["epoch_start"] == WEEK_SECONDS
    assert data["next_epoch_start"] == 2 * WEEK_SECONDS


@pytest.mark.asyncio
async def test_ve_locked_returns_structured_payload():
    adapter = AerodromeAdapter()
    ve = MagicMock()
    ve.functions.locked = MagicMock(return_value=_mock_call((42, 99, True)))

    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(return_value=ve)

    with patch.object(
        aerodrome_common_module,
        "web3_from_chain_id",
        _web3_ctx(mock_web3),
    ):
        ok, data = await adapter.ve_locked(token_id=1)

    assert ok is True
    assert data == {"amount": 42, "end": 99, "is_permanent": True}


@pytest.mark.asyncio
async def test_ve_locked_flattens_nested_tuple_payload():
    adapter = AerodromeAdapter()
    ve = MagicMock()
    ve.functions.locked = MagicMock(return_value=_mock_call(((42, 99, True),)))

    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(return_value=ve)

    with patch.object(
        aerodrome_common_module,
        "web3_from_chain_id",
        _web3_ctx(mock_web3),
    ):
        ok, data = await adapter.ve_locked(token_id=1)

    assert ok is True
    assert data == {"amount": 42, "end": 99, "is_permanent": True}


@pytest.mark.asyncio
async def test_ve_locked_returns_clean_error_on_exception():
    adapter = AerodromeAdapter()
    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(side_effect=RuntimeError("boom"))

    with patch.object(
        aerodrome_common_module,
        "web3_from_chain_id",
        _web3_ctx(mock_web3),
    ):
        ok, msg = await adapter.ve_locked(token_id=1)

    assert ok is False
    assert "boom" in msg


@pytest.mark.asyncio
async def test_estimate_ve_apr_percent_uses_aero_price(monkeypatch):
    adapter = AerodromeAdapter()

    async def _fake_token_price(_token: str) -> float:
        return 2.0

    async def _fake_token_decimals(_token: str) -> int:
        return 18

    monkeypatch.setattr(adapter, "token_price_usdc", _fake_token_price)
    monkeypatch.setattr(adapter, "token_decimals", _fake_token_decimals)

    ok, apr = await adapter.estimate_ve_apr_percent(
        usdc_per_ve=5.0,
        votes_raw=2 * 10**18,
        aero_locked_raw=10 * 10**18,
    )

    assert ok is True
    assert apr == pytest.approx(2600.0)


@pytest.mark.asyncio
async def test_estimate_votes_for_lock_caps_duration():
    adapter = AerodromeAdapter()

    ok, votes = await adapter.estimate_votes_for_lock(
        aero_amount_raw=4 * 10**18,
        lock_duration=10**12,
    )

    assert ok is True
    assert votes == 4 * 10**18


@pytest.mark.asyncio
async def test_sugar_all_batches_large_limit():
    adapter = AerodromeAdapter()

    def _addr(n: int) -> str:
        return "0x" + f"{n:040x}"

    def _row(i: int) -> tuple:
        addr = _addr(i + 1)
        token0 = _addr(i + 1001)
        token1 = _addr(i + 2001)
        return (
            addr,
            f"pool-{i}",
            18,
            100,
            0,
            0,
            0,
            token0,
            1,
            1,
            token1,
            1,
            1,
            _addr(i + 3001),
            1,
            True,
            _addr(i + 4001),
            _addr(i + 5001),
            _addr(i + 6001),
            0,
            _addr(i + 7001),
            30,
            5,
            0,
            0,
            1,
        )

    rows_page_1 = [_row(i) for i in range(300)]
    rows_page_2 = [_row(i) for i in range(300, 350)]
    seen_calls: list[tuple[int, int]] = []

    def _all(limit: int, offset: int):
        seen_calls.append((limit, offset))
        if (limit, offset) == (300, 10):
            return _mock_call(rows_page_1)
        if (limit, offset) == (50, 310):
            return _mock_call(rows_page_2)
        raise AssertionError(f"unexpected sugar.all({limit}, {offset})")

    sugar = MagicMock()
    sugar.functions.all = MagicMock(side_effect=_all)

    mock_web3 = MagicMock()
    mock_web3.eth.contract = MagicMock(return_value=sugar)

    with patch.object(
        aerodrome_adapter_module,
        "web3_from_chain_id",
        _web3_ctx(mock_web3),
    ):
        pools = await adapter.sugar_all(limit=350, offset=10)

    assert seen_calls == [(300, 10), (50, 310)]
    assert len(pools) == 350
    assert pools[0].symbol == "pool-0"
    assert pools[-1].symbol == "pool-349"


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
            assert limit == 300
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
async def test_v2_pool_helpers_and_emissions_apr(monkeypatch):
    adapter = AerodromeAdapter()
    token0 = "0x" + "11" * 20
    token1 = "0x" + "22" * 20
    reward = "0x" + "33" * 20
    pool = SugarPool(
        lp=FAKE_POOL,
        symbol="A/B",
        lp_decimals=18,
        lp_total_supply=1_000 * 10**18,
        pool_type=0,
        tick=0,
        sqrt_ratio=0,
        token0=token0,
        reserve0=2_000_000,
        staked0=0,
        token1=token1,
        reserve1=3 * 10**18,
        staked1=0,
        gauge=FAKE_GAUGE,
        gauge_liquidity=250 * 10**18,
        gauge_alive=True,
        fee="0x" + "44" * 20,
        bribe="0x" + "55" * 20,
        factory="0x" + "66" * 20,
        emissions_per_sec=10**18,
        emissions_token=reward,
        pool_fee_pips=30,
        unstaked_fee_pips=5,
        token0_fees=0,
        token1_fees=0,
        created_at=1,
    )

    async def _fake_decimals(token: str) -> int:
        if token.lower() == token0.lower():
            return 6
        return 18

    async def _fake_price(token: str) -> float | None:
        if token.lower() == token0.lower():
            return 1.0
        if token.lower() == token1.lower():
            return 2.0
        if token.lower() == reward.lower():
            return 4.0
        return None

    monkeypatch.setattr(adapter, "token_decimals", _fake_decimals)
    monkeypatch.setattr(adapter, "token_price_usdc", _fake_price)

    tvl = await adapter.v2_pool_tvl_usdc(pool)
    staked_tvl = await adapter.v2_staked_tvl_usdc(pool)
    apr = await adapter.v2_emissions_apr(pool)

    assert tvl == pytest.approx(8.0)
    assert staked_tvl == pytest.approx(2.0)
    assert apr == pytest.approx((SECONDS_PER_YEAR * 4.0) / 2.0)


@pytest.mark.asyncio
async def test_rank_v2_pools_by_emissions_apr(monkeypatch):
    adapter = AerodromeAdapter()
    token = "0x" + "11" * 20

    pool_a = SugarPool(
        lp="0x" + "aa" * 20,
        symbol="A/B",
        lp_decimals=18,
        lp_total_supply=100,
        pool_type=0,
        tick=0,
        sqrt_ratio=0,
        token0=token,
        reserve0=1,
        staked0=0,
        token1=token,
        reserve1=1,
        staked1=0,
        gauge="0x" + "01" * 20,
        gauge_liquidity=50,
        gauge_alive=True,
        fee="0x" + "02" * 20,
        bribe="0x" + "03" * 20,
        factory="0x" + "04" * 20,
        emissions_per_sec=200,
        emissions_token=token,
        pool_fee_pips=30,
        unstaked_fee_pips=5,
        token0_fees=0,
        token1_fees=0,
        created_at=1,
    )
    pool_b = SugarPool(
        lp="0x" + "bb" * 20,
        symbol="C/D",
        lp_decimals=18,
        lp_total_supply=100,
        pool_type=0,
        tick=0,
        sqrt_ratio=0,
        token0=token,
        reserve0=1,
        staked0=0,
        token1=token,
        reserve1=1,
        staked1=0,
        gauge="0x" + "05" * 20,
        gauge_liquidity=50,
        gauge_alive=True,
        fee="0x" + "06" * 20,
        bribe="0x" + "07" * 20,
        factory="0x" + "08" * 20,
        emissions_per_sec=100,
        emissions_token=token,
        pool_fee_pips=30,
        unstaked_fee_pips=5,
        token0_fees=0,
        token1_fees=0,
        created_at=1,
    )
    pool_filtered = SugarPool(
        lp="0x" + "cc" * 20,
        symbol="E/F",
        lp_decimals=18,
        lp_total_supply=100,
        pool_type=0,
        tick=0,
        sqrt_ratio=0,
        token0=token,
        reserve0=1,
        staked0=0,
        token1=token,
        reserve1=0,
        staked1=0,
        gauge=ZERO_ADDRESS,
        gauge_liquidity=0,
        gauge_alive=False,
        fee="0x" + "09" * 20,
        bribe="0x" + "0a" * 20,
        factory="0x" + "0b" * 20,
        emissions_per_sec=300,
        emissions_token=token,
        pool_fee_pips=30,
        unstaked_fee_pips=5,
        token0_fees=0,
        token1_fees=0,
        created_at=1,
    )

    async def _fake_list_pools(*, page_size: int, max_pools=None):
        assert page_size == 500
        assert max_pools is None
        return [pool_b, pool_filtered, pool_a]

    async def _fake_apr(pool: SugarPool) -> float | None:
        if pool.lp.lower() == pool_a.lp.lower():
            return 1.5
        if pool.lp.lower() == pool_b.lp.lower():
            return 0.5
        return None

    monkeypatch.setattr(adapter, "list_pools", _fake_list_pools)
    monkeypatch.setattr(adapter, "v2_emissions_apr", _fake_apr)

    ranked = await adapter.rank_v2_pools_by_emissions_apr(top_n=5)

    assert ranked == [(1.5, pool_a), (0.5, pool_b)]


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

    pools = [
        SugarPool(
            lp=lp_a,
            symbol="A/B",
            lp_decimals=18,
            lp_total_supply=100,
            pool_type=0,
            tick=0,
            sqrt_ratio=0,
            token0=lp_a,
            reserve0=1,
            staked0=1,
            token1=lp_b,
            reserve1=1,
            staked1=1,
            gauge=lp_a,
            gauge_liquidity=1,
            gauge_alive=True,
            fee=lp_a,
            bribe=lp_a,
            factory=lp_a,
            emissions_per_sec=0,
            emissions_token=lp_a,
            pool_fee_pips=0,
            unstaked_fee_pips=0,
            token0_fees=0,
            token1_fees=0,
            created_at=1,
        ),
        SugarPool(
            lp=lp_b,
            symbol="B/C",
            lp_decimals=18,
            lp_total_supply=100,
            pool_type=0,
            tick=0,
            sqrt_ratio=0,
            token0=lp_b,
            reserve0=1,
            staked0=1,
            token1=lp_a,
            reserve1=1,
            staked1=1,
            gauge=lp_b,
            gauge_liquidity=1,
            gauge_alive=True,
            fee=lp_b,
            bribe=lp_b,
            factory=lp_b,
            emissions_per_sec=0,
            emissions_token=lp_b,
            pool_fee_pips=0,
            unstaked_fee_pips=0,
            token0_fees=0,
            token1_fees=0,
            created_at=1,
        ),
    ]

    async def _fake_list_pools(*, max_pools: int | None = None) -> list[SugarPool]:
        assert max_pools == 1000
        return pools

    async def _fake_epochs_by_address(
        *,
        pool: str,
        limit: int,
        offset: int,
    ) -> list[SugarEpoch]:
        assert limit == 1
        assert offset == 0
        if pool.lower() == lp_a.lower():
            return [epoch_a_latest, epoch_a_old]
        if pool.lower() == lp_b.lower():
            return [epoch_b_latest]
        return []

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

    monkeypatch.setattr(adapter, "list_pools", _fake_list_pools)
    monkeypatch.setattr(adapter, "sugar_epochs_by_address", _fake_epochs_by_address)
    monkeypatch.setattr(adapter, "epoch_total_incentives_usdc", _fake_total_usdc)

    ranked = await adapter.rank_pools_by_usdc_per_ve(top_n=10, limit=1000)

    assert len(ranked) == 2
    assert ranked[0][1].lp.lower() == lp_a.lower()
    assert ranked[1][1].lp.lower() == lp_b.lower()
    assert ranked[0][0] > ranked[1][0]


@pytest.mark.asyncio
async def test_rank_pools_by_usdc_per_ve_uses_pool_epoch_lookup(monkeypatch):
    adapter = AerodromeAdapter()
    lp_a = "0x" + "11" * 20
    lp_b = "0x" + "22" * 20
    token = "0x" + "33" * 20

    pools = [
        SugarPool(
            lp=lp_a,
            symbol="A/B",
            lp_decimals=18,
            lp_total_supply=100,
            pool_type=0,
            tick=0,
            sqrt_ratio=0,
            token0=token,
            reserve0=1,
            staked0=1,
            token1=token,
            reserve1=1,
            staked1=1,
            gauge="0x" + "44" * 20,
            gauge_liquidity=1,
            gauge_alive=True,
            fee="0x" + "55" * 20,
            bribe="0x" + "66" * 20,
            factory="0x" + "77" * 20,
            emissions_per_sec=0,
            emissions_token=token,
            pool_fee_pips=30,
            unstaked_fee_pips=5,
            token0_fees=0,
            token1_fees=0,
            created_at=1,
        ),
        SugarPool(
            lp=lp_b,
            symbol="C/D",
            lp_decimals=18,
            lp_total_supply=100,
            pool_type=0,
            tick=0,
            sqrt_ratio=0,
            token0=token,
            reserve0=1,
            staked0=1,
            token1=token,
            reserve1=1,
            staked1=1,
            gauge="0x" + "88" * 20,
            gauge_liquidity=1,
            gauge_alive=True,
            fee="0x" + "99" * 20,
            bribe="0x" + "aa" * 20,
            factory="0x" + "bb" * 20,
            emissions_per_sec=0,
            emissions_token=token,
            pool_fee_pips=30,
            unstaked_fee_pips=5,
            token0_fees=0,
            token1_fees=0,
            created_at=1,
        ),
    ]

    epoch_a = SugarEpoch(
        ts=100,
        lp=lp_a,
        votes=10,
        emissions=0,
        bribes=[],
        fees=[],
    )
    epoch_b = SugarEpoch(
        ts=100,
        lp=lp_b,
        votes=20,
        emissions=0,
        bribes=[],
        fees=[],
    )

    async def _unexpected_epochs_latest(*, limit: int, offset: int) -> list[SugarEpoch]:
        raise AssertionError("sugar_epochs_latest should not be called")

    async def _fake_list_pools(*, max_pools: int | None = None) -> list[SugarPool]:
        assert max_pools == 2
        return pools

    async def _fake_epochs_by_address(
        *,
        pool: str,
        limit: int,
        offset: int,
    ) -> list[SugarEpoch]:
        assert limit == 1
        assert offset == 0
        if pool.lower() == lp_a.lower():
            return [epoch_a]
        if pool.lower() == lp_b.lower():
            return [epoch_b]
        return []

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

    monkeypatch.setattr(adapter, "sugar_epochs_latest", _unexpected_epochs_latest)
    monkeypatch.setattr(adapter, "list_pools", _fake_list_pools)
    monkeypatch.setattr(adapter, "sugar_epochs_by_address", _fake_epochs_by_address)
    monkeypatch.setattr(adapter, "epoch_total_incentives_usdc", _fake_total_usdc)

    ranked = await adapter.rank_pools_by_usdc_per_ve(top_n=10, limit=2)

    assert len(ranked) == 2
    assert ranked[0][1].lp.lower() == lp_a.lower()
    assert ranked[1][1].lp.lower() == lp_b.lower()
    assert ranked[0][0] > ranked[1][0]
    assert adapter._latest_epochs_for_ranking_stats == {
        "requested_limit": 2,
        "pool_count": 2,
        "batch_size": 10,
        "rpc_calls": 2,
        "epochs_found": 2,
        "empty_pools": 0,
        "failed_pools": 0,
    }


@pytest.mark.asyncio
async def test_latest_epochs_for_ranking_skips_failed_and_empty_pools(monkeypatch):
    adapter = AerodromeAdapter()
    lp_a = "0x1111111111111111111111111111111111111111"
    lp_b = "0x2222222222222222222222222222222222222222"
    lp_c = "0x3333333333333333333333333333333333333333"

    pools = [
        SugarPool(
            lp=lp_a,
            symbol="pool-a",
            lp_decimals=18,
            lp_total_supply=1,
            pool_type=0,
            tick=0,
            sqrt_ratio=0,
            token0=lp_a,
            reserve0=0,
            staked0=0,
            token1=lp_b,
            reserve1=0,
            staked1=0,
            gauge=lp_a,
            gauge_liquidity=0,
            gauge_alive=True,
            fee=lp_a,
            bribe=lp_a,
            factory=lp_a,
            emissions_per_sec=0,
            emissions_token=lp_a,
            pool_fee_pips=0,
            unstaked_fee_pips=0,
            token0_fees=0,
            token1_fees=0,
            created_at=1,
        ),
        SugarPool(
            lp=lp_b,
            symbol="pool-b",
            lp_decimals=18,
            lp_total_supply=1,
            pool_type=0,
            tick=0,
            sqrt_ratio=0,
            token0=lp_a,
            reserve0=0,
            staked0=0,
            token1=lp_b,
            reserve1=0,
            staked1=0,
            gauge=lp_b,
            gauge_liquidity=0,
            gauge_alive=True,
            fee=lp_b,
            bribe=lp_b,
            factory=lp_b,
            emissions_per_sec=0,
            emissions_token=lp_b,
            pool_fee_pips=0,
            unstaked_fee_pips=0,
            token0_fees=0,
            token1_fees=0,
            created_at=1,
        ),
        SugarPool(
            lp=lp_c,
            symbol="pool-c",
            lp_decimals=18,
            lp_total_supply=1,
            pool_type=0,
            tick=0,
            sqrt_ratio=0,
            token0=lp_a,
            reserve0=0,
            staked0=0,
            token1=lp_c,
            reserve1=0,
            staked1=0,
            gauge=lp_c,
            gauge_liquidity=0,
            gauge_alive=True,
            fee=lp_c,
            bribe=lp_c,
            factory=lp_c,
            emissions_per_sec=0,
            emissions_token=lp_c,
            pool_fee_pips=0,
            unstaked_fee_pips=0,
            token0_fees=0,
            token1_fees=0,
            created_at=1,
        ),
    ]

    epoch_a = SugarEpoch(
        ts=100,
        lp=lp_a,
        votes=10,
        emissions=0,
        bribes=[],
        fees=[],
    )

    async def _fake_list_pools(*, max_pools: int | None = None) -> list[SugarPool]:
        assert max_pools == 3
        return pools

    async def _fake_epochs_by_address(
        *,
        pool: str,
        limit: int,
        offset: int,
    ) -> list[SugarEpoch]:
        assert limit == 1
        assert offset == 0
        if pool.lower() == lp_a.lower():
            return [epoch_a]
        if pool.lower() == lp_b.lower():
            return []
        raise RuntimeError("boom")

    monkeypatch.setattr(adapter, "list_pools", _fake_list_pools)
    monkeypatch.setattr(adapter, "sugar_epochs_by_address", _fake_epochs_by_address)

    epochs = await adapter._latest_epochs_for_ranking(limit=3)

    assert [epoch.lp.lower() for epoch in epochs] == [lp_a.lower()]
    assert adapter._latest_epochs_for_ranking_stats == {
        "requested_limit": 3,
        "pool_count": 3,
        "batch_size": 10,
        "rpc_calls": 3,
        "epochs_found": 1,
        "empty_pools": 1,
        "failed_pools": 1,
    }
