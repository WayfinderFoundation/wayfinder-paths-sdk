from __future__ import annotations

from typing import Any

# Minimal ABIs for ether.fi liquid restaking (LiquidityPool / eETH / weETH / WithdrawRequestNFT).

ETHERFI_LIQUIDITY_POOL_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "deposit",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "payable",
    },
    {
        "type": "function",
        "name": "deposit",
        "inputs": [{"name": "_referral", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "payable",
    },
    {
        "type": "function",
        "name": "requestWithdraw",
        "inputs": [
            {"name": "recipient", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "requestWithdrawWithPermit",
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_amount", "type": "uint256"},
            {
                "name": "_permit",
                "type": "tuple",
                "components": [
                    {"name": "value", "type": "uint256"},
                    {"name": "deadline", "type": "uint256"},
                    {"name": "v", "type": "uint8"},
                    {"name": "r", "type": "bytes32"},
                    {"name": "s", "type": "bytes32"},
                ],
            },
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "sharesForAmount",
        "inputs": [{"name": "amount", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "amountForShare",
        "inputs": [{"name": "share", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "sharesForWithdrawalAmount",
        "inputs": [{"name": "amount", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "getTotalPooledEther",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "getTotalEtherClaimOf",
        "inputs": [{"name": "_user", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "paused",
        "inputs": [],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
    },
]

ETHERFI_EETH_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "name",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "symbol",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "decimals",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "totalSupply",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "balanceOf",
        "inputs": [{"name": "_user", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "allowance",
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "approve",
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "transfer",
        "inputs": [
            {"name": "_recipient", "type": "address"},
            {"name": "_amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "transferFrom",
        "inputs": [
            {"name": "_sender", "type": "address"},
            {"name": "_recipient", "type": "address"},
            {"name": "_amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "shares",
        "inputs": [{"name": "", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "totalShares",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "nonces",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "DOMAIN_SEPARATOR",
        "inputs": [],
        "outputs": [{"name": "", "type": "bytes32"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "permit",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "deadline", "type": "uint256"},
            {"name": "v", "type": "uint8"},
            {"name": "r", "type": "bytes32"},
            {"name": "s", "type": "bytes32"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
]

ETHERFI_WEETH_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "name",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "symbol",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "decimals",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "balanceOf",
        "inputs": [{"name": "", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "allowance",
        "inputs": [
            {"name": "", "type": "address"},
            {"name": "", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "approve",
        "inputs": [
            {"name": "", "type": "address"},
            {"name": "", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "wrap",
        "inputs": [{"name": "_eETHAmount", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "wrapWithPermit",
        "inputs": [
            {"name": "_eETHAmount", "type": "uint256"},
            {
                "name": "_permit",
                "type": "tuple",
                "components": [
                    {"name": "value", "type": "uint256"},
                    {"name": "deadline", "type": "uint256"},
                    {"name": "v", "type": "uint8"},
                    {"name": "r", "type": "bytes32"},
                    {"name": "s", "type": "bytes32"},
                ],
            },
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "unwrap",
        "inputs": [{"name": "_weETHAmount", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "getWeETHByeETH",
        "inputs": [{"name": "_eETHAmount", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "getEETHByWeETH",
        "inputs": [{"name": "_weETHAmount", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "getRate",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
]

ETHERFI_WITHDRAW_REQUEST_NFT_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "claimWithdraw",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "getClaimableAmount",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "isFinalized",
        "inputs": [{"name": "requestId", "type": "uint256"}],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "isValid",
        "inputs": [{"name": "requestId", "type": "uint256"}],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "ownerOf",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
    },
    {
        "type": "event",
        "name": "WithdrawRequestCreated",
        "inputs": [
            {"indexed": True, "name": "requestId", "type": "uint32"},
            {"indexed": False, "name": "amountOfEEth", "type": "uint256"},
            {"indexed": False, "name": "shareOfEEth", "type": "uint256"},
            {"indexed": False, "name": "recipient", "type": "address"},
            {"indexed": False, "name": "fee", "type": "uint256"},
        ],
        "anonymous": False,
    },
]
