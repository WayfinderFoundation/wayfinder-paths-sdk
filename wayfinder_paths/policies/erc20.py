from wayfinder_paths.core.constants.erc20_abi import ERC20_ABI


def any_erc20_function(token_address: str) -> dict:
    return {
        "name": "Allow Any ERC20 Transfer To Address",
        "method": "eth_signTransaction",
        "action": "ALLOW",
        "conditions": [
            {
                "field_source": "ethereum_calldata",
                "field": "transfer.to",
                "abi": ERC20_ABI,
                "operator": "eq",
                "value": token_address,
            }
        ],
    }


def erc20_spender_for_any_token(spender_address: str) -> dict:
    return {
        "name": "Allow Any ERC20 Approve To Spender",
        "method": "eth_signTransaction",
        "action": "ALLOW",
        "conditions": [
            {
                "field_source": "ethereum_calldata",
                "field": "approve.spender",
                "abi": ERC20_ABI,
                "operator": "eq",
                "value": spender_address,
            },
        ],
    }
