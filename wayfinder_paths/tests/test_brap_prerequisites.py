from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from eth_abi import encode
from eth_utils import to_checksum_address

from wayfinder_paths.adapters.brap_adapter import adapter as adapter_module
from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.utils import transaction as transaction_module
from wayfinder_paths.core.utils.transaction import TransactionRevertedError
from wayfinder_paths.mcp.tools import execute as execute_module

CHAIN_ID = 4663
OWNER = "0x000000000000000000000000000000000000dEaD"
TOKEN = "0x73c2de14c7fa0a57cc2d9722b959ea70b881ffe4"
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
ROUTER = "0x8876789976DEcbFcBBBe364623c63652Db8C0904"


@dataclass
class SwapCase:
    quote: dict[str, Any]
    token: dict[str, Any]
    send: AsyncMock
    approve: AsyncMock
    record: AsyncMock
    execute: Callable[[bool], Coroutine[Any, Any, tuple[bool, Any]]]


@pytest.fixture(params=["mcp", "adapter"])
def swap_case(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> SwapCase:
    # Same two-step allowance contract as the direct v4 solver: token -> Permit2,
    # then Permit2 -> router. Never replace these with token -> router approval.
    quote: dict[str, Any] = {
        "provider": "uniswap_v4",
        "input_amount": "1000000",
        "output_amount": "2000000",
        "calldata": {
            "chainId": CHAIN_ID,
            "from": OWNER.lower(),
            "to": ROUTER,
            "data": "0x1234",
            "value": "0x0",
        },
        "prerequisite_transactions": [
            {
                "chainId": CHAIN_ID,
                "from": OWNER.lower(),
                "to": TOKEN,
                "data": "0x095ea7b3"
                + encode(["address", "uint256"], [PERMIT2, 2**256 - 1]).hex(),
                "value": "0x0",
                "description": "Approve Permit2 to move the token",
            },
            {
                "chainId": CHAIN_ID,
                "from": OWNER,
                "to": PERMIT2,
                "data": "0x87517c45"
                + encode(
                    ["address", "address", "uint160", "uint48"],
                    [TOKEN, ROUTER, 2**160 - 1, 2**48 - 1],
                ).hex(),
                "value": "0",
                "description": "Authorize the router through Permit2",
            },
        ],
    }
    token = {
        "address": TOKEN,
        "chain_id": CHAIN_ID,
        "chain": {"id": CHAIN_ID},
        "decimals": 6,
        "symbol": "BOOMER",
    }
    destination = {**token, "address": ZERO_ADDRESS, "symbol": "ETH"}
    send = AsyncMock(return_value="0xtest")
    approve = AsyncMock(return_value=(True, "0xapproval"))
    record = AsyncMock(return_value={})
    signer = AsyncMock(return_value=b"signed")
    signer.wallet_address = None
    monkeypatch.setattr(execute_module, "send_transaction", send)
    monkeypatch.setattr(execute_module, "ensure_allowance", approve)
    monkeypatch.setattr(execute_module, "_annotate_profile", lambda **_: None)
    monkeypatch.setattr(
        execute_module, "_token_balance", AsyncMock(return_value=10**18)
    )
    monkeypatch.setattr(
        execute_module,
        "get_wallet_signing_callback_for_chain",
        AsyncMock(return_value=(signer, OWNER)),
    )
    monkeypatch.setattr(
        execute_module,
        "find_wallet_leg_for_chain",
        AsyncMock(return_value={"address": OWNER}),
    )
    monkeypatch.setattr(
        execute_module.TokenResolver,
        "resolve_token_meta",
        AsyncMock(side_effect=[token, destination]),
    )
    monkeypatch.setattr(
        execute_module.BRAP_CLIENT,
        "get_quote",
        AsyncMock(return_value={"best_quote": quote}),
    )
    monkeypatch.setattr("wayfinder_paths.mcp.utils._report_tool_metric", Mock())
    monkeypatch.setattr(adapter_module, "send_transaction", send)
    monkeypatch.setattr(adapter_module, "ensure_allowance", approve)
    adapter = adapter_module.BRAPAdapter(sign_callback=signer)
    monkeypatch.setattr(adapter, "_record_swap_operation", record)

    async def execute(wait_for_receipt: bool = True) -> tuple[bool, Any]:
        if request.param == "adapter":
            try:
                return await adapter.swap_from_quote(token, destination, OWNER, quote)
            except (TransactionRevertedError, TimeoutError) as exc:
                return False, str(exc)
        result = await execute_module.onchain_swap(
            wallet_label="main",
            from_token="from",
            to_token="to",
            amount="1.0",
            wait_for_receipt=wait_for_receipt,
            receipt_confirmations=0,
        )
        return result["ok"] and result["result"]["status"] != "failed", result

    return SwapCase(quote, token, send, approve, record, execute)


@pytest.mark.asyncio
async def test_direct_approvals_are_confirmed_in_order(swap_case: SwapCase) -> None:
    original = deepcopy(swap_case.quote)
    success, _ = await swap_case.execute(False)
    assert success
    assert swap_case.send.await_count == 3
    swap_case.approve.assert_not_awaited()
    calls = swap_case.send.await_args_list
    expected = [*original["prerequisite_transactions"], original["calldata"]]
    for call, transaction in zip(calls, expected, strict=True):
        assert call.args[0] == {
            "chainId": CHAIN_ID,
            "from": OWNER,
            "to": to_checksum_address(transaction["to"]),
            "data": transaction["data"],
            "value": 0,
        }
    for call in calls[:2]:
        assert call.kwargs == {"wait_for_receipt": True, "confirmations": 0}
    assert swap_case.quote == original


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [0, 1])
async def test_no_next_step_before_prerequisite_receipt(
    swap_case: SwapCase, index: int
) -> None:
    waiting, release = asyncio.Event(), asyncio.Event()

    async def send(*args: Any, **kwargs: Any) -> str:
        if swap_case.send.await_count == index + 1:
            waiting.set()
            await release.wait()
        return "0xtest"

    swap_case.send.side_effect = send
    task = asyncio.create_task(swap_case.execute(False))
    try:
        await asyncio.wait_for(waiting.wait(), timeout=2)
        assert swap_case.send.await_count == index + 1
        assert not task.done()
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=2)
    assert task.result()[0]
    assert swap_case.send.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize(
    "failure",
    [
        TransactionRevertedError("0xbad", {"status": 0}),
        TimeoutError("Receipt timed out"),
    ],
)
async def test_prerequisite_failure_never_sends_swap(
    swap_case: SwapCase, index: int, failure: Exception
) -> None:
    swap_case.send.side_effect = [*["0xapproval"] * index, failure]
    success, result = await swap_case.execute(False)
    assert not success
    assert str(failure) in str(result)
    assert swap_case.send.await_count == index + 1
    swap_case.record.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_status", [0, 1])
