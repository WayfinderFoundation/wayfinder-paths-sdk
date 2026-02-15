from __future__ import annotations

# Minimal Morpho Blue ABI: only the functions this SDK calls.
#
# Morpho Blue markets are defined by MarketParams:
# (loanToken, collateralToken, oracle, irm, lltv)
MARKET_PARAMS_COMPONENTS = [
    {"name": "loanToken", "type": "address"},
    {"name": "collateralToken", "type": "address"},
    {"name": "oracle", "type": "address"},
    {"name": "irm", "type": "address"},
    {"name": "lltv", "type": "uint256"},
]

MORPHO_BLUE_ABI = [
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "supply",
        "inputs": [
            {
                "name": "marketParams",
                "type": "tuple",
                "components": MARKET_PARAMS_COMPONENTS,
            },
            {"name": "assets", "type": "uint256"},
            {"name": "shares", "type": "uint256"},
            {"name": "onBehalf", "type": "address"},
            {"name": "data", "type": "bytes"},
        ],
        "outputs": [
            {"name": "assetsSupplied", "type": "uint256"},
            {"name": "sharesSupplied", "type": "uint256"},
        ],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "withdraw",
        "inputs": [
            {
                "name": "marketParams",
                "type": "tuple",
                "components": MARKET_PARAMS_COMPONENTS,
            },
            {"name": "assets", "type": "uint256"},
            {"name": "shares", "type": "uint256"},
            {"name": "onBehalf", "type": "address"},
            {"name": "receiver", "type": "address"},
        ],
        "outputs": [
            {"name": "assetsWithdrawn", "type": "uint256"},
            {"name": "sharesWithdrawn", "type": "uint256"},
        ],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "borrow",
        "inputs": [
            {
                "name": "marketParams",
                "type": "tuple",
                "components": MARKET_PARAMS_COMPONENTS,
            },
            {"name": "assets", "type": "uint256"},
            {"name": "shares", "type": "uint256"},
            {"name": "onBehalf", "type": "address"},
            {"name": "receiver", "type": "address"},
        ],
        "outputs": [
            {"name": "assetsBorrowed", "type": "uint256"},
            {"name": "sharesBorrowed", "type": "uint256"},
        ],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "repay",
        "inputs": [
            {
                "name": "marketParams",
                "type": "tuple",
                "components": MARKET_PARAMS_COMPONENTS,
            },
            {"name": "assets", "type": "uint256"},
            {"name": "shares", "type": "uint256"},
            {"name": "onBehalf", "type": "address"},
            {"name": "data", "type": "bytes"},
        ],
        "outputs": [
            {"name": "assetsRepaid", "type": "uint256"},
            {"name": "sharesRepaid", "type": "uint256"},
        ],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "supplyCollateral",
        "inputs": [
            {
                "name": "marketParams",
                "type": "tuple",
                "components": MARKET_PARAMS_COMPONENTS,
            },
            {"name": "assets", "type": "uint256"},
            {"name": "onBehalf", "type": "address"},
            {"name": "data", "type": "bytes"},
        ],
        "outputs": [{"name": "assetsSupplied", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "withdrawCollateral",
        "inputs": [
            {
                "name": "marketParams",
                "type": "tuple",
                "components": MARKET_PARAMS_COMPONENTS,
            },
            {"name": "assets", "type": "uint256"},
            {"name": "onBehalf", "type": "address"},
            {"name": "receiver", "type": "address"},
        ],
        "outputs": [{"name": "assetsWithdrawn", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "position",
        "inputs": [
            {"name": "marketId", "type": "bytes32"},
            {"name": "user", "type": "address"},
        ],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "supplyShares", "type": "uint256"},
                    {"name": "borrowShares", "type": "uint256"},
                    {"name": "collateral", "type": "uint256"},
                ],
            }
        ],
    },
]

