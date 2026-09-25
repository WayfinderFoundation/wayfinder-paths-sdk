import asyncio
import math
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from wayfinder_paths.core.constants.base import (
    GAS_BUFFER_MULTIPLIER,
    MAX_BASE_FEE_GROWTH_MULTIPLIER,
    SUGGESTED_GAS_PRICE_MULTIPLIER,
    SUGGESTED_PRIORITY_FEE_MULTIPLIER,
)
from wayfinder_paths.core.utils import transaction as tx

ADDRESS = "0x" + "11" * 20
HASH = "0x" + "ab" * 32
RECEIPT = {"status": 1, "blockNumber": 100}


def rpc(*, error: Exception | None = None, nonce: int = 3) -> MagicMock:
    web3 = MagicMock()
    web3.eth.get_transaction_count = AsyncMock(return_value=nonce, side_effect=error)
    web3.eth.get_block = AsyncMock(
        return_value={"baseFeePerGas": 100}, side_effect=error
    )
    web3.eth.fee_history = AsyncMock(return_value={"reward": [[5]]}, side_effect=error)
    web3.eth.wait_for_transaction_receipt = AsyncMock(
        return_value=RECEIPT, side_effect=error
    )
    web3.eth.height = AsyncMock(return_value=102, side_effect=error)
    web3.eth.price = AsyncMock(return_value=10, side_effect=error)
    type(web3.eth).block_number = PropertyMock(side_effect=web3.eth.height)
    type(web3.eth).gas_price = PropertyMock(side_effect=web3.eth.price)
    return web3


def pool(monkeypatch: pytest.MonkeyPatch, *nodes: MagicMock) -> None:
    context = MagicMock()
    context.return_value.__aenter__.return_value = list(nodes)
    monkeypatch.setattr(tx, "web3s_from_chain_id", context)


@pytest.mark.asyncio
async def test_nonce_uses_highest_healthy_pending_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nodes = [rpc(nonce=4), rpc(error=RuntimeError("403 quota")), rpc(nonce=9)]
    pool(monkeypatch, *nodes)
    result = await tx.nonce_transaction({"chainId": 8453, "from": ADDRESS})
    assert result["nonce"] == 9
    for node in nodes:
        node.eth.get_transaction_count.assert_awaited_once_with(
            ADDRESS, block_identifier="pending"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("chain_id", [1, 56])
async def test_fees_ignore_failed_rpc(
    monkeypatch: pytest.MonkeyPatch, chain_id: int
) -> None:
    pool(monkeypatch, rpc(error=RuntimeError("403 quota")), rpc())
    result = await tx.gas_price_transaction({"chainId": chain_id})
    if chain_id == 56:
        assert result["gasPrice"] == int(10 * SUGGESTED_GAS_PRICE_MULTIPLIER)
    else:
        priority = int(5 * SUGGESTED_PRIORITY_FEE_MULTIPLIER)
        assert result["maxPriorityFeePerGas"] == priority
        assert (
            result["maxFeePerGas"]
            == int(100 * MAX_BASE_FEE_GROWTH_MULTIPLIER) + priority
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["nonce_transaction", "gas_price_transaction"])
@pytest.mark.parametrize("empty", [False, True])
async def test_no_usable_rpc_fails_closed(
    monkeypatch: pytest.MonkeyPatch, operation: str, empty: bool
) -> None:
    pool(monkeypatch, *([] if empty else [rpc(error=RuntimeError("offline"))]))
    with pytest.raises(RuntimeError, match="All RPCs failed"):
        await getattr(tx, operation)({"chainId": 1, "from": ADDRESS})


@pytest.mark.asyncio
async def test_hung_nonce_read_is_bounded_and_drained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped = asyncio.Event()

    async def hang(*args: Any, **kwargs: Any) -> int:
        try:
            await asyncio.Event().wait()
            return 0
        finally:
            stopped.set()

    hung = rpc()
    hung.eth.get_transaction_count.side_effect = hang
    pool(monkeypatch, hung, rpc(nonce=8))
    monkeypatch.setattr(tx, "_RPC_READ_TIMEOUT", 0.01)
    result = await tx.nonce_transaction({"chainId": 1, "from": ADDRESS})
    assert result["nonce"] == 8
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_gas_estimate_uses_healthy_rpc_when_peer_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped = asyncio.Event()

    async def hang(*args: Any, **kwargs: Any) -> int:
        try:
            await asyncio.Event().wait()
            return 0
        finally:
            stopped.set()

    hung, healthy = rpc(), rpc()
    hung.eth.estimate_gas = AsyncMock(side_effect=hang)
    healthy.eth.estimate_gas = AsyncMock(return_value=21000)
    pool(monkeypatch, hung, healthy)
    monkeypatch.setattr(tx, "_RPC_READ_TIMEOUT", 0.01)
    monkeypatch.setattr(tx, "_is_gorlami_fork_chain", lambda _: False)
    result = await tx.gas_limit_transaction({"chainId": 8453, "from": ADDRESS})
    assert result["gas"] == math.ceil(21000 * GAS_BUFFER_MULTIPLIER)
    assert stopped.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmations", [0, 1, 3])
async def test_fast_failure_does_not_cancel_healthy_receipt(
    monkeypatch: pytest.MonkeyPatch, confirmations: int
) -> None:
    failed = rpc(error=RuntimeError("403 quota"))
    healthy = rpc()

    async def delayed(*args: Any, **kwargs: Any) -> dict[str, int]:
        await asyncio.sleep(0.01)
        return RECEIPT

    healthy.eth.wait_for_transaction_receipt.side_effect = delayed
    pool(monkeypatch, failed, healthy)
    result = await tx.wait_for_transaction_receipt(
        8453, HASH, confirmations=confirmations
    )
    assert result == RECEIPT
    if confirmations <= 1:
        healthy.eth.height.assert_not_awaited()
    else:
        healthy.eth.height.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_height_read_does_not_poison_other_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = rpc()
    failed.eth.height.side_effect = RuntimeError("403 quota")
    healthy = rpc()
    healthy.eth.height.side_effect = [100, 101, 102]
    pool(monkeypatch, failed, healthy)
    assert await tx.wait_for_transaction_receipt(8453, HASH, poll_interval=0) == RECEIPT
    assert healthy.eth.height.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "revert", "timeout", "cancel"])
