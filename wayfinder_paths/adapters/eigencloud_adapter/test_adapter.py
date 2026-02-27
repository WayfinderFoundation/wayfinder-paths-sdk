from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from web3 import Web3
from web3._utils.events import event_abi_to_log_topic

from wayfinder_paths.adapters.eigencloud_adapter.adapter import EigenCloudAdapter
from wayfinder_paths.core.constants.contracts import (
    EIGEN_TOKEN,
    EIGENCLOUD_BEACON_CHAIN_ETH_STRATEGY_SENTINEL,
    EIGENCLOUD_DELEGATION_MANAGER,
    EIGENCLOUD_EIGEN_STRATEGY,
    EIGENCLOUD_REWARDS_COORDINATOR,
    EIGENCLOUD_STRATEGY_MANAGER,
    ZERO_ADDRESS,
)
from wayfinder_paths.core.constants.eigencloud_abi import IDELEGATION_MANAGER_ABI

FAKE_WALLET = "0x1234567890123456789012345678901234567890"
FAKE_STRATEGY = "0x1111111111111111111111111111111111111111"
FAKE_TOKEN = "0x2222222222222222222222222222222222222222"


def _mock_call(return_value):
    return MagicMock(call=AsyncMock(return_value=return_value))


def _make_strategy_contract(*, underlying_token=FAKE_TOKEN):
    contract = MagicMock()
    contract.functions.underlyingToken = MagicMock(
        return_value=_mock_call(underlying_token)
    )
    contract.functions.totalShares = MagicMock(return_value=_mock_call(123))
    contract.functions.sharesToUnderlyingView = MagicMock(return_value=_mock_call(456))
    return contract


def _make_strategy_manager_contract(*, whitelisted=True):
    contract = MagicMock()
    contract.functions.strategyIsWhitelistedForDeposit = MagicMock(
        return_value=_mock_call(whitelisted)
    )
    return contract


def _make_delegation_manager_contract(
    *,
    queued_withdrawal=None,
    deposited=None,
    withdrawable=None,
    is_delegated=False,
    delegated_to=ZERO_ADDRESS,
):
    contract = MagicMock()
    contract.functions.getQueuedWithdrawal = MagicMock(
        return_value=_mock_call(queued_withdrawal)
    )
    contract.functions.queueWithdrawals = MagicMock(
        return_value=_mock_call([b"\x11" * 32])
    )
    contract.functions.getDepositedShares = MagicMock(
        return_value=_mock_call(deposited)
    )
    contract.functions.getWithdrawableShares = MagicMock(
        return_value=_mock_call(withdrawable)
    )
    contract.functions.isDelegated = MagicMock(return_value=_mock_call(is_delegated))
    contract.functions.delegatedTo = MagicMock(return_value=_mock_call(delegated_to))
    return contract


@pytest.fixture
def adapter():
    return EigenCloudAdapter(wallet_address=FAKE_WALLET)


@pytest.fixture
def adapter_with_signer():
    return EigenCloudAdapter(
        sign_callback=AsyncMock(return_value="0xdeadbeef"), wallet_address=FAKE_WALLET
    )


@pytest.fixture
def adapter_no_wallet():
    return EigenCloudAdapter()


def test_adapter_type(adapter):
    assert adapter.adapter_type == "EIGENCLOUD"


def test_default_addresses(adapter):
    assert adapter.chain_id == 1
    assert adapter.strategy_manager == EIGENCLOUD_STRATEGY_MANAGER
    assert adapter.delegation_manager == EIGENCLOUD_DELEGATION_MANAGER
    assert adapter.rewards_coordinator == EIGENCLOUD_REWARDS_COORDINATOR


@pytest.mark.asyncio
async def test_deposit_requires_wallet(adapter_no_wallet):
    ok, msg = await adapter_no_wallet.deposit(strategy=FAKE_STRATEGY, amount=1)
    assert ok is False
    assert "wallet_address" in str(msg).lower()


@pytest.mark.asyncio
async def test_deposit_requires_sign_callback(adapter):
    ok, msg = await adapter.deposit(strategy=FAKE_STRATEGY, amount=1)
    assert ok is False
    assert "sign_callback" in str(msg).lower()


@pytest.mark.asyncio
async def test_deposit_rejects_non_positive_amount(adapter_with_signer):
    ok, msg = await adapter_with_signer.deposit(strategy=FAKE_STRATEGY, amount=0)
    assert ok is False
    assert "positive" in str(msg).lower()


