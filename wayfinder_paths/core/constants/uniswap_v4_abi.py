# Minimal ABIs for Uniswap v4 PoolManager + StateView (read-only verification).

POOL_MANAGER_ABI = [
    {
        "name": "initialize",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "key",
                "type": "tuple",
                "components": [
                    {"name": "currency0", "type": "address"},
                    {"name": "currency1", "type": "address"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "tickSpacing", "type": "int24"},
                    {"name": "hooks", "type": "address"},
                ],
            },
            {"name": "sqrtPriceX96", "type": "uint160"},
        ],
        "outputs": [{"name": "tick", "type": "int24"}],
    }
]

STATE_VIEW_ABI = [
    {
        "name": "getSlot0",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "poolId", "type": "bytes32"}],
        "outputs": [
            {"name": "sqrtPriceX96", "type": "uint160"},
            {"name": "tick", "type": "int24"},
            {"name": "protocolFee", "type": "uint24"},
            {"name": "lpFee", "type": "uint24"},
        ],
    }
]