async def test_real_sender_checks_prerequisite_receipts(
    swap_case: SwapCase, monkeypatch: pytest.MonkeyPatch, receipt_status: int
) -> None:
    events: list[str] = []

    async def broadcast(chain_id: int, signed: bytes) -> str:
        events.append("broadcast")
        return "0xtest"

    async def receipt(*args: Any, **kwargs: Any) -> dict[str, int]:
        events.append("receipt")
        return {"status": receipt_status, "blockNumber": 1}

    # Exercise send_transaction's real receipt/revert handling without signing
    # or submitting anything on-chain. Every network boundary is mocked.
    for name in ("gas_limit_transaction", "nonce_transaction", "gas_price_transaction"):
        monkeypatch.setattr(
            transaction_module, name, AsyncMock(side_effect=lambda tx: tx)
        )
    monkeypatch.setattr(
        transaction_module, "_is_gorlami_fork_chain", Mock(return_value=False)
    )
    monkeypatch.setattr(transaction_module, "broadcast_transaction", broadcast)
    monkeypatch.setattr(transaction_module, "wait_for_transaction_receipt", receipt)
    swap_case.send.side_effect = transaction_module.send_transaction
    success, _ = await swap_case.execute(True)
    assert success is bool(receipt_status)
    assert events == ["broadcast", "receipt"] * (3 if receipt_status else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["pons_v2", "uniswap_v3", "uniswap_v4"])