@pytest.mark.asyncio
async def test_deposit_discovers_underlying_and_approves_strategy_manager(
    adapter_with_signer,
):
    mock_web3 = MagicMock()
    sm = _make_strategy_manager_contract(whitelisted=True)
    strat = _make_strategy_contract(underlying_token=FAKE_TOKEN)

    def contract_side_effect(*, address=None, abi=None):
        if str(address).lower() == EIGENCLOUD_STRATEGY_MANAGER.lower():
            return sm
        if str(address).lower() == FAKE_STRATEGY.lower():
            return strat
        raise AssertionError(f"unexpected contract address: {address}")

    mock_web3.eth.contract = MagicMock(side_effect=contract_side_effect)

    @asynccontextmanager
    async def mock_web3_ctx(_chain_id):
        yield mock_web3

    with (
        patch(
            "wayfinder_paths.adapters.eigencloud_adapter.adapter.web3_from_chain_id",
            mock_web3_ctx,
        ),
        patch(
            "wayfinder_paths.adapters.eigencloud_adapter.adapter.ensure_allowance",
            new_callable=AsyncMock,
            return_value=(True, "0xapprove"),
        ) as mock_allow,
        patch(
            "wayfinder_paths.adapters.eigencloud_adapter.adapter.encode_call",
            new_callable=AsyncMock,
            return_value={"to": "0x", "from": FAKE_WALLET, "data": "0x"},
        ),
        patch(
            "wayfinder_paths.adapters.eigencloud_adapter.adapter.send_transaction",
            new_callable=AsyncMock,
            return_value="0xtx",
        ),
    ):
        ok, res = await adapter_with_signer.deposit(strategy=FAKE_STRATEGY, amount=123)

    assert ok is True
    assert res["tx_hash"] == "0xtx"
    assert res["approve_tx_hash"] == "0xapprove"
    mock_allow.assert_awaited()
    kwargs = mock_allow.await_args.kwargs
    assert kwargs["spender"] == EIGENCLOUD_STRATEGY_MANAGER
    assert kwargs["token_address"].lower() == FAKE_TOKEN.lower()


@pytest.mark.asyncio
async def test_queue_withdrawals_validates_lengths(adapter_with_signer):
    ok, msg = await adapter_with_signer.queue_withdrawals(
        strategies=[FAKE_STRATEGY], deposit_shares=[1, 2]
    )
    assert ok is False
    assert "equal length" in str(msg).lower()


@pytest.mark.asyncio
async def test_queue_withdrawals_includes_withdrawal_roots(adapter_with_signer):
    with (
        patch(
            "wayfinder_paths.adapters.eigencloud_adapter.adapter.encode_call",
            new_callable=AsyncMock,
            return_value={"to": "0x", "from": FAKE_WALLET, "data": "0x"},
        ),
        patch(
            "wayfinder_paths.adapters.eigencloud_adapter.adapter.send_transaction",
            new_callable=AsyncMock,
            return_value="0xtx",
        ),
        patch.object(
            adapter_with_signer,
            "get_withdrawal_roots_from_tx_hash",
            new_callable=AsyncMock,
            return_value=(True, ["0x" + ("11" * 32)]),
        ),
    ):
        ok, res = await adapter_with_signer.queue_withdrawals(
            strategies=[FAKE_STRATEGY], deposit_shares=[123]
        )

    assert ok is True
    assert res["tx_hash"] == "0xtx"
    assert res["withdrawal_roots"] == ["0x" + ("11" * 32)]


@pytest.mark.asyncio
async def test_get_withdrawal_roots_from_tx_hash_decodes_slashing_events(adapter):
    event_abi = next(
        i
        for i in IDELEGATION_MANAGER_ABI
        if i.get("type") == "event" and i.get("name") == "SlashingWithdrawalQueued"
    )

    w3 = Web3()
    topic0 = event_abi_to_log_topic(event_abi)

    withdrawal_root = b"\x11" * 32
    withdrawal = (
        FAKE_WALLET,
        ZERO_ADDRESS,
        FAKE_WALLET,
        1,
        100,
        [FAKE_STRATEGY],
        [10],
    )
    shares_to_withdraw = [10]

    data = w3.codec.encode(
        [
            "bytes32",
            "(address,address,address,uint256,uint32,address[],uint256[])",
            "uint256[]",
        ],
        [withdrawal_root, withdrawal, shares_to_withdraw],
    )

    receipt = {
        "logs": [
            {
                "address": EIGENCLOUD_DELEGATION_MANAGER,
                "topics": [topic0],
                "data": data,
                "logIndex": 0,
                "transactionIndex": 0,
                "transactionHash": b"\x00" * 32,
                "blockHash": b"\x00" * 32,
                "blockNumber": 1,
            }
        ]
    }

    mock_web3 = MagicMock()
    mock_web3.codec = w3.codec
    mock_web3.eth.get_transaction_receipt = AsyncMock(return_value=receipt)

    @asynccontextmanager
    async def mock_web3_ctx(_chain_id):
        yield mock_web3

    with patch(
        "wayfinder_paths.adapters.eigencloud_adapter.adapter.web3_from_chain_id",
        mock_web3_ctx,
    ):
        ok, roots = await adapter.get_withdrawal_roots_from_tx_hash(tx_hash="0xtx")

    assert ok is True
    assert roots == ["0x" + ("11" * 32)]


