from wayfinder_paths.core.utils.evm_helpers import get_abi_filtered


async def allow_functions(
    policy_name: str,
    abi_chain_id: int,
    address: str,
    function_names: list[str],
    abi_address_override: str = None,
    manual_abi: dict = None,
):
    # Note: ChainID is just for fetching ABI, doesn't appear in the final policy. Doesn't bind a strict chain.
    return {
        "name": policy_name,
        "method": "eth_signTransaction",
        "action": "ALLOW",
        "conditions": [
            {
                "field_source": "ethereum_transaction",
                "field": "to",
                "operator": "eq",
                "value": address,
            },
            {
                "field_source": "ethereum_calldata",
                "field": "function_name",
                "abi": manual_abi
                or await get_abi_filtered(
                    abi_chain_id, abi_address_override or address, function_names
                ),
                "operator": "in",
                "value": function_names,
            },
        ],
    }
