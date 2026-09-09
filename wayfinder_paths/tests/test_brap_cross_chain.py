from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from wayfinder_paths.adapters.brap_adapter.adapter import BRAPAdapter
from wayfinder_paths.core.clients.BRAPClient import BRAP_CLIENT
from wayfinder_paths.mcp.tools.execute import onchain_swap
from wayfinder_paths.mcp.tools.quotes import onchain_quote_swap

EVM = "0x000000000000000000000000000000000000dEaD"
SVM = "BTXGZD6APaEPLUnELUT3Q1HWUYaWatu42WXT3YCU1vxY"
ROUTER = "0x" + "11" * 20
SPENDER = "0x" + "22" * 20
TOKEN = "0x" + "33" * 20
RELAYER_FEE = 940000000000000


@pytest.mark.asyncio
@pytest.mark.parametrize("from_chain,to_chain", [(4663, 900), (900, 4663)])
@pytest.mark.parametrize("recipient", [None, "", " "])
async def test_client_requires_cross_family_destination_before_network(
    from_chain: int, to_chain: int, recipient: str | None
) -> None:
    with patch.object(
        BRAP_CLIENT, "_authed_request", new_callable=AsyncMock
    ) as request:
        with pytest.raises(ValueError, match="to_wallet is required"):
            await BRAP_CLIENT.get_quote(
                from_token=TOKEN,
                to_token=TOKEN,
                from_chain=from_chain,
                to_chain=to_chain,
                from_wallet=EVM if from_chain == 4663 else SVM,
                to_wallet=recipient,
                from_amount="1000000",
            )
        request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "from_chain,to_chain,sender,recipient",
    [
        (4663, 900, EVM, SVM),
        (900, 4663, SVM, EVM),
        (4663, 1, EVM, None),
        (900, 900, SVM, None),
    ],
)
async def test_client_preserves_destination_and_internal_chain_ids(
    from_chain: int, to_chain: int, sender: str, recipient: str | None
) -> None:
    response = httpx.Response(
        200,
        json={"quotes": []},
        request=httpx.Request("GET", "https://example.test/quote"),
    )
    with patch.object(
        BRAP_CLIENT, "_authed_request", new=AsyncMock(return_value=response)
    ) as request:
        await BRAP_CLIENT.get_quote(
            from_token=TOKEN,
            to_token=TOKEN,
            from_chain=from_chain,
            to_chain=to_chain,
            from_wallet=sender,
            to_wallet=recipient,
            from_amount="1000000",
        )
    params = request.await_args.kwargs["params"]
    assert params["from_chain"] == from_chain
    assert params["to_chain"] == to_chain
    assert params.get("to_wallet") == recipient


