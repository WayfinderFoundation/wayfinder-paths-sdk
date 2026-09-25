from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.utils import transaction as tx
from wayfinder_paths.core.utils.transaction import TransactionConfirmationError
from wayfinder_paths.mcp.tools import execute

CHAIN = 8453
OWNER = "0x" + "11" * 20
TOKEN = "0x" + "22" * 20
ROUTER = "0x" + "33" * 20
HASH = "0x" + "ab" * 32


@pytest.fixture
def scenario(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    token = {"address": TOKEN, "chain_id": CHAIN, "symbol": "USDC", "decimals": 6}
    quote = {
        "provider": "lifi",
        "input_amount": "1000000",
        "output_amount": "1000000",
        "approval_address": ROUTER,
        "calldata": {"chainId": CHAIN, "to": ROUTER, "data": "0x1234", "value": "0"},
        "bridge_tracking": {"provider": "lifi"},
    }
    send = AsyncMock(side_effect=TransactionConfirmationError(HASH, CHAIN))
    approve = AsyncMock(return_value=(True, None))
    bridge = AsyncMock()
    monkeypatch.setattr(execute, "send_transaction", send)
    monkeypatch.setattr(execute, "ensure_allowance", approve)
    monkeypatch.setattr(execute, "_annotate_profile", Mock())
    monkeypatch.setattr(execute, "_token_balance", AsyncMock(return_value=10**18))
    monkeypatch.setattr(
        execute,
        "get_wallet_signing_callback_for_chain",
        AsyncMock(return_value=(AsyncMock(), OWNER)),
    )
    monkeypatch.setattr(
        execute, "find_wallet_leg_for_chain", AsyncMock(return_value={"address": OWNER})
    )
    monkeypatch.setattr(
        execute.TokenResolver, "resolve_token_meta", AsyncMock(return_value=token)
    )
    monkeypatch.setattr(
        execute.BRAP_CLIENT, "get_quote", AsyncMock(return_value={"best_quote": quote})
    )
    monkeypatch.setattr(execute.BRAP_CLIENT, "wait_for_bridge_execution", bridge)
    monkeypatch.setattr(
        execute, "build_send_transaction", AsyncMock(return_value={"chainId": CHAIN})
    )
    monkeypatch.setattr("wayfinder_paths.mcp.utils._report_tool_metric", Mock())
    return {
        "token": token,
        "quote": quote,
        "send": send,
        "approve": approve,
        "bridge": bridge,
    }


def assert_submitted(result: dict[str, Any], effect: dict[str, Any]) -> None:
    assert result["status"] == "submitted"
    assert effect["status"] == "submitted"
    assert effect["confirmed"] is False
    assert effect["txn_hash"] == HASH
    assert effect["chain_id"] == CHAIN
    assert HASH in effect["explorer_url"]
    assert "before retrying" in effect["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_send_returns_submitted_not_failed_on_confirmation_outage(
    scenario: dict[str, Any], native: bool
) -> None:
    if native:
        scenario["token"]["address"] = ZERO_ADDRESS
    output = await execute.onchain_send(
        wallet_label="main", token="asset", recipient=ROUTER, amount="1.0"
    )
    assert output["ok"] is True
    result = output["result"]
    effect = result["effects"]["send_native" if native else "send_erc20"]
    assert_submitted(result, effect)
    scenario["send"].assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["approval", "prerequisite", "swap"])
async def test_unconfirmed_swap_stage_preserves_hash_and_stops_flow(
    scenario: dict[str, Any], stage: str
) -> None:
    if stage == "approval":
        scenario["approve"].side_effect = TransactionConfirmationError(HASH, CHAIN)
    elif stage == "prerequisite":
        scenario["quote"]["prerequisite_transactions"] = [
            {"chainId": CHAIN, "to": TOKEN, "data": "0x1234", "value": "0"},
            {"chainId": CHAIN, "to": ROUTER, "data": "0x5678", "value": "0"},
        ]

    output = await execute.onchain_swap(
        wallet_label="main", from_token="from", to_token="to", amount="1.0"
    )
    assert output["ok"] is True
    result = output["result"]
    effect = (
        result["effects"]["prerequisites"][0]
        if stage == "prerequisite"
        else result["effects"][stage]
    )
    assert_submitted(result, effect)
    scenario["bridge"].assert_not_awaited()
    if stage == "approval":
        scenario["send"].assert_not_awaited()
    else:
        scenario["send"].assert_awaited_once()
    if stage != "swap":
        assert "swap" not in result["effects"]


@pytest.mark.asyncio
async def test_pre_broadcast_failure_stays_failed(scenario: dict[str, Any]) -> None:
    scenario["send"].side_effect = RuntimeError("All RPCs failed pending nonce read")
    output = await execute.onchain_send(
        wallet_label="main", token="asset", recipient=ROUTER, amount="1.0"
    )
    assert output["result"]["status"] == "failed"
    assert "txn_hash" not in output["result"]["effects"]["send_erc20"]


@pytest.mark.asyncio
@pytest.mark.parametrize("healthy_peer", [False, True])
async def test_send_with_real_confirmation_handling(
    scenario: dict[str, Any], monkeypatch: pytest.MonkeyPatch, healthy_peer: bool
) -> None:
    # Keep send -> RPC confirmation -> MCP outcome handling real. Only signing
    # and network boundaries are replaced; no transaction leaves this test.
    monkeypatch.setattr(execute, "send_transaction", tx.send_transaction)
    signer = AsyncMock(return_value=b"signed")
    signer.wallet_address = None
    monkeypatch.setattr(
        execute,
        "get_wallet_signing_callback_for_chain",
        AsyncMock(return_value=(signer, OWNER)),
    )
    monkeypatch.setattr(tx, "_is_gorlami_fork_chain", lambda _: False)
    for name in ("gas_limit_transaction", "nonce_transaction", "gas_price_transaction"):
        monkeypatch.setattr(
            tx, name, AsyncMock(side_effect=lambda transaction: transaction)
        )
    broadcast = AsyncMock(return_value=HASH)
    monkeypatch.setattr(tx, "broadcast_transaction", broadcast)

    failed, peer = MagicMock(), MagicMock()
    failed.eth.wait_for_transaction_receipt = AsyncMock(
        side_effect=RuntimeError("403 quota")
    )
    peer.eth.wait_for_transaction_receipt = AsyncMock(
        return_value={"status": 1, "blockNumber": 100}
    )
    context = MagicMock()
    context.return_value.__aenter__.return_value = (
        [failed, peer] if healthy_peer else [failed]
    )
    monkeypatch.setattr(tx, "web3s_from_chain_id", context)

    output = await execute.onchain_send(
        wallet_label="main", token="asset", recipient=ROUTER, amount="1.0"
    )
    assert output["ok"] is True
    result = output["result"]
    effect = result["effects"]["send_erc20"]
    if healthy_peer:
        assert result["status"] == "confirmed"
        assert effect["txn_hash"] == HASH
    else:
        assert_submitted(result, effect)
    signer.assert_awaited_once()
    broadcast.assert_awaited_once()
