from __future__ import annotations

# Minimal ABIs for rewards claiming.

MERKL_DISTRIBUTOR_ABI = [
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "claim",
        "inputs": [
            {"name": "users", "type": "address[]"},
            {"name": "tokens", "type": "address[]"},
            {"name": "amounts", "type": "uint256[]"},
            {"name": "proofs", "type": "bytes32[][]"},
        ],
        "outputs": [],
    }
]

URD_ABI = [
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "claim",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "reward", "type": "address"},
            {"name": "claimable", "type": "uint256"},
            {"name": "proof", "type": "bytes32[]"},
        ],
        "outputs": [{"name": "amount", "type": "uint256"}],
    }
]
