from __future__ import annotations

from typing import Any

# Ondo InstantManager write surface on Ethereum mainnet.
OUSG_INSTANT_MANAGER_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "subscribe",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "depositToken", "type": "address"},
            {"name": "depositAmount", "type": "uint256"},
            {"name": "minimumRwaReceived", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "subscribeRebasingOUSG",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "depositToken", "type": "address"},
            {"name": "depositAmount", "type": "uint256"},
            {"name": "minimumRwaReceived", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "redeem",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "rwaAmount", "type": "uint256"},
            {"name": "receivingToken", "type": "address"},
            {"name": "minimumTokenReceived", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "redeemRebasingOUSG",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "rousgAmount", "type": "uint256"},
            {"name": "receivingToken", "type": "address"},
            {"name": "minimumTokenReceived", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "acceptedSubscriptionTokens",
        "stateMutability": "view",
        "inputs": [{"name": "token", "type": "address"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
]

USDY_INSTANT_MANAGER_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "subscribe",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "depositToken", "type": "address"},
            {"name": "depositAmount", "type": "uint256"},
            {"name": "minimumRwaReceived", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "subscribeRebasingUSDY",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "depositToken", "type": "address"},
            {"name": "depositAmount", "type": "uint256"},
            {"name": "minimumRwaReceived", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "redeem",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "rwaAmount", "type": "uint256"},
            {"name": "receivingToken", "type": "address"},
            {"name": "minimumTokenReceived", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "redeemRebasingUSDY",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "rusdyAmount", "type": "uint256"},
            {"name": "receivingToken", "type": "address"},
            {"name": "minimumTokenReceived", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "acceptedSubscriptionTokens",
        "stateMutability": "view",
        "inputs": [{"name": "token", "type": "address"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
]

ONDO_ID_REGISTRY_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "getRegisteredID",
        "stateMutability": "view",
        "inputs": [
            {"name": "rwaToken", "type": "address"},
            {"name": "account", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "bytes32"}],
    }
]

ONDO_PRICE_ORACLE_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "getAssetPrice",
        "stateMutability": "view",
        "inputs": [{"name": "token", "type": "address"}],
        "outputs": [{"name": "price", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "getPrice",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "getPriceData",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {"name": "price", "type": "uint256"},
            {"name": "timestamp", "type": "uint256"},
        ],
    },
]

ROUSG_WRAPPER_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "wrap",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "_OUSGAmount", "type": "uint256"}],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "unwrap",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "_rOUSGAmount", "type": "uint256"}],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "sharesOf",
        "stateMutability": "view",
        "inputs": [{"name": "_account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "getSharesByROUSG",
        "stateMutability": "view",
        "inputs": [{"name": "_rOUSGAmount", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "getROUSGByShares",
        "stateMutability": "view",
        "inputs": [{"name": "_shares", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "getOUSGPrice",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "price", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "getTotalShares",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

# rUSDY on Ethereum and mUSD on Mantle share the same rebasing/share surface.
RUSDY_WRAPPER_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "wrap",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "_USDYAmount", "type": "uint256"}],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "unwrap",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "_rUSDYAmount", "type": "uint256"}],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "sharesOf",
        "stateMutability": "view",
        "inputs": [{"name": "_account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "getSharesByRUSDY",
        "stateMutability": "view",
        "inputs": [{"name": "_rUSDYAmount", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "getRUSDYByShares",
        "stateMutability": "view",
        "inputs": [{"name": "_shares", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "getTotalShares",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "oracle",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "type": "function",
        "name": "usdy",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
]
