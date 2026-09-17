from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from wayfinder_paths.core.utils import transaction as tx
from wayfinder_paths.mcp.tools import contracts, evm_contract, execute
from wayfinder_paths.mcp.tools import transaction_status as status_tool

HASH = "0x" + "ab" * 32
RECEIPT = {"status": 1, "blockNumber": 100}


def receipt_providers(monkeypatch: pytest.MonkeyPatch, *waits: AsyncMock) -> list[Any]:
    providers = []
    for wait in waits:
        w3 = MagicMock()
        w3.eth.wait_for_transaction_receipt = wait
        providers.append(w3)
    context = MagicMock()
    context.return_value.__aenter__.return_value = providers
    monkeypatch.setattr(tx, "web3s_from_chain_id", context)
    return providers


@pytest.mark.asyncio
async def test_fast_403_does_not_cancel_healthy_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def healthy(**_: Any) -> dict[str, Any]:
        await asyncio.sleep(0.02)
        return RECEIPT

    async def delayed(*_: Any, **kwargs: Any) -> dict[str, Any]:
        return await healthy(**kwargs)

    slow = AsyncMock(side_effect=delayed)
    receipt_providers(
        monkeypatch,
        AsyncMock(side_effect=RuntimeError("403 https://rpc.invalid/key")),
        slow,
    )
    receipt = await tx.wait_for_transaction_receipt(42161, HASH, confirmations=0)
    assert receipt == RECEIPT
    slow.assert_awaited_once()


@pytest.mark.asyncio
async def test_success_cancels_and_awaits_remaining_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = asyncio.Event()

    async def hangs(*_: Any, **__: Any) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    receipt_providers(
        monkeypatch, AsyncMock(return_value=RECEIPT), AsyncMock(side_effect=hangs)
    )
    await tx.wait_for_transaction_receipt(1, HASH, confirmations=0)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_receipt_and_confirmation_wait_share_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Eth:
        wait_for_transaction_receipt = AsyncMock(return_value=RECEIPT)

        @property
        async def block_number(self) -> int:
            return 100

    providers = receipt_providers(monkeypatch, AsyncMock())
    providers[0].eth = Eth()
    with pytest.raises(TimeoutError):
        await tx.wait_for_transaction_receipt(
            1, HASH, timeout=0.03, poll_interval=0.01, confirmations=3
        )


@pytest.mark.asyncio
async def test_confirmation_heights_tolerate_one_bad_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Eth:
        wait_for_transaction_receipt = AsyncMock(return_value=RECEIPT)

        def __init__(self, broken: bool) -> None:
            self.broken = broken

        @property
        async def block_number(self) -> int:
            if self.broken:
                raise RuntimeError("403")
            return 103

    providers = receipt_providers(monkeypatch, AsyncMock(), AsyncMock())
    providers[0].eth, providers[1].eth = Eth(True), Eth(False)
    assert await tx.wait_for_transaction_receipt(1, HASH, timeout=1) == RECEIPT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "receipt", [{"status": 0, "blockNumber": 100}, {"blockNumber": 100}]
)
async def test_revert_or_missing_status_never_confirms(
    monkeypatch: pytest.MonkeyPatch, receipt: dict[str, Any]
) -> None:
    receipt_providers(monkeypatch, AsyncMock(return_value=receipt))
    with pytest.raises(
        tx.TransactionRevertedError if receipt.get("status") == 0 else RuntimeError
    ):
        await tx.wait_for_transaction_receipt(1, HASH, confirmations=0)


@pytest.mark.asyncio
async def test_submitted_hash_survives_all_receipt_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = AsyncMock()
    signer.wallet_address = "0x" + "12" * 20
    submit = AsyncMock(return_value=HASH)
    monkeypatch.setattr(tx, "sponsorship_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(tx, "send_sponsored_transaction", submit)
    receipt_providers(monkeypatch, AsyncMock(side_effect=RuntimeError("403 Forbidden")))
    with pytest.raises(tx.TransactionConfirmationError) as caught:
        await tx.send_transaction({"chainId": 42161}, signer)
    assert caught.value.txn_hash == HASH
    assert caught.value.chain_id == 42161
    submit.assert_awaited_once()
    signer.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_tool_reconciles_bridge_even_if_receipt_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        status_tool,
        "wait_for_transaction_receipt",
        AsyncMock(side_effect=RuntimeError("secret")),
    )
    bridge = AsyncMock(
        return_value={
            "is_success": True,
            "state": "completed",
            "destination_tx_hash": HASH,
        }
    )
    monkeypatch.setattr(status_tool.BRAP_CLIENT, "wait_for_bridge_execution", bridge)
    out = await status_tool.onchain_get_transaction_status(
        42161, HASH, {"from_chain": 42161, "to_chain": 5042, "provider": "lifi"}
    )
    assert out["result"]["status"] == "confirmed"
    assert out["result"]["source"]["status"] == "unavailable"
    assert out["result"]["destination"]["state"] == "completed"
    assert "secret" not in str(out)


