from __future__ import annotations

from wayfinder_paths.core.constants.erc4626_abi import ERC4626_ABI

# Minimal ABI surface for the canonical sUSDe staking vault (ERC-4626 + Ethena
# cooldown/unstake extensions).
ETHENA_SUSDE_VAULT_ABI = [
    *ERC4626_ABI,
    {
        "type": "function",
        "stateMutability": "view",
        "name": "cooldownDuration",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "cooldowns",
        "inputs": [{"name": "", "type": "address"}],
        "outputs": [
            {"name": "cooldownEnd", "type": "uint256"},
            {"name": "underlyingAmount", "type": "uint256"},
        ],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "cooldownShares",
        "inputs": [{"name": "shares", "type": "uint256"}],
        "outputs": [{"name": "assets", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "cooldownAssets",
        "inputs": [{"name": "assets", "type": "uint256"}],
        "outputs": [{"name": "shares", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "unstake",
        "inputs": [{"name": "receiver", "type": "address"}],
        "outputs": [{"name": "assets", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "getUnvestedAmount",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "lastDistributionTimestamp",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]
