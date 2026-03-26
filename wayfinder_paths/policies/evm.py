def native_transfer(destination_address: str, value: int) -> dict:
    # TODO THIS FUNCTION IS NOT DONE CAUSE POLICIES DONT KNOW THE WALLET ADDRESS YET.
    return {
        "name": "Allow Native Transfer To Address",
        "method": "eth_signTransaction",
        "action": "ALLOW",
        "conditions": [
            {
                "field_source": "ethereum_transaction",
                "field": "to",
                "operator": "eq",
                "value": destination_address,
            },
            {
                "field_source": "ethereum_transaction",
                "field": "value",
                "operator": "eq",
                "value": hex(value),
            },
        ],
    }
