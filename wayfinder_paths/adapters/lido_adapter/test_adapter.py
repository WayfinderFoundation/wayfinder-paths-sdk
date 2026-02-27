from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.adapters.lido_adapter.adapter import (
    WITHDRAWAL_MAX_WEI,
    WITHDRAWAL_MIN_WEI,
    LidoAdapter,
    _split_withdrawal_amount,
)
from wayfinder_paths.core.constants.base import MAX_UINT256
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM
from wayfinder_paths.core.constants.contracts import ZERO_ADDRESS
from wayfinder_paths.core.constants.lido_contracts import LIDO_BY_CHAIN


def test_adapter_type():
    adapter = LidoAdapter(config={})
    assert adapter.adapter_type == "LIDO"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,kwargs",
    [
        ("stake_eth", {"amount_wei": 100}),
        ("wrap_steth", {"amount_steth_wei": 100}),
        ("unwrap_wsteth", {"amount_wsteth_wei": 100}),
        ("request_withdrawal", {"asset": "stETH", "amount_wei": 100}),
        ("claim_withdrawals", {"request_ids": [1]}),
    ],
)
async def test_require_wallet_returns_false_when_no_wallet(method, kwargs):
    adapter = LidoAdapter(config={})
    ok, msg = await getattr(adapter, method)(**kwargs)
    assert ok is False
    assert msg == "wallet address not configured"


def test_split_withdrawal_amount_min():
    assert _split_withdrawal_amount(WITHDRAWAL_MIN_WEI) == [WITHDRAWAL_MIN_WEI]


def test_split_withdrawal_amount_below_min_raises():
    with pytest.raises(ValueError):
        _split_withdrawal_amount(WITHDRAWAL_MIN_WEI - 1)


def test_split_withdrawal_amount_adjusts_small_tail():
    amount = WITHDRAWAL_MAX_WEI + 50
    assert _split_withdrawal_amount(amount) == [
        WITHDRAWAL_MAX_WEI - 50,
        WITHDRAWAL_MIN_WEI,
    ]


def test_split_withdrawal_amount_multi_chunk_adjusts_small_tail():
    amount = 2 * WITHDRAWAL_MAX_WEI + 50
    assert _split_withdrawal_amount(amount) == [
        WITHDRAWAL_MAX_WEI,
        WITHDRAWAL_MAX_WEI - 50,
        WITHDRAWAL_MIN_WEI,
    ]


@pytest.mark.asyncio
async def test_stake_eth_receive_steth_calls_submit():
    adapter = LidoAdapter(
        config={},
        sign_callback=AsyncMock(),
        wallet_address="0x1111111111111111111111111111111111111111",
    )
    entry = LIDO_BY_CHAIN[CHAIN_ID_ETHEREUM]

    with (
        patch.object(
            LidoAdapter,
            "_get_staking_state",
            new_callable=AsyncMock,
            return_value=(False, MAX_UINT256),
        ) as _mock_state,
        patch(
            "wayfinder_paths.adapters.lido_adapter.adapter.encode_call",
            new_callable=AsyncMock,
            return_value={"to": entry["steth"], "data": "0x"},
        ) as mock_encode,
        patch(
            "wayfinder_paths.adapters.lido_adapter.adapter.send_transaction",
            new_callable=AsyncMock,
            return_value="0xstake",
        ) as mock_send,
    ):
        ok, res = await adapter.stake_eth(amount_wei=123, receive="stETH")

    assert ok is True
    assert res == "0xstake"
    mock_encode.assert_awaited_once()
    kwargs = mock_encode.await_args.kwargs
    assert kwargs["target"] == entry["steth"]
    assert kwargs["fn_name"] == "submit"
    assert kwargs["args"] == [ZERO_ADDRESS]
    assert kwargs["value"] == 123
    mock_send.assert_awaited_once_with(
        {"to": entry["steth"], "data": "0x"}, adapter.sign_callback
    )


