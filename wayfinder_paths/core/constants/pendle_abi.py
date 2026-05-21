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

PENDLE_LIMIT_ORDER_STRUCT_ABI: list[dict[str, Any]] = [
    {"internalType": "uint256", "name": "salt", "type": "uint256"},
    {"internalType": "uint256", "name": "expiry", "type": "uint256"},
    {"internalType": "uint256", "name": "nonce", "type": "uint256"},
    {
        "internalType": "enum IPLimitOrderType.OrderType",
        "name": "orderType",
        "type": "uint8",
    },
    {"internalType": "address", "name": "token", "type": "address"},
    {"internalType": "address", "name": "YT", "type": "address"},
    {"internalType": "address", "name": "maker", "type": "address"},
    {"internalType": "address", "name": "receiver", "type": "address"},
    {"internalType": "uint256", "name": "makingAmount", "type": "uint256"},
    {"internalType": "uint256", "name": "lnImpliedRate", "type": "uint256"},
    {"internalType": "uint256", "name": "failSafeRate", "type": "uint256"},
    {"internalType": "bytes", "name": "permit", "type": "bytes"},
]

PENDLE_LIMIT_ROUTER_ABI: list[dict[str, Any]] = [
    {
        "inputs": [
            {
                "components": PENDLE_LIMIT_ORDER_STRUCT_ABI,
                "internalType": "struct Order",
                "name": "order",
                "type": "tuple",
            }
        ],
        "name": "cancelSingle",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {
                "components": PENDLE_LIMIT_ORDER_STRUCT_ABI,
                "internalType": "struct Order[]",
                "name": "orders",
                "type": "tuple[]",
            }
        ],
        "name": "cancelBatch",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {
                "components": [
                    {
                        "components": PENDLE_LIMIT_ORDER_STRUCT_ABI,
                        "internalType": "struct Order",
                        "name": "order",
                        "type": "tuple",
                    },
                    {
                        "internalType": "bytes",
                        "name": "signature",
                        "type": "bytes",
                    },
                    {
                        "internalType": "uint256",
                        "name": "makingAmount",
                        "type": "uint256",
                    },
                ],
                "internalType": "struct FillOrderParams[]",
                "name": "params",
                "type": "tuple[]",
            },
            {"internalType": "address", "name": "receiver", "type": "address"},
            {"internalType": "uint256", "name": "maxTaking", "type": "uint256"},
            {"internalType": "bytes", "name": "", "type": "bytes"},
            {"internalType": "bytes", "name": "callback", "type": "bytes"},
        ],
        "name": "fill",
        "outputs": [
            {"internalType": "uint256", "name": "actualMaking", "type": "uint256"},
            {"internalType": "uint256", "name": "actualTaking", "type": "uint256"},
            {"internalType": "uint256", "name": "totalFee", "type": "uint256"},
            {"internalType": "bytes", "name": "callbackReturn", "type": "bytes"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "increaseNonce",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]
