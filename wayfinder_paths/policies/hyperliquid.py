def any_hyperliquid_l1_payload():
    return {
        "name": "Allow Hypecore L1 Payload",
        "method": "eth_signTypedData_v4",
        "action": "ALLOW",
        "conditions": [
            {
                "field": "chainId",
                "field_source": "ethereum_typed_data_domain",
                "operator": "eq",
                "value": "1337",
            }
        ],
    }


def any_hyperliquid_user_payload():
    return {
        "name": "Allow User Signed Payload",
        "method": "eth_signTypedData_v4",
        "action": "ALLOW",
        "conditions": [
            {
                "field": "chainId",
                "field_source": "ethereum_typed_data_domain",
                "operator": "eq",
                "value": "42161",
            }
        ],
    }
