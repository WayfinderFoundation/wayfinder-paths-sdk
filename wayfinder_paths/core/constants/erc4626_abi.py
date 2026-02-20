from __future__ import annotations

ERC4626_ABI = [
    {
        "type": "function",
        "stateMutability": "view",
        "name": "asset",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    # --- ERC-20 metadata / balances (ERC-4626 shares are ERC-20) ---
    {
        "type": "function",
        "stateMutability": "view",
        "name": "name",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "symbol",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "decimals",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "totalSupply",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "balanceOf",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    # --- ERC-4626 views ---
    {
        "type": "function",
        "stateMutability": "view",
        "name": "totalAssets",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "convertToAssets",
        "inputs": [{"name": "shares", "type": "uint256"}],
        "outputs": [{"name": "assets", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "convertToShares",
        "inputs": [{"name": "assets", "type": "uint256"}],
        "outputs": [{"name": "shares", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "previewDeposit",
        "inputs": [{"name": "assets", "type": "uint256"}],
        "outputs": [{"name": "shares", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "previewRedeem",
        "inputs": [{"name": "shares", "type": "uint256"}],
        "outputs": [{"name": "assets", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "maxWithdraw",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "maxRedeem",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "deposit",
        "inputs": [
            {"name": "assets", "type": "uint256"},
            {"name": "receiver", "type": "address"},
        ],
        "outputs": [{"name": "shares", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "withdraw",
        "inputs": [
            {"name": "assets", "type": "uint256"},
            {"name": "receiver", "type": "address"},
            {"name": "owner", "type": "address"},
        ],
        "outputs": [{"name": "shares", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "mint",
        "inputs": [
            {"name": "shares", "type": "uint256"},
            {"name": "receiver", "type": "address"},
        ],
        "outputs": [{"name": "assets", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "redeem",
        "inputs": [
            {"name": "shares", "type": "uint256"},
            {"name": "receiver", "type": "address"},
            {"name": "owner", "type": "address"},
        ],
        "outputs": [{"name": "assets", "type": "uint256"}],
    },
]
