from __future__ import annotations

# Minimal ABIs for SparkLend (Aave v3-style) pool operations and data reads.
# These are intentionally small: enough for supply/withdraw/borrow/repay, market
# discovery, user positions, rewards claiming, and wrapped-native gateway flows.

POOL_ABI = [
    {
        "name": "supply",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "onBehalfOf", "type": "address"},
            {"name": "referralCode", "type": "uint16"},
        ],
        "outputs": [],
    },
    {
        "name": "withdraw",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "to", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "borrow",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "interestRateMode", "type": "uint256"},
            {"name": "referralCode", "type": "uint16"},
            {"name": "onBehalfOf", "type": "address"},
        ],
        "outputs": [],
    },
    {
        "name": "repay",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "interestRateMode", "type": "uint256"},
            {"name": "onBehalfOf", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "setUserUseReserveAsCollateral",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "useAsCollateral", "type": "bool"},
        ],
        "outputs": [],
    },
    {
        "name": "getUserAccountData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "user", "type": "address"}],
        "outputs": [
            {"name": "totalCollateralBase", "type": "uint256"},
            {"name": "totalDebtBase", "type": "uint256"},
            {"name": "availableBorrowsBase", "type": "uint256"},
            {"name": "currentLiquidationThreshold", "type": "uint256"},
            {"name": "ltv", "type": "uint256"},
            {"name": "healthFactor", "type": "uint256"},
        ],
    },
]


PROTOCOL_DATA_PROVIDER_ABI = [
    {
        "name": "getAllReservesTokens",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {
                "name": "",
                "type": "tuple[]",
                "components": [
                    {"name": "symbol", "type": "string"},
                    {"name": "tokenAddress", "type": "address"},
                ],
            }
        ],
    },
    {
        "name": "getReserveConfigurationData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "asset", "type": "address"}],
        "outputs": [
            {"name": "decimals", "type": "uint256"},
            {"name": "ltv", "type": "uint256"},
            {"name": "liquidationThreshold", "type": "uint256"},
            {"name": "liquidationBonus", "type": "uint256"},
            {"name": "reserveFactor", "type": "uint256"},
            {"name": "usageAsCollateralEnabled", "type": "bool"},
            {"name": "borrowingEnabled", "type": "bool"},
            {"name": "stableBorrowRateEnabled", "type": "bool"},
            {"name": "isActive", "type": "bool"},
            {"name": "isFrozen", "type": "bool"},
        ],
    },
    {
        "name": "getReserveCaps",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "asset", "type": "address"}],
        "outputs": [
            {"name": "borrowCap", "type": "uint256"},
            {"name": "supplyCap", "type": "uint256"},
        ],
    },
    {
        "name": "getReserveData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "asset", "type": "address"}],
        "outputs": [
            {"name": "unbacked", "type": "uint256"},
            {"name": "accruedToTreasuryScaled", "type": "uint256"},
            {"name": "totalAToken", "type": "uint256"},
            {"name": "totalStableDebt", "type": "uint256"},
            {"name": "totalVariableDebt", "type": "uint256"},
            {"name": "liquidityRate", "type": "uint256"},
            {"name": "variableBorrowRate", "type": "uint256"},
            {"name": "stableBorrowRate", "type": "uint256"},
            {"name": "averageStableBorrowRate", "type": "uint256"},
            {"name": "liquidityIndex", "type": "uint256"},
            {"name": "variableBorrowIndex", "type": "uint256"},
            {"name": "lastUpdateTimestamp", "type": "uint40"},
        ],
    },
    {
        "name": "getUserReserveData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "user", "type": "address"},
        ],
        "outputs": [
            {"name": "currentATokenBalance", "type": "uint256"},
            {"name": "currentStableDebt", "type": "uint256"},
            {"name": "currentVariableDebt", "type": "uint256"},
            {"name": "principalStableDebt", "type": "uint256"},
            {"name": "scaledVariableDebt", "type": "uint256"},
            {"name": "stableBorrowRate", "type": "uint256"},
            {"name": "liquidityRate", "type": "uint256"},
            {"name": "stableRateLastUpdated", "type": "uint40"},
            {"name": "usageAsCollateralEnabled", "type": "bool"},
        ],
    },
    {
        "name": "getReserveTokensAddresses",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "asset", "type": "address"}],
        "outputs": [
            {"name": "aTokenAddress", "type": "address"},
            {"name": "stableDebtTokenAddress", "type": "address"},
            {"name": "variableDebtTokenAddress", "type": "address"},
        ],
    },
]


REWARDS_CONTROLLER_ABI = [
    {
        "name": "getRewardsByAsset",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "asset", "type": "address"}],
        "outputs": [{"name": "", "type": "address[]"}],
    },
    {
        "name": "getRewardsData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "reward", "type": "address"},
        ],
        "outputs": [
            {"name": "index", "type": "uint104"},
            {"name": "emissionPerSecond", "type": "uint88"},
            {"name": "lastUpdateTimestamp", "type": "uint32"},
            {"name": "distributionEnd", "type": "uint32"},
        ],
    },
    {
        "name": "claimAllRewardsToSelf",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "assets", "type": "address[]"}],
        "outputs": [
            {"name": "rewardsList", "type": "address[]"},
            {"name": "claimedAmounts", "type": "uint256[]"},
        ],
    },
    {
        "name": "claimAllRewards",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "assets", "type": "address[]"},
            {"name": "to", "type": "address"},
        ],
        "outputs": [
            {"name": "rewardsList", "type": "address[]"},
            {"name": "claimedAmounts", "type": "uint256[]"},
        ],
    },
    {
        "name": "claimRewardsToSelf",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "assets", "type": "address[]"},
            {"name": "amount", "type": "uint256"},
            {"name": "reward", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "claimRewards",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "assets", "type": "address[]"},
            {"name": "amount", "type": "uint256"},
            {"name": "to", "type": "address"},
            {"name": "reward", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


WETH_GATEWAY_ABI = [
    {
        "type": "function",
        "stateMutability": "view",
        "name": "getWETHAddress",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "type": "function",
        "stateMutability": "payable",
        "name": "depositETH",
        "inputs": [
            {"name": "pool", "type": "address"},
            {"name": "onBehalfOf", "type": "address"},
            {"name": "referralCode", "type": "uint16"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "withdrawETH",
        "inputs": [
            {"name": "pool", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "to", "type": "address"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "stateMutability": "payable",
        "name": "repayETH",
        "inputs": [
            {"name": "pool", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "rateMode", "type": "uint256"},
            {"name": "onBehalfOf", "type": "address"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "borrowETH",
        "inputs": [
            {"name": "pool", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "interestRateMode", "type": "uint256"},
            {"name": "referralCode", "type": "uint16"},
        ],
        "outputs": [],
    },
]