@pytest.mark.asyncio
async def test_token_id_swap_passes_recipient_through_adapter_to_client() -> None:
    adapter = BRAPAdapter()
    from_token = {"address": TOKEN, "chain": {"id": 4663}}
    to_token = {"address": SVM, "chain": {"id": 900}}
    quote = {"provider": "lifi", "output_amount": "123"}
    with (
        patch(
            "wayfinder_paths.adapters.brap_adapter.adapter.TOKEN_CLIENT.get_token_details",
            new=AsyncMock(side_effect=[from_token, to_token]),
        ),
        patch.object(
            BRAP_CLIENT, "get_quote", new=AsyncMock(return_value={"best_quote": quote})
        ) as request,
        patch.object(
            adapter, "swap_from_quote", new=AsyncMock(return_value=(True, {}))
        ) as execute,
    ):
        success, _ = await adapter.swap_from_token_ids(
            "from", "to", EVM, "1000", to_address=SVM
        )
    assert success
    assert request.await_args.kwargs["to_wallet"] == SVM
    assert execute.await_args.kwargs["quote"] == quote


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [RELAYER_FEE, str(RELAYER_FEE), hex(RELAYER_FEE)])
async def test_mcp_execution_quote_round_trips_router_fee_and_approval(
    value: int | str,
) -> None:
    from_token = {
        "token_id": "from",
        "symbol": "PONS",
        "address": TOKEN,
        "chain_id": 4663,
        "chain": {"id": 4663},
        "decimals": 18,
    }
    to_token = {
        "token_id": "to",
        "symbol": "STONK",
        "address": SVM,
        "chain_id": 900,
        "chain": {"id": 900},
        "decimals": 6,
    }
    calldata = {"to": ROUTER, "data": "0x1234", "value": value, "chainId": 4663}
    quote = {
        "provider": "lifi",
        "input_amount": "1000",
        "output_amount": "2000",
        "approval_address": SPENDER,
        "calldata": calldata,
    }
    ring = [
        {"address": EVM, "chain_type": "ethereum"},
        {"address": SVM, "chain_type": "solana"},
    ]
    adapter = BRAPAdapter()
    with (
        patch("wayfinder_paths.mcp.utils._report_tool_metric"),
        patch(
            "wayfinder_paths.mcp.tools.quotes.load_wallet_ring",
            new=AsyncMock(return_value=ring),
        ),
        patch(
            "wayfinder_paths.mcp.tools.quotes.TokenResolver.resolve_token_meta",
            new=AsyncMock(side_effect=[from_token, to_token]),
        ),
        patch.object(
            BRAP_CLIENT, "get_quote", new=AsyncMock(return_value={"best_quote": quote})
        ),
        patch(
            "wayfinder_paths.adapters.brap_adapter.adapter.ensure_allowance",
            new=AsyncMock(return_value=(True, "0xapproval")),
        ) as approve,
        patch(
            "wayfinder_paths.adapters.brap_adapter.adapter.send_transaction",
            new=AsyncMock(return_value="0xtest"),
        ) as send,
        patch.object(adapter, "_record_swap_operation", new=AsyncMock(return_value={})),
    ):
        preview = await onchain_quote_swap(
            wallet_label="main",
            from_token="from",
            to_token="to",
            amount="1.0",
            include_calldata=True,
        )
        assert preview["ok"]
        execution_quote = preview["result"]["execution_quote"]
        assert execution_quote == quote
        success, _ = await adapter.swap_from_quote(
            from_token, to_token, EVM, execution_quote
        )
    assert success
    tx = send.await_args.args[0]
    assert tx["to"] == ROUTER
    assert tx["value"] == RELAYER_FEE
    assert tx["data"] == calldata["data"]
    assert tx["chainId"] == 4663
    assert approve.await_args.kwargs["spender"] == SPENDER
    assert approve.await_args.kwargs["amount"] == 1000
    assert calldata["value"] == value  # Do not mutate the provider's quote.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "calldata", ["0x1234", {"to": ROUTER, "data": "0x1234", "chainId": 1}]
)
async def test_adapter_rejects_incomplete_or_wrong_chain_quote_before_approval(
    calldata: Any,
) -> None:
    with (
        patch(
            "wayfinder_paths.adapters.brap_adapter.adapter.ensure_allowance",
            new_callable=AsyncMock,
        ) as approve,
        patch(
            "wayfinder_paths.adapters.brap_adapter.adapter.send_transaction",
            new_callable=AsyncMock,
        ) as send,
    ):
        success, _ = await BRAPAdapter().swap_from_quote(
            {"address": TOKEN, "chain": {"id": 4663}},
            {"chain": {"id": 900}},
            EVM,
            {"calldata": calldata},
        )
    assert not success
    approve.assert_not_awaited()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_destination_stops_before_balance_quote_or_broadcast() -> None:
    with (
        patch("wayfinder_paths.mcp.utils._report_tool_metric"),
        patch(
            "wayfinder_paths.mcp.tools.execute.is_solana_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.TokenResolver.resolve_token_meta",
            new=AsyncMock(
                side_effect=[
                    {"chain_id": 4663, "address": TOKEN},
                    {"chain_id": 900, "address": SVM},
                ]
            ),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.get_wallet_signing_callback_for_chain",
            new=AsyncMock(return_value=(AsyncMock(), EVM)),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.find_wallet_leg_for_chain",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute._token_balance", new_callable=AsyncMock
        ) as balance,
        patch.object(BRAP_CLIENT, "get_quote", new_callable=AsyncMock) as quote,
        patch(
            "wayfinder_paths.mcp.tools.execute._broadcast", new_callable=AsyncMock
        ) as send,
    ):
        result = await onchain_swap(
            wallet_label="main", from_token="from", to_token="to", amount="1.0"
        )
    assert not result["ok"]
    assert result["error"]["code"] == "invalid_wallet"
    balance.assert_not_awaited()
    quote.assert_not_awaited()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_robinhood_to_solana_execution_keeps_recipient_and_native_fee() -> None:
    quote = {
        "provider": "lifi",
        "input_amount": "1000000",
        "output_amount": "900000",
        "calldata": {
            "to": ROUTER,
            "data": "0x1234",
            "value": str(RELAYER_FEE),
            "chainId": 4663,
        },
    }
    with (
        patch("wayfinder_paths.mcp.utils._report_tool_metric"),
        patch("wayfinder_paths.mcp.tools.execute._annotate_profile"),
        patch(
            "wayfinder_paths.mcp.tools.execute.is_solana_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.TokenResolver.resolve_token_meta",
            new=AsyncMock(
                side_effect=[
                    {"chain_id": 4663, "address": TOKEN, "decimals": 6},
                    {"chain_id": 900, "address": SVM, "decimals": 6},
                ]
            ),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.get_wallet_signing_callback_for_chain",
            new=AsyncMock(return_value=(AsyncMock(), EVM)),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute.find_wallet_leg_for_chain",
            new=AsyncMock(return_value={"address": SVM}),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute._token_balance",
            new=AsyncMock(return_value=1000000),
        ),
        patch.object(
            BRAP_CLIENT, "get_quote", new=AsyncMock(return_value={"best_quote": quote})
        ) as get_quote,
        patch(
            "wayfinder_paths.mcp.tools.execute._ensure_allowance",
            new=AsyncMock(return_value=(True, None)),
        ),
        patch(
            "wayfinder_paths.mcp.tools.execute._broadcast",
            new=AsyncMock(return_value=(True, {"txn_hash": "0xtest"})),
        ) as send,
    ):
        result = await onchain_swap(
            wallet_label="main", from_token="from", to_token="to", amount="1.0"
        )
    assert result["ok"]
    assert result["result"]["recipient"] == SVM
    assert get_quote.await_args.kwargs["to_wallet"] == SVM
    assert send.await_args.args[1]["value"] == RELAYER_FEE
    assert send.await_args.args[1]["to"] == ROUTER
    assert send.await_args.kwargs["chain_id"] == 4663