@pytest.mark.asyncio
async def test_complete_withdrawal_builds_tokens_list(adapter_with_signer):
    mock_web3 = MagicMock()
    withdrawal = (
        FAKE_WALLET,
        ZERO_ADDRESS,
        FAKE_WALLET,
        1,
        100,
        [
            FAKE_STRATEGY,
            EIGENCLOUD_BEACON_CHAIN_ETH_STRATEGY_SENTINEL,
            EIGENCLOUD_EIGEN_STRATEGY,
        ],
        [10, 20, 30],
    )
    shares = [10, 20, 30]
    dm = _make_delegation_manager_contract(queued_withdrawal=(withdrawal, shares))
    strat = _make_strategy_contract(underlying_token=FAKE_TOKEN)

    def contract_side_effect(*, address=None, abi=None):
        addr = str(address).lower()
        if addr == EIGENCLOUD_DELEGATION_MANAGER.lower():
            return dm
        if addr == FAKE_STRATEGY.lower():
            return strat
        # adapter should not call underlyingToken on sentinel or eigen strategy
        if addr in (
            EIGENCLOUD_BEACON_CHAIN_ETH_STRATEGY_SENTINEL.lower(),
            EIGENCLOUD_EIGEN_STRATEGY.lower(),
        ):
            return MagicMock()
        raise AssertionError(f"unexpected contract address: {address}")

    mock_web3.eth.contract = MagicMock(side_effect=contract_side_effect)

    @asynccontextmanager
    async def mock_web3_ctx(_chain_id):
        yield mock_web3

    captured = {}

    async def _encode_call(**kwargs):
        captured.update(kwargs)
        return {"to": "0x", "from": FAKE_WALLET, "data": "0x"}

    with (
        patch(
            "wayfinder_paths.adapters.eigencloud_adapter.adapter.web3_from_chain_id",
            mock_web3_ctx,
        ),
        patch(
            "wayfinder_paths.adapters.eigencloud_adapter.adapter.encode_call",
            new_callable=AsyncMock,
            side_effect=_encode_call,
        ),
        patch(
            "wayfinder_paths.adapters.eigencloud_adapter.adapter.send_transaction",
            new_callable=AsyncMock,
            return_value="0xtx",
        ),
    ):
        ok, res = await adapter_with_signer.complete_withdrawal(
            withdrawal_root="0x" + ("aa" * 32),
            receive_as_tokens=True,
        )

    assert ok is True
    assert res["tx_hash"] == "0xtx"
    args = captured["args"]
    tokens = args[1]
    assert tokens[0].lower() == FAKE_TOKEN.lower()
    assert tokens[1].lower() == ZERO_ADDRESS.lower()
    assert tokens[2].lower() == EIGEN_TOKEN.lower()


@pytest.mark.asyncio
async def test_get_pos_happy_path(adapter):
    mock_web3 = MagicMock()
    dm = _make_delegation_manager_contract(
        deposited=([FAKE_STRATEGY], [100]),
        withdrawable=([90], [100]),
        is_delegated=True,
        delegated_to="0x3333333333333333333333333333333333333333",
    )
    strat = MagicMock()
    strat.functions.underlyingToken = MagicMock(return_value=_mock_call(FAKE_TOKEN))
    strat.functions.sharesToUnderlyingView = MagicMock(return_value=_mock_call(200))

    def contract_side_effect(*, address=None, abi=None):
        addr = str(address).lower()
        if addr == EIGENCLOUD_DELEGATION_MANAGER.lower():
            return dm
        if addr == FAKE_STRATEGY.lower():
            return strat
        raise AssertionError(f"unexpected contract address: {address}")

    mock_web3.eth.contract = MagicMock(side_effect=contract_side_effect)

    @asynccontextmanager
    async def mock_web3_ctx(_chain_id):
        yield mock_web3

    with patch(
        "wayfinder_paths.adapters.eigencloud_adapter.adapter.web3_from_chain_id",
        mock_web3_ctx,
    ):
        ok, data = await adapter.get_pos(account=FAKE_WALLET, include_usd=False)

    assert ok is True
    assert data["isDelegated"] is True
    assert len(data["positions"]) == 1
    pos = data["positions"][0]
    assert pos["strategy"].lower() == FAKE_STRATEGY.lower()
    assert pos["deposit_shares"] == 100
    assert pos["withdrawable_shares"] == 90
    assert pos["underlying"].lower() == FAKE_TOKEN.lower()
