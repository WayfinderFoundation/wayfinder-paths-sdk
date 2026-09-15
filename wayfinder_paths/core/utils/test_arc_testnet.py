from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wayfinder_paths.core.constants.chains import (
    ARC_USDC_ADDRESS,
    CHAIN_CODE_TO_ID,
    CHAIN_EXPLORER_URLS,
    CHAIN_ID_ARC_TESTNET,
    CHAIN_ID_TO_CODE,
    GAS_SPONSORED_CHAIN_IDS,
    SUPPORTED_CHAINS,
)
from wayfinder_paths.core.utils.brap import normalize_swap_token
from wayfinder_paths.core.utils.transaction import gas_price_transaction


@pytest.mark.parametrize(
    "chain",
    [{"chain_id": CHAIN_ID_ARC_TESTNET}, {"chain": {"id": CHAIN_ID_ARC_TESTNET}}],
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


def test_arc_is_explicit_testnet_not_a_mainnet_alias_or_sponsored_scan_target():
    assert CHAIN_CODE_TO_ID["arc-testnet"] == CHAIN_ID_ARC_TESTNET
    assert CHAIN_ID_TO_CODE[CHAIN_ID_ARC_TESTNET] == "arc-testnet"
    assert "arc" not in CHAIN_CODE_TO_ID
    assert CHAIN_ID_ARC_TESTNET not in GAS_SPONSORED_CHAIN_IDS
    assert CHAIN_ID_ARC_TESTNET not in SUPPORTED_CHAINS
    assert CHAIN_EXPLORER_URLS[CHAIN_ID_ARC_TESTNET] == "https://testnet.arcscan.app/"


@pytest.mark.asyncio
@pytest.mark.parametrize("base_fee", [0, 10**9, 100 * 10**9])
async def test_arc_max_fee_floor_does_not_inflate_priority_fee(base_fee):
    rpc = MagicMock()
    rpc.eth.get_block = AsyncMock(return_value={"baseFeePerGas": base_fee})
    rpc.eth.fee_history = AsyncMock(return_value={"reward": [[0]] * 10})
    with patch("wayfinder_paths.core.utils.transaction.web3s_from_chain_id") as context:
        context.return_value.__aenter__.return_value = [rpc]
        result = await gas_price_transaction({"chainId": CHAIN_ID_ARC_TESTNET})
    assert result["maxFeePerGas"] == max(base_fee * 2, 20 * 10**9)
    assert result["maxPriorityFeePerGas"] == 0
    assert "gasPrice" not in result