@pytest.mark.asyncio
async def test_source_receipt_does_not_confirm_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        status_tool, "wait_for_transaction_receipt", AsyncMock(return_value=RECEIPT)
    )
    monkeypatch.setattr(
        status_tool.BRAP_CLIENT,
        "wait_for_bridge_execution",
        AsyncMock(side_effect=TimeoutError("https://rpc.invalid/key")),
    )
    out = await status_tool.onchain_get_transaction_status(
        42161, HASH, {"from_chain": 42161, "provider": "lifi"}
    )
    result = out["result"]
    assert result["status"] == "submitted"
    assert result["source"]["status"] == "confirmed"
    assert result["destination"]["status"] == "pending"
    assert result["next_action"]["tool"] == "onchain_get_transaction_status"
    assert "Do not repeat" in result["message"]
    assert "rpc.invalid" not in str(out)


@pytest.mark.asyncio
async def test_invalid_status_request_does_no_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read = AsyncMock()
    monkeypatch.setattr(status_tool, "wait_for_transaction_receipt", read)
    assert not (await status_tool.onchain_get_transaction_status(1, "not-a-hash"))["ok"]
    assert not (
        await status_tool.onchain_get_transaction_status(1, HASH, {"from_chain": 2})
    )["ok"]
    read.assert_not_awaited()


