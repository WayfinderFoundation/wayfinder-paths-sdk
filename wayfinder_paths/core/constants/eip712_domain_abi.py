# ERC-5267: an account's EIP-712 domain (used to wrap EIP-1271 digests for
# smart accounts such as Kernel EIP-7702 delegates).
EIP712_DOMAIN_ABI = [
    {
        "type": "function",
        "name": "eip712Domain",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {"name": "fields", "type": "bytes1"},
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
            {"name": "salt", "type": "bytes32"},
            {"name": "extensions", "type": "uint256[]"},
        ],
    }
]
