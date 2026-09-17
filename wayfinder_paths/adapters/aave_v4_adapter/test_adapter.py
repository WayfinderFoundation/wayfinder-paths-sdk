from unittest.mock import AsyncMock, Mock, patch

import pytest

from wayfinder_paths.adapters.aave_v4_adapter.adapter import AaveV4Adapter


class TestAaveV4Adapter:
    @pytest.fixture
    def adapter(self):
        return AaveV4Adapter()

    def test_init(self, adapter):
        assert adapter.adapter_type == "AAVE_V4"
        assert adapter.name == "aave_v4_adapter"


@pytest.fixture
def writable():
    return AaveV4Adapter(
        wallet_address="0x1111111111111111111111111111111111111111",
        sign_callback=Mock(),
    )


@pytest.fixture
def reserve():
    return {
        "underlying": "0x3600000000000000000000000000000000000000",
        "decimals": 6,
        "paused": False,
        "frozen": False,
        "borrowable": True,
        "liquidity": 100_000_000,
        "supplied": 0,
        "borrowed": 0,
        "hub_config": {"active": True, "halted": False, "add_cap": 10, "draw_cap": 10},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["supply", "repay", "borrow", "withdraw"])
@pytest.mark.parametrize("spoke", ["main", "forex"])
async def test_actions_target_selected_spoke_use_raw_units_and_check_receipts(
    writable, reserve, action, spoke
):
    module = "wayfinder_paths.adapters.aave_v4_adapter.adapter"
    with (
        patch.object(writable, "get_reserve", AsyncMock(return_value=(True, reserve))),
        patch(
            f"{module}.ensure_allowance", AsyncMock(return_value=(True, None))
        ) as approve,
        patch(
            f"{module}.encode_call",
            AsyncMock(
                return_value={
                    "to": writable._spoke(spoke),
                    "data": "0x1234",
                    "from": writable.wallet_address,
                }
            ),
        ) as encode,
        patch(f"{module}.web3_from_chain_id") as rpc,
        patch(f"{module}.send_transaction", AsyncMock(return_value="0xabc")) as send,
    ):
        rpc.return_value.__aenter__.return_value.eth.call = AsyncMock()
        ok, tx = await getattr(writable, action)(0, 1_000_000, spoke=spoke)
    assert ok and tx == "0xabc"
    assert encode.await_args.kwargs["args"] == [0, 1_000_000, writable.wallet_address]
    assert encode.await_args.kwargs["fn_name"] == action
    assert encode.await_args.kwargs["target"] == writable._spoke(spoke)
    assert approve.await_count == (1 if action in ("supply", "repay") else 0)
    if approve.await_count:
        assert approve.await_args.kwargs["spender"] == writable._spoke(spoke)
        assert approve.await_args.kwargs["approval_amount"] == 1_000_000
    send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,action,amount",
    [
        ({"paused": True}, "repay", 1),
        ({"frozen": True}, "supply", 1),
        ({"borrowable": False}, "borrow", 1),
        ({"liquidity": 0}, "withdraw", 1),
        ({}, "borrow", 11_000_000),
        ({}, "supply", 11_000_000),
        ({}, "supply", -1),
    ],
)
async def test_restrictions_fail_before_approvals_or_broadcast(
    writable, reserve, change, action, amount
):
    with (
        patch.object(
            writable,
            "get_reserve",
            AsyncMock(return_value=(True, {**reserve, **change})),
        ),
        patch(
            "wayfinder_paths.adapters.aave_v4_adapter.adapter.send_transaction",
            AsyncMock(),
        ) as send,
        patch(
            "wayfinder_paths.adapters.aave_v4_adapter.adapter.ensure_allowance",
            AsyncMock(),
        ) as approve,
    ):
        ok, error = await getattr(writable, action)(0, amount)
    assert not ok and error
    approve.assert_not_awaited()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_health_simulation_failure_does_not_broadcast(writable, reserve):
    module = "wayfinder_paths.adapters.aave_v4_adapter.adapter"
    with (
        patch.object(writable, "get_reserve", AsyncMock(return_value=(True, reserve))),
        patch(f"{module}.encode_call", AsyncMock(return_value={})),
        patch(f"{module}.web3_from_chain_id") as rpc,
        patch(f"{module}.send_transaction", AsyncMock()) as send,
    ):
        rpc.return_value.__aenter__.return_value.eth.call = AsyncMock(
            side_effect=ValueError("HealthFactorTooLow")
        )
        ok, error = await writable.borrow(0, 1)
    assert not ok and "HealthFactorTooLow" in error
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_tokenized_spokes_reuse_erc4626_with_six_decimal_usdc(writable):
    from wayfinder_paths.core.constants.aave_v4_contracts import ARC_AAVE_V4_VAULTS

    with patch.object(
        writable, "vault_deposit", AsyncMock(return_value=(True, "0xabc"))
    ) as deposit:
        assert await writable.tokenized_deposit("USDC", 1_000_000) == (True, "0xabc")
    deposit.assert_awaited_once_with(
        chain_id=5042, vault_address=ARC_AAVE_V4_VAULTS["USDC"], assets=1_000_000
    )


def test_unknown_spoke_and_other_chains_fail_closed():
    with pytest.raises(ValueError, match="Arc only"):
        AaveV4Adapter({"chain_id": 1})
    with pytest.raises(ValueError, match="main or forex"):
        AaveV4Adapter._spoke("arbitrary")