@pytest.fixture
def swap_runtime(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    sender = "0x" + "12" * 20
    tracking = {
        "provider": "lifi",
        "from_chain": 42161,
        "to_chain": 5042,
        "bridge": "across",
    }
    best: dict[str, Any] = {
        "provider": "lifi",
        "input_amount": 945768,
        "output_amount": 940300,
        "calldata": {"to": "0x" + "33" * 20, "data": "0xabcd", "value": "0"},
        "bridge_tracking": tracking,
    }

    async def resolve(query: str, **_: Any) -> dict[str, Any]:
        return {
            "address": "0x" + ("11" if query == "from" else "22") * 20,
            "chain_id": 42161 if query == "from" else 5042,
            "symbol": "USDC",
            "decimals": 6,
        }

    monkeypatch.setattr(
        execute.TokenResolver, "resolve_token_meta", AsyncMock(side_effect=resolve)
    )
    monkeypatch.setattr(
        execute,
        "get_wallet_signing_callback_for_chain",
        AsyncMock(return_value=(AsyncMock(), sender)),
    )
    monkeypatch.setattr(
        execute,
        "find_wallet_leg_for_chain",
        AsyncMock(return_value={"address": sender}),
    )
    monkeypatch.setattr(execute, "get_token_balance", AsyncMock(return_value=1000000))
    monkeypatch.setattr(
        execute, "ensure_allowance", AsyncMock(return_value=(True, None))
    )
    monkeypatch.setattr(
        execute.BRAP_CLIENT,
        "get_quote",
        AsyncMock(return_value={"best_quote": best, "quotes": []}),
    )
    bridge = AsyncMock(return_value={"is_success": True, "state": "completed"})
    monkeypatch.setattr(execute.BRAP_CLIENT, "wait_for_bridge_execution", bridge)
    send = AsyncMock(side_effect=tx.TransactionConfirmationError(42161, HASH))
    monkeypatch.setattr(execute, "send_transaction", send)
    profile = Mock()
    monkeypatch.setattr(execute, "_annotate_profile", profile)
    return {
        "best": best,
        "sender": sender,
        "send": send,
        "bridge": bridge,
        "profile": profile,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["swap", "approval", "prerequisite"])
async def test_unconfirmed_swap_steps_never_resubmit_or_continue(
    monkeypatch: pytest.MonkeyPatch, swap_runtime: dict[str, Any], step: str
) -> None:
    if step == "approval":
        monkeypatch.setattr(
            execute,
            "ensure_allowance",
            AsyncMock(side_effect=tx.TransactionConfirmationError(42161, HASH)),
        )
    if step == "prerequisite":
        swap_runtime["best"]["prerequisite_transactions"] = [
            {"chainId": 42161, "to": "0x" + "44" * 20, "data": "0x1234"}
        ]
    out = await execute.onchain_swap(
        wallet_label="main", from_token="from", to_token="to", amount="0.945768"
    )
    assert out["ok"] is True
    result = out["result"]
    assert result["status"] == "submitted"
    effect = (
        result["effects"]["prerequisites"][0]
        if step == "prerequisite"
        else result["effects"][step]
    )
    assert effect["txn_hash"] == HASH
    assert "Do not repeat" in effect["message"]
    action = effect["next_action"]
    assert action["arguments"]["txn_hash"] == HASH
    swap_runtime["bridge"].assert_not_awaited()
    if step == "approval":
        swap_runtime["send"].assert_not_awaited()
    else:
        swap_runtime["send"].assert_awaited_once()
    if step == "swap":
        assert (
            action["arguments"]["bridge_tracking"]
            == swap_runtime["best"]["bridge_tracking"]
        )
        assert swap_runtime["profile"].call_args.kwargs["status"] == "submitted"
    else:
        assert "swap" not in result["effects"]
        assert "bridge_tracking" not in action["arguments"]
        assert result["pending_step"] == step


@pytest.mark.asyncio
async def test_transfer_retains_submitted_status(
    monkeypatch: pytest.MonkeyPatch, swap_runtime: dict[str, Any]
) -> None:
    monkeypatch.setattr(
        execute, "build_send_transaction", AsyncMock(return_value={"chainId": 42161})
    )
    out = await execute.onchain_send(
        wallet_label="main",
        token="from",
        recipient=swap_runtime["sender"],
        amount="0.945768",
    )
    assert out["result"]["status"] == "submitted"
    assert out["result"]["effects"]["send_erc20"]["txn_hash"] == HASH
    swap_runtime["send"].assert_awaited_once()
    assert swap_runtime["profile"].call_args.kwargs["status"] == "submitted"


@pytest.mark.asyncio
@pytest.mark.parametrize("bridge_error", [True, False])
async def test_confirmed_source_pending_bridge_includes_status_action(
    swap_runtime: dict[str, Any], bridge_error: bool
) -> None:
    swap_runtime["send"].side_effect = None
    swap_runtime["send"].return_value = HASH
    if bridge_error:
        swap_runtime["bridge"].side_effect = TimeoutError("https://rpc.invalid/key")
    else:
        swap_runtime["bridge"].return_value = {
            "state": "destination_pending",
            "is_success": False,
            "is_finished": False,
        }
    out = await execute.onchain_swap(
        wallet_label="main", from_token="from", to_token="to", amount="0.945768"
    )
    result = out["result"]
    assert result["status"] == "submitted"
    assert result["effects"]["swap"]["confirmation_waited"] is True
    assert result["next_action"] == {
        "tool": "onchain_get_transaction_status",
        "arguments": {
            "chain_id": 42161,
            "txn_hash": HASH,
            "bridge_tracking": swap_runtime["best"]["bridge_tracking"],
        },
    }
    assert "Do not repeat" in result["message"]
    assert "rpc.invalid" not in str(result)
    swap_runtime["send"].assert_awaited_once()
    assert swap_runtime["profile"].call_args.kwargs["status"] == "submitted"


@pytest.mark.asyncio
async def test_contract_call_retains_submitted_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        evm_contract,
        "get_wallet_signing_callback",
        AsyncMock(return_value=(AsyncMock(), "0x" + "12" * 20)),
    )
    monkeypatch.setattr(
        evm_contract, "encode_call", AsyncMock(return_value={"chainId": 1})
    )
    send = AsyncMock(side_effect=tx.TransactionConfirmationError(1, HASH))
    monkeypatch.setattr(evm_contract, "send_transaction", send)
    annotate = Mock()
    monkeypatch.setattr(evm_contract, "_annotate", annotate)
    out = await evm_contract.contracts_execute(
        wallet_label="main",
        chain_id=1,
        contract_address="0x" + "33" * 20,
        function_name="deposit",
        args="[]",
        abi=[
            {
                "type": "function",
                "name": "deposit",
                "stateMutability": "nonpayable",
                "inputs": [],
                "outputs": [],
            }
        ],
    )
    assert out["result"]["status"] == "submitted"
    assert out["result"]["tx_hash"] == HASH
    send.assert_awaited_once()
    assert annotate.call_args.kwargs["status"] == "submitted"


