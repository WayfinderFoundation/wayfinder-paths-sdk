# Minimal ABIs for Uniswap V4 execution: Universal Router (V4_SWAP command)
# and the canonical Permit2 approve used to fund ERC-20 inputs.

UNIVERSAL_ROUTER_ABI = [
    {
        "type": "function",
        "name": "execute",
        "stateMutability": "payable",
        "inputs": [
            {"name": "commands", "type": "bytes"},
            {"name": "inputs", "type": "bytes[]"},
            {"name": "deadline", "type": "uint256"},
        ],
        "outputs": [],
    }
]

PERMIT2_ABI = [
    {
        "type": "function",
        "name": "approve",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "token", "type": "address"},
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint160"},
            {"name": "expiration", "type": "uint48"},
        ],
        "outputs": [],
    }
]