@pytest.mark.parametrize("prerequisites", [None, []])
async def test_already_approved_solver_needs_no_router_approval(
    swap_case: SwapCase, provider: str, prerequisites: list[Any] | None
) -> None:
    swap_case.quote.update(provider=provider, prerequisite_transactions=prerequisites)
    assert (await swap_case.execute(True))[0]
    swap_case.approve.assert_not_awaited()
    swap_case.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_pons_curve_uses_single_quoted_approval(swap_case: SwapCase) -> None:
    swap_case.quote["provider"] = "pons_v2"
    swap_case.quote["prerequisite_transactions"] = swap_case.quote[
        "prerequisite_transactions"
    ][:1]
    assert (await swap_case.execute(True))[0]
    assert swap_case.send.await_count == 2
    swap_case.approve.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "native", [ZERO_ADDRESS, "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"]
)
@pytest.mark.parametrize("provider", ["lifi", "pons_v2", "uniswap_v4"])
async def test_native_buy_does_not_approve(
    swap_case: SwapCase, native: str, provider: str
) -> None:
    swap_case.token["address"] = native
    swap_case.quote.update(provider=provider, prerequisite_transactions=[])
    swap_case.quote["calldata"]["value"] = "0xf4240"
    assert (await swap_case.execute(True))[0]
    swap_case.approve.assert_not_awaited()
    swap_case.send.assert_awaited_once()
    assert swap_case.send.await_args is not None
    assert swap_case.send.await_args.args[0]["value"] == 1000000


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_legacy_approval_is_still_required(
    swap_case: SwapCase, approved: bool
) -> None:
    swap_case.quote.update(
        provider="lifi", approval_address=PERMIT2, prerequisite_transactions=None
    )
    swap_case.approve.return_value = approved, "0xapproval"
    assert (await swap_case.execute(True))[0] is approved
    assert swap_case.approve.await_args is not None
    assert swap_case.approve.await_args.kwargs["spender"] == PERMIT2
    assert swap_case.approve.await_args.kwargs["amount"] == 1000000
    assert swap_case.send.await_count == int(approved)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["swap", "last_prerequisite"])
@pytest.mark.parametrize(
    "change",
    [
        {"chainId": 1},
        {"from": TOKEN},
        {"to": "invalid"},
        {"data": "0xnothex"},
        {"value": "invalid"},
        {"value": -1},
        {"calls": []},
    ],
)
async def test_all_transactions_validated_before_any_send(
    swap_case: SwapCase, target: str, change: dict[str, Any]
) -> None:
    tx = (
        swap_case.quote["calldata"]
        if target == "swap"
        else swap_case.quote["prerequisite_transactions"][-1]
    )
    tx.update(change)
    assert not (await swap_case.execute(True))[0]
    swap_case.send.assert_not_awaited()
    swap_case.approve.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"atomic_calls": [{"to": TOKEN, "data": "0x1234"}]},
        {"provider": "pons_v2_batch"},
        {"prerequisite_transactions": "invalid"},
        {"prerequisite_transactions": [None]},
        {"prerequisite_transactions": [{"to": TOKEN, "data": "0x1234"}]},
    ],
)
async def test_unsupported_sequence_does_not_sign(
    swap_case: SwapCase, change: dict[str, Any]
) -> None:
    swap_case.quote.update(change)
    assert not (await swap_case.execute(True))[0]
    swap_case.send.assert_not_awaited()
    swap_case.approve.assert_not_awaited()
