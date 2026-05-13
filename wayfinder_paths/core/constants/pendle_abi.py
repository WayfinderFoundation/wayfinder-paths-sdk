"""Pendle ABI subsets used by the pendle adapter."""

from typing import Any

PENDLE_ROUTER_STATIC_ABI: list[dict[str, Any]] = [
    {
        "inputs": [{"internalType": "address", "name": "market", "type": "address"}],
        "name": "getLpToSyRate",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "market", "type": "address"}],
        "name": "getPtToSyRate",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "market", "type": "address"}],
        "name": "getLpToAssetRate",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "market", "type": "address"}],
        "name": "getPtToAssetRate",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]