@pytest.mark.asyncio
async def test_contract_deployment_retains_submitted_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contracts,
        "get_wallet_signing_callback",
        AsyncMock(return_value=(AsyncMock(), "0x" + "12" * 20)),
    )
    monkeypatch.setattr(
        contracts,
        "_load_solidity_source",
        Mock(return_value=(None, "Test.sol", "// test")),
    )
    deploy = AsyncMock(side_effect=tx.TransactionConfirmationError(1, HASH))
    monkeypatch.setattr(contracts, "_deploy_contract", deploy)
    annotate = Mock()
    monkeypatch.setattr(contracts, "_annotate_deploy", annotate)
    artifacts = Mock()
    monkeypatch.setattr(contracts.ContractArtifactStore, "default", artifacts)
    out = await contracts.contracts_deploy(
        wallet_label="main", source_path="Test.sol", contract_name="Test", chain_id=1
    )
    assert out["result"]["status"] == "submitted"
    assert out["result"]["tx_hash"] == HASH
    assert out["result"]["next_action"]["arguments"]["txn_hash"] == HASH
    deploy.assert_awaited_once()
    assert annotate.call_args.kwargs["status"] == "submitted"
    artifacts.assert_not_called()


@pytest.mark.asyncio
async def test_broadcast_errors_redact_rpc_urls_and_reverts_keep_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        execute,
        "send_transaction",
        AsyncMock(
            side_effect=RuntimeError(
                "403 https://user:secret@rpc.invalid/private-key?token=secret"
            )
        ),
    )
    success, result = await execute._broadcast(AsyncMock(), {}, chain_id=1)
    assert not success
    assert "secret" not in str(result)
    assert "rpc.invalid" not in str(result)
    monkeypatch.setattr(
        execute,
        "send_transaction",
        AsyncMock(side_effect=tx.TransactionRevertedError(HASH, {"status": 0})),
    )
    success, result = await execute._broadcast(AsyncMock(), {}, chain_id=1)
    assert not success
    assert result["status"] == "failed"
    assert result["txn_hash"] == HASH


@pytest.mark.asyncio
async def test_receipt_timeout_cleans_up_pending_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = asyncio.Event()

    async def hangs(*_: Any, **__: Any) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    receipt_providers(monkeypatch, AsyncMock(side_effect=hangs))
    with pytest.raises(TimeoutError):
        await tx.wait_for_transaction_receipt(1, HASH, timeout=0.01)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_status_check_timeout_never_turns_into_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def hangs(*_: Any, **__: Any) -> None:
        await asyncio.Event().wait()

    real_timeout = asyncio.timeout
    monkeypatch.setattr(status_tool.asyncio, "timeout", lambda _: real_timeout(0.01))
    monkeypatch.setattr(
        status_tool, "wait_for_transaction_receipt", AsyncMock(side_effect=hangs)
    )
    result = (await status_tool.onchain_get_transaction_status(1, HASH))["result"]
    assert result["status"] == "submitted"
    assert result["txn_hash"] == HASH
    assert result["next_action"]["arguments"] == {"chain_id": 1, "txn_hash": HASH}


@pytest.mark.parametrize(
    ("bridge", "expected"),
    [
        (
            {"is_success": False, "is_finished": False, "state": "destination_pending"},
            "submitted",
        ),
        ({"is_success": False, "is_finished": True, "state": "refunded"}, "failed"),
        ({"is_success": True, "state": "completed"}, "confirmed"),
    ],
)
def test_pending_bridge_is_not_a_failed_bridge(
    bridge: dict[str, Any], expected: str
) -> None:
    assert status_tool.bridge_transaction_status(bridge) == expected


@pytest.mark.asyncio
async def test_post_approval_read_error_keeps_approval_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wayfinder_paths.core.utils import tokens

    monkeypatch.setattr(
        tokens, "get_token_allowance", AsyncMock(side_effect=[0, RuntimeError("403")])
    )
    monkeypatch.setattr(
        tokens, "build_approve_transaction", AsyncMock(return_value={"chainId": 1})
    )
    send = AsyncMock(return_value=HASH)
    monkeypatch.setattr(tokens, "send_transaction", send)
    monkeypatch.setattr(tokens.asyncio, "sleep", AsyncMock())
    with pytest.raises(tx.TransactionConfirmationError) as caught:
        await tokens.ensure_allowance(
            token_address="0x" + "33" * 20,
            owner="0x" + "12" * 20,
            spender="0x" + "44" * 20,
            amount=1,
            chain_id=1,
            signing_callback=AsyncMock(),
        )
    assert caught.value.txn_hash == HASH
    send.assert_awaited_once()
