from __future__ import annotations

from typing import Any

from wayfinder_paths.core.constants.erc1155_abi import ERC1155_APPROVAL_ABI

# Verified against the factory's current implementation
# (0x848eeb1a79a8d0fd964e3386db6da400c22d278d, upgraded 2026-06-29 to a
# beacon-proxy scheme). The one-arg predict is canonical; a two-arg overload
# exists but ignores its address argument. There is no deployed-wallet
# registry getter — pre-upgrade wallets are detected via eth_getCode on the
# legacy derivation.
POLYMARKET_DEPOSIT_WALLET_FACTORY_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "predictWalletAddress",
        "stateMutability": "view",
        "inputs": [{"name": "_id", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "address"}],
    }
]

POLYMARKET_DEPOSIT_WALLET_BATCH_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Call": [
        {"name": "target", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "data", "type": "bytes"},
    ],
    "Batch": [
        {"name": "wallet", "type": "address"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint256"},
        {"name": "calls", "type": "Call[]"},
    ],
}

CONDITIONAL_TOKENS_ABI: list[dict[str, Any]] = [
    *ERC1155_APPROVAL_ABI,
    {
        "type": "function",
        "stateMutability": "view",
        "name": "getCollectionId",
        "inputs": [
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSet", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "getPositionId",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "collectionId", "type": "bytes32"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "balanceOf",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "redeemPositions",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "payoutDenominator",
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "payoutNumerators",
        "inputs": [
            {"name": "conditionId", "type": "bytes32"},
            {"name": "index", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

TOKEN_UNWRAP_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "unwrap",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [],
    }
]

POLYMARKET_COLLATERAL_RAMP_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "wrap",
        "inputs": [
            {"name": "_asset", "type": "address", "internalType": "address"},
            {"name": "_to", "type": "address", "internalType": "address"},
            {"name": "_amount", "type": "uint256", "internalType": "uint256"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "unwrap",
        "inputs": [
            {"name": "_asset", "type": "address", "internalType": "address"},
            {"name": "_to", "type": "address", "internalType": "address"},
            {"name": "_amount", "type": "uint256", "internalType": "uint256"},
        ],
        "outputs": [],
    },
]