@pytest.mark.asyncio
async def test_stake_eth_receive_wsteth_stakes_then_wraps_delta():
    adapter = LidoAdapter(
        config={},
        sign_callback=AsyncMock(),
        wallet_address="0x1111111111111111111111111111111111111111",
    )
    entry = LIDO_BY_CHAIN[CHAIN_ID_ETHEREUM]

    with (
        patch.object(
            LidoAdapter,
            "_get_staking_state",
            new_callable=AsyncMock,
            return_value=(False, MAX_UINT256),
        ),
        patch(
            "wayfinder_paths.adapters.lido_adapter.adapter.get_token_balance",
            new_callable=AsyncMock,
            side_effect=[10, 110],
        ) as mock_balance,
        patch(
            "wayfinder_paths.adapters.lido_adapter.adapter.ensure_allowance",
            new_callable=AsyncMock,
            return_value=(True, {}),
        ) as mock_allowance,
        patch(
            "wayfinder_paths.adapters.lido_adapter.adapter.encode_call",
            new_callable=AsyncMock,
            side_effect=[{"tx": "stake"}, {"tx": "wrap"}],
        ) as mock_encode,
        patch(
            "wayfinder_paths.adapters.lido_adapter.adapter.send_transaction",
            new_callable=AsyncMock,
            side_effect=["0xstake", "0xwrap"],
        ) as mock_send,
    ):
        ok, res = await adapter.stake_eth(amount_wei=123, receive="wstETH")

    assert ok is True
    assert isinstance(res, dict)
    assert res["stake_tx"] == "0xstake"
    assert res["wrap_tx"] == "0xwrap"
    assert res["steth_wrapped"] == 100

    assert mock_balance.await_count == 2
    mock_allowance.assert_awaited_once()
    allow_kwargs = mock_allowance.await_args.kwargs
    assert allow_kwargs["token_address"] == entry["steth"]
    assert allow_kwargs["spender"] == entry["wsteth"]
    assert allow_kwargs["amount"] == 100

    assert mock_encode.await_count == 2
    assert mock_send.await_count == 2


@pytest.mark.asyncio
async def test_request_withdrawal_steth_splits_and_calls_queue():
    adapter = LidoAdapter(
        config={},
        sign_callback=AsyncMock(),
        wallet_address="0x1111111111111111111111111111111111111111",
    )
    entry = LIDO_BY_CHAIN[CHAIN_ID_ETHEREUM]

    with (
        patch(
            "wayfinder_paths.adapters.lido_adapter.adapter.ensure_allowance",
            new_callable=AsyncMock,
            return_value=(True, {}),
        ) as mock_allowance,
        patch(
            "wayfinder_paths.adapters.lido_adapter.adapter.encode_call",
            new_callable=AsyncMock,
            return_value={"tx": "req"},
        ) as mock_encode,
        patch(
            "wayfinder_paths.adapters.lido_adapter.adapter.send_transaction",
            new_callable=AsyncMock,
            return_value="0xreq",
        ),
    ):
        ok, res = await adapter.request_withdrawal(
            asset="stETH",
            amount_wei=WITHDRAWAL_MAX_WEI + 50,
        )

    assert ok is True
    assert res["tx"] == "0xreq"
    assert res["asset"] == "stETH"
    assert res["amounts"] == [WITHDRAWAL_MAX_WEI - 50, WITHDRAWAL_MIN_WEI]

    mock_allowance.assert_awaited_once()
    allow_kwargs = mock_allowance.await_args.kwargs
    assert allow_kwargs["token_address"] == entry["steth"]
    assert allow_kwargs["spender"] == entry["withdrawal_queue"]

    mock_encode.assert_awaited_once()
    kwargs = mock_encode.await_args.kwargs
    assert kwargs["target"] == entry["withdrawal_queue"]
    assert kwargs["fn_name"] == "requestWithdrawals"
    assert kwargs["args"][0] == [WITHDRAWAL_MAX_WEI - 50, WITHDRAWAL_MIN_WEI]


@pytest.mark.asyncio
async def test_claim_withdrawals_sorts_and_dedupes_ids():
    adapter = LidoAdapter(
        config={},
        sign_callback=AsyncMock(),
        wallet_address="0x1111111111111111111111111111111111111111",
    )
    entry = LIDO_BY_CHAIN[CHAIN_ID_ETHEREUM]

    with (
        patch.object(
            LidoAdapter,
            "_find_checkpoint_hints",
            new_callable=AsyncMock,
            return_value=[7, 8],
        ) as mock_hints,
        patch(
            "wayfinder_paths.adapters.lido_adapter.adapter.encode_call",
            new_callable=AsyncMock,
            return_value={"tx": "claim"},
        ) as mock_encode,
        patch(
            "wayfinder_paths.adapters.lido_adapter.adapter.send_transaction",
            new_callable=AsyncMock,
            return_value="0xclaim",
        ),
    ):
        ok, res = await adapter.claim_withdrawals(request_ids=[5, 2, 2])

    assert ok is True
    assert res == "0xclaim"

    mock_hints.assert_awaited_once_with(chain_id=CHAIN_ID_ETHEREUM, request_ids=[2, 5])
    mock_encode.assert_awaited_once()
    kwargs = mock_encode.await_args.kwargs
    assert kwargs["target"] == entry["withdrawal_queue"]
    assert kwargs["fn_name"] == "claimWithdrawals"
    assert kwargs["args"][0] == [2, 5]
    assert kwargs["args"][1] == [7, 8]