@pytest.mark.asyncio
async def test_svm_execution_quote_keeps_serialized_transaction() -> None:
    token = {
        "token_id": "from",
        "symbol": "SOL",
        "chain_id": 900,
        "decimals": 9,
        "address": SVM,
    }
    quote = {
        "provider": "jupiter",
        "calldata": {
            "serializedTransaction": "AQID",
            "chainId": 900,
            "chainType": "solana",
            "lastValidBlockHeight": 123,
        },
    }
    with (
        patch("wayfinder_paths.mcp.utils._report_tool_metric"),
        patch(
            "wayfinder_paths.mcp.tools.quotes.load_wallet_ring",
            new=AsyncMock(return_value=[{"address": SVM, "chain_type": "solana"}]),
        ),
        patch(
            "wayfinder_paths.mcp.tools.quotes.TokenResolver.resolve_token_meta",
            new=AsyncMock(return_value=token),
        ),
        patch.object(
            BRAP_CLIENT, "get_quote", new=AsyncMock(return_value={"best_quote": quote})
        ),
    ):
        result = await onchain_quote_swap(
            wallet_label="main",
            from_token="from",
            to_token="to",
            amount="1.0",
            include_calldata=True,
        )
    assert result["ok"]
    assert result["result"]["execution_quote"] == quote
