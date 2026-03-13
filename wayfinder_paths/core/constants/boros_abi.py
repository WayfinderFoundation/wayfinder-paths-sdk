from __future__ import annotations

from typing import Any

# Minimal Boros ABI surface used by the SDK.

BOROS_MARKET_HUB_VIEW_ABI: list[dict[str, Any]] = [
    {
        "inputs": [{"internalType": "address", "name": "user", "type": "address"}],
        "name": "getPersonalCooldown",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint16", "name": "tokenId", "type": "uint16"}],
        "name": "tokenData",
        "outputs": [
            {
                "components": [
                    {"internalType": "address", "name": "token", "type": "address"},
                    {
                        "internalType": "uint96",
                        "name": "scalingFactor",
                        "type": "uint96",
                    },
                ],
                "internalType": "struct TokenData",
                "name": "",
                "type": "tuple",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "userAddress", "type": "address"},
            {"internalType": "uint16", "name": "tokenId", "type": "uint16"},
        ],
        "name": "getUserWithdrawalStatus",
        "outputs": [
            {"internalType": "uint32", "name": "start", "type": "uint32"},
            {"internalType": "uint224", "name": "unscaled", "type": "uint224"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

BOROS_ROUTER_VIEW_ABI: list[dict[str, Any]] = [
    {
        "name": "ammIdToAcc",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "ammId", "type": "uint24"}],
        "outputs": [{"name": "", "type": "bytes26"}],
    },
    {
        "name": "addLiquiditySingleCashToAmm",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "req",
                "type": "tuple",
                "components": [
                    {"name": "cross", "type": "bool"},
                    {"name": "ammId", "type": "uint24"},
                    {"name": "enterMarket", "type": "bool"},
                    {"name": "netCashIn", "type": "int256"},
                    {"name": "minLpOut", "type": "uint256"},
                    {"name": "desiredSwapSide", "type": "uint8"},
                    {"name": "desiredSwapRate", "type": "int128"},
                ],
            }
        ],
        "outputs": [],
    },
    {
        "name": "removeLiquiditySingleCashFromAmm",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "req",
                "type": "tuple",
                "components": [
                    {"name": "cross", "type": "bool"},
                    {"name": "ammId", "type": "uint24"},
                    {"name": "lpToRemove", "type": "uint256"},
                    {"name": "minCashOut", "type": "int256"},
                    {"name": "desiredSwapSide", "type": "uint8"},
                    {"name": "desiredSwapRate", "type": "int128"},
                ],
            }
        ],
        "outputs": [],
    },
]

BOROS_VAULT_BALANCE_ABI: list[dict[str, Any]] = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "bytes26"}],
        "outputs": [{"name": "", "type": "uint256"}],
    }
]

BOROS_MERKLE_DISTRIBUTOR_ABI: list[dict[str, Any]] = [
    {
        "name": "claim",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "receiver", "type": "address"},
            {"name": "tokens", "type": "address[]"},
            {"name": "totalAccrueds", "type": "uint256[]"},
            {"name": "proofs", "type": "bytes32[][]"},
        ],
        "outputs": [],
    }
]
