from __future__ import annotations

import pytest

from wayfinder_paths.core.config import get_etherscan_api_key
from wayfinder_paths.testing.gorlami import gorlami_configured

pytestmark = pytest.mark.skipif(
    not gorlami_configured(),
    reason="api_key not configured (needed for gorlami fork proxy)",
)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_contract_call_uniswap_v3_slot0_on_mainnet(gorlami):
    # Uniswap V3 USDC/WETH 0.3% pool on Ethereum mainnet.
    pool = "0x8ad599c3a0ff1de082011efddc58f1908eb6e6d8"
    slot0_abi = [
        {
            "inputs": [],
            "name": "slot0",
            "outputs": [
                {"internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"},
                {"internalType": "int24", "name": "tick", "type": "int24"},
                {
                    "internalType": "uint16",
                    "name": "observationIndex",
                    "type": "uint16",
                },
                {
                    "internalType": "uint16",
                    "name": "observationCardinality",
                    "type": "uint16",
                },
                {
                    "internalType": "uint16",
                    "name": "observationCardinalityNext",
                    "type": "uint16",
                },
                {"internalType": "uint8", "name": "feeProtocol", "type": "uint8"},
                {"internalType": "bool", "name": "unlocked", "type": "bool"},
            ],
            "stateMutability": "view",
            "type": "function",
        }
    ]

    from wayfinder_paths.mcp.tools.evm_contract import contract_call

    out = await contract_call(
        chain_id=1,
        contract_address=pool,
        function_signature="slot0()",
        abi=slot0_abi,
    )

    assert out["ok"] is True, out
    value = out["result"]["value"]
    assert isinstance(value, list)
    assert len(value) == 7
    assert isinstance(value[0], int)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_contract_call_can_fetch_abi_from_etherscan(gorlami):
    if not get_etherscan_api_key():
        pytest.skip("etherscan_api_key not configured")

    # Uniswap V3 USDC/WETH 0.3% pool on Ethereum mainnet.
    pool = "0x8ad599c3a0ff1de082011efddc58f1908eb6e6d8"

    from wayfinder_paths.mcp.tools.evm_contract import contract_call

    out = await contract_call(
        chain_id=1,
        contract_address=pool,
        function_signature="slot0()",
    )

    assert out["ok"] is True, out
    assert out["result"]["abi_source"] == "etherscan_v2"
    value = out["result"]["value"]
    assert isinstance(value, list)
    assert len(value) == 7
    assert isinstance(value[0], int)
