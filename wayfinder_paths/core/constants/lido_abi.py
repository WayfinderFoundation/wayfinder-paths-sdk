from __future__ import annotations

from typing import Any

# Minimal ABIs for Lido (stETH / wstETH / WithdrawalQueueERC721).

STETH_LIDO_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "submit",
        "stateMutability": "payable",
        "inputs": [{"name": "_referral", "type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "isStakingPaused",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "bool"}],
    },
    {
        "type": "function",
        "name": "getCurrentStakeLimit",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "sharesOf",
        "stateMutability": "view",
        "inputs": [{"name": "_account", "type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "getSharesByPooledEth",
        "stateMutability": "view",
        "inputs": [{"name": "_ethAmount", "type": "uint256"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "getPooledEthByShares",
        "stateMutability": "view",
        "inputs": [{"name": "_sharesAmount", "type": "uint256"}],
        "outputs": [{"type": "uint256"}],
    },
]

WSTETH_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "wrap",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "_stETHAmount", "type": "uint256"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "unwrap",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "_wstETHAmount", "type": "uint256"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "getStETHByWstETH",
        "stateMutability": "view",
        "inputs": [{"name": "_wstETHAmount", "type": "uint256"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "getWstETHByStETH",
        "stateMutability": "view",
        "inputs": [{"name": "_stETHAmount", "type": "uint256"}],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "stEthPerToken",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "tokensPerStEth",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
]

WITHDRAWAL_QUEUE_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "requestWithdrawals",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "_amounts", "type": "uint256[]"},
            {"name": "_owner", "type": "address"},
        ],
        "outputs": [{"type": "uint256[]"}],
    },
    {
        "type": "function",
        "name": "requestWithdrawalsWstETH",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "_amounts", "type": "uint256[]"},
            {"name": "_owner", "type": "address"},
        ],
        "outputs": [{"type": "uint256[]"}],
    },
    {
        "type": "function",
        "name": "getWithdrawalRequests",
        "stateMutability": "view",
        "inputs": [{"name": "_owner", "type": "address"}],
        "outputs": [{"type": "uint256[]"}],
    },
    {
        "type": "function",
        "name": "getWithdrawalStatus",
        "stateMutability": "view",
        "inputs": [{"name": "_requestIds", "type": "uint256[]"}],
        "outputs": [
            {
                "name": "statuses",
                "type": "tuple[]",
                "components": [
                    {"name": "amountOfStETH", "type": "uint256"},
                    {"name": "amountOfShares", "type": "uint256"},
                    {"name": "owner", "type": "address"},
                    {"name": "timestamp", "type": "uint256"},
                    {"name": "isFinalized", "type": "bool"},
                    {"name": "isClaimed", "type": "bool"},
                ],
            }
        ],
    },
    {
        "type": "function",
        "name": "getLastCheckpointIndex",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "type": "function",
        "name": "findCheckpointHints",
        "stateMutability": "view",
        "inputs": [
            {"name": "_requestIds", "type": "uint256[]"},
            {"name": "_firstIndex", "type": "uint256"},
            {"name": "_lastIndex", "type": "uint256"},
        ],
        "outputs": [{"name": "hintIds", "type": "uint256[]"}],
    },
    {
        "type": "function",
        "name": "getClaimableEther",
        "stateMutability": "view",
        "inputs": [
            {"name": "_requestIds", "type": "uint256[]"},
            {"name": "_hints", "type": "uint256[]"},
        ],
        "outputs": [{"name": "claimableEthValues", "type": "uint256[]"}],
    },
    {
        "type": "function",
        "name": "claimWithdrawals",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "_requestIds", "type": "uint256[]"},
            {"name": "_hints", "type": "uint256[]"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "claimWithdrawalsTo",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "_requestIds", "type": "uint256[]"},
            {"name": "_hints", "type": "uint256[]"},
            {"name": "_recipient", "type": "address"},
        ],
        "outputs": [],
    },
]