async def test_receipt_wait_always_drains_pending_tasks(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    started, stopped = asyncio.Event(), asyncio.Event()

    async def hang(*args: Any, **kwargs: Any) -> dict[str, int]:
        started.set()
        try:
            await asyncio.Event().wait()
            return RECEIPT
        finally:
            stopped.set()

    hung = rpc()
    hung.eth.wait_for_transaction_receipt.side_effect = hang
    healthy = rpc()
    if outcome == "revert":
        healthy.eth.wait_for_transaction_receipt.return_value = {**RECEIPT, "status": 0}
    pool(monkeypatch, hung, *([healthy] if outcome in ("success", "revert") else []))
    # Small deadline exercises the overall bound without waiting 300 seconds.
    task = asyncio.create_task(
        tx.wait_for_transaction_receipt(8453, HASH, timeout=0.05)
    )
    await started.wait()
    if outcome == "cancel":
        task.cancel()
    error = {
        "revert": tx.TransactionRevertedError,
        "timeout": TimeoutError,
        "cancel": asyncio.CancelledError,
    }.get(outcome)
    if error:
        with pytest.raises(error):
            await task
    else:
        assert await task == RECEIPT
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_confirmation_height_poll_has_overall_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = rpc()
    node.eth.height.return_value = 100
    pool(monkeypatch, node)
    with pytest.raises(TimeoutError):
        await tx.wait_for_transaction_receipt(
            8453, HASH, timeout=0.02, poll_interval=0.001
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", [None, {"blockNumber": 100}])
async def test_unusable_receipts_do_not_count_as_success(
    monkeypatch: pytest.MonkeyPatch, receipt: dict[str, int] | None
) -> None:
    node = rpc()
    node.eth.wait_for_transaction_receipt.return_value = receipt
    pool(monkeypatch, rpc(error=RuntimeError("403 quota")), node)
    with pytest.raises(RuntimeError, match="All RPCs failed"):
        await tx.wait_for_transaction_receipt(8453, HASH)


@pytest.mark.asyncio
@pytest.mark.parametrize("sponsored", [False, True])
async def test_confirmation_failure_preserves_hash_without_resubmitting(
    monkeypatch: pytest.MonkeyPatch, sponsored: bool
) -> None:
    for name in ("gas_limit_transaction", "nonce_transaction", "gas_price_transaction"):
        monkeypatch.setattr(
            tx, name, AsyncMock(side_effect=lambda transaction: transaction)
        )
    monkeypatch.setattr(tx, "_is_gorlami_fork_chain", lambda _: False)
    monkeypatch.setattr(tx, "sponsorship_enabled", AsyncMock(return_value=sponsored))
    broadcast = AsyncMock(return_value=HASH.removeprefix("0x"))
    sponsor = AsyncMock(return_value=HASH)
    monkeypatch.setattr(tx, "broadcast_transaction", broadcast)
    monkeypatch.setattr(tx, "send_sponsored_transaction", sponsor)
    failure = TimeoutError("RPC unavailable")
    monkeypatch.setattr(
        tx, "wait_for_transaction_receipt", AsyncMock(side_effect=failure)
    )
    signer = AsyncMock(return_value=b"signed")
    signer.wallet_address = ADDRESS if sponsored else None

    with pytest.raises(tx.TransactionConfirmationError) as raised:
        await tx.send_transaction({"chainId": 8453, "from": ADDRESS}, signer)

    assert raised.value.txn_hash == HASH
    assert raised.value.chain_id == 8453
    assert raised.value.__cause__ is failure
    assert "before retrying" in str(raised.value)
    assert sponsor.await_count == int(sponsored)
    assert broadcast.await_count == int(not sponsored)
    assert signer.await_count == int(not sponsored)
