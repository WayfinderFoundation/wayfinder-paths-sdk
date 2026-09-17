from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wayfinder_paths.adapters.brap_adapter.adapter import BRAPAdapter
from wayfinder_paths.core.constants.chains import (
    ARC_USDC_ADDRESS,
    CHAIN_CODE_TO_ID,
    CHAIN_EXPLORER_URLS,
    CHAIN_ID_ARC,
    CHAIN_ID_TO_CODE,
    GAS_SPONSORED_CHAIN_IDS,
    SUPPORTED_CHAINS,
)
from wayfinder_paths.core.utils.brap import normalize_swap_token
from wayfinder_paths.core.utils.transaction import gas_price_transaction


@pytest.mark.parametrize(
    "chain",
    [{"chain_id": CHAIN_ID_ARC}, {"chain": {"id": CHAIN_ID_ARC}}],
)
def test_arc_swap_metadata_is_normalized_without_mutating_native_gas_units(chain):
    token = {**chain, "address": "0x" + "0" * 40, "decimals": 18}
    normalized = normalize_swap_token(token)
    assert normalized["address"] == ARC_USDC_ADDRESS
    assert normalized["decimals"] == 6
    assert token["decimals"] == 18
    assert normalize_swap_token(normalized) is normalized


def test_other_chain_native_tokens_are_unchanged():
    token = {"chain_id": 1, "address": "0x" + "0" * 40, "decimals": 18}
    assert normalize_swap_token(token) is token


@pytest.mark.asyncio
async def test_arc_ledger_ids_describe_the_normalized_units_not_the_native_db_row():
    token = normalize_swap_token(
        {
            "id": 123,
            "chain_id": CHAIN_ID_ARC,
            "address": "native",
            "decimals": 18,
        }
    )
    adapter = BRAPAdapter()
    with patch.object(
        adapter.ledger_adapter, "record_operation", AsyncMock(return_value=(True, {}))
    ) as record:
        await adapter._record_swap_operation(
            token,
            token,
            "0x" + "1" * 40,
            {"input_amount": "1000000", "output_amount": "999999"},
            "0xtest",
        )
    operation = record.await_args.kwargs["operation_data"]
    assert operation.from_token_id == f"arc_{ARC_USDC_ADDRESS}"
    assert operation.from_amount == "1000000"
    assert operation.from_amount_usd == 0


def test_arc_is_mainnet_and_not_sponsored():
    assert CHAIN_CODE_TO_ID["arc"] == CHAIN_ID_ARC
    assert CHAIN_ID_TO_CODE[CHAIN_ID_ARC] == "arc"
    assert "arc-testnet" not in CHAIN_CODE_TO_ID
    assert CHAIN_ID_ARC not in GAS_SPONSORED_CHAIN_IDS
    assert CHAIN_ID_ARC not in SUPPORTED_CHAINS
    assert CHAIN_EXPLORER_URLS[CHAIN_ID_ARC] == "https://explorer.arc.io/"


@pytest.mark.asyncio
@pytest.mark.parametrize("base_fee", [0, 10**9, 100 * 10**9])
async def test_arc_uses_live_fee_estimates(base_fee):
    rpc = MagicMock()
    rpc.eth.get_block = AsyncMock(return_value={"baseFeePerGas": base_fee})
    rpc.eth.fee_history = AsyncMock(return_value={"reward": [[0]] * 10})
    with patch("wayfinder_paths.core.utils.transaction.web3s_from_chain_id") as context:
        context.return_value.__aenter__.return_value = [rpc]
        result = await gas_price_transaction({"chainId": CHAIN_ID_ARC})
    assert result["maxFeePerGas"] == base_fee * 2
    assert result["maxPriorityFeePerGas"] == 0
    assert "gasPrice" not in result
