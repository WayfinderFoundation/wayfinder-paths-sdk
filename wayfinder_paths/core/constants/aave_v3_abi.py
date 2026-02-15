from __future__ import annotations

# Minimal ABIs for Aave v3 pool operations + UI periphery helpers.

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
]


UI_POOL_DATA_PROVIDER_ABI = [
    {
        "name": "getReservesData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "provider", "type": "address"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple[]",
                "components": [
                    {"name": "underlyingAsset", "type": "address"},
                    {"name": "name", "type": "string"},
                    {"name": "symbol", "type": "string"},
                    {"name": "decimals", "type": "uint256"},
                    {"name": "baseLTVasCollateral", "type": "uint256"},
                    {"name": "reserveLiquidationThreshold", "type": "uint256"},
                    {"name": "reserveLiquidationBonus", "type": "uint256"},
                    {"name": "reserveFactor", "type": "uint256"},
                    {"name": "usageAsCollateralEnabled", "type": "bool"},
                    {"name": "borrowingEnabled", "type": "bool"},
                    {"name": "isActive", "type": "bool"},
                    {"name": "isFrozen", "type": "bool"},
                    {"name": "liquidityIndex", "type": "uint128"},
                    {"name": "variableBorrowIndex", "type": "uint128"},
                    {"name": "liquidityRate", "type": "uint128"},
                    {"name": "variableBorrowRate", "type": "uint128"},
                    {"name": "lastUpdateTimestamp", "type": "uint40"},
                    {"name": "aTokenAddress", "type": "address"},
                    {"name": "variableDebtTokenAddress", "type": "address"},
                    {"name": "interestRateStrategyAddress", "type": "address"},
                    {"name": "availableLiquidity", "type": "uint256"},
                    {"name": "totalScaledVariableDebt", "type": "uint256"},
                    {"name": "priceInMarketReferenceCurrency", "type": "uint256"},
                    {"name": "priceOracle", "type": "address"},
                    {"name": "variableRateSlope1", "type": "uint256"},
                    {"name": "variableRateSlope2", "type": "uint256"},
                    {"name": "baseVariableBorrowRate", "type": "uint256"},
                    {"name": "optimalUsageRatio", "type": "uint256"},
                    {"name": "isPaused", "type": "bool"},
                    {"name": "isSiloedBorrowing", "type": "bool"},
                    {"name": "accruedToTreasury", "type": "uint128"},
                    {"name": "unbacked", "type": "uint128"},
                    {"name": "flashLoanEnabled", "type": "bool"},
                    {"name": "debtCeiling", "type": "uint256"},
                    {"name": "debtCeilingDecimals", "type": "uint256"},
                    {"name": "borrowCap", "type": "uint256"},
                    {"name": "supplyCap", "type": "uint256"},
                    {"name": "borrowableInIsolation", "type": "bool"},
                    {"name": "virtualUnderlyingBalance", "type": "uint128"},
                    {"name": "isolationModeTotalDebt", "type": "uint128"},
                ],
            },
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "marketReferenceCurrencyUnit", "type": "uint256"},
                    {"name": "marketReferenceCurrencyPriceInUsd", "type": "int256"},
                    {"name": "networkBaseTokenPriceInUsd", "type": "int256"},
                    {"name": "networkBaseTokenPriceDecimals", "type": "uint8"},
                ],
            },
        ],
    },
    {
        "name": "getUserReservesData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "provider", "type": "address"},
            {"name": "user", "type": "address"},
        ],
        "outputs": [
            {
                "name": "",
                "type": "tuple[]",
                "components": [
                    {"name": "underlyingAsset", "type": "address"},
                    {"name": "scaledATokenBalance", "type": "uint256"},
                    {"name": "usageAsCollateralEnabledOnUser", "type": "bool"},
                    {"name": "scaledVariableDebt", "type": "uint256"},
                ],
            },
            {"name": "", "type": "uint8"},
        ],
    },
]

UI_POOL_RESERVE_KEYS = [
    c.get("name")
    for c in (
        UI_POOL_DATA_PROVIDER_ABI[0].get("outputs", [{}])[0].get("components") or []
    )
    if c.get("name")
]


UI_INCENTIVE_DATA_PROVIDER_V3_ABI = [
    {
        "name": "getReservesIncentivesData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "provider", "type": "address"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple[]",
                "components": [
                    {"name": "underlyingAsset", "type": "address"},
                    {
                        "name": "aIncentiveData",
                        "type": "tuple",
                        "components": [
                            {"name": "tokenAddress", "type": "address"},
                            {"name": "incentiveControllerAddress", "type": "address"},
                            {
                                "name": "rewardsTokenInformation",
                                "type": "tuple[]",
                                "components": [
                                    {"name": "rewardTokenSymbol", "type": "string"},
                                    {
                                        "name": "rewardTokenAddress",
                                        "type": "address",
                                    },
                                    {"name": "rewardOracleAddress", "type": "address"},
                                    {"name": "emissionPerSecond", "type": "uint256"},
                                    {
                                        "name": "incentivesLastUpdateTimestamp",
                                        "type": "uint256",
                                    },
                                    {
                                        "name": "tokenIncentivesIndex",
                                        "type": "uint256",
                                    },
                                    {"name": "emissionEndTimestamp", "type": "uint256"},
                                    {"name": "rewardPriceFeed", "type": "int256"},
                                    {"name": "rewardTokenDecimals", "type": "uint8"},
                                    {"name": "precision", "type": "uint8"},
                                    {"name": "priceFeedDecimals", "type": "uint8"},
                                ],
                            },
                        ],
                    },
                    {
                        "name": "vIncentiveData",
                        "type": "tuple",
                        "components": [
                            {"name": "tokenAddress", "type": "address"},
                            {"name": "incentiveControllerAddress", "type": "address"},
                            {
                                "name": "rewardsTokenInformation",
                                "type": "tuple[]",
                                "components": [
                                    {"name": "rewardTokenSymbol", "type": "string"},
                                    {
                                        "name": "rewardTokenAddress",
                                        "type": "address",
                                    },
                                    {"name": "rewardOracleAddress", "type": "address"},
                                    {"name": "emissionPerSecond", "type": "uint256"},
                                    {
                                        "name": "incentivesLastUpdateTimestamp",
                                        "type": "uint256",
                                    },
                                    {
                                        "name": "tokenIncentivesIndex",
                                        "type": "uint256",
                                    },
                                    {"name": "emissionEndTimestamp", "type": "uint256"},
                                    {"name": "rewardPriceFeed", "type": "int256"},
                                    {"name": "rewardTokenDecimals", "type": "uint8"},
                                    {"name": "precision", "type": "uint8"},
                                    {"name": "priceFeedDecimals", "type": "uint8"},
                                ],
                            },
                        ],
                    },
                ],
            }
        ],
    },
    {
        "name": "getUserReservesIncentivesData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "provider", "type": "address"},
            {"name": "user", "type": "address"},
        ],
        "outputs": [
            {
                "name": "",
                "type": "tuple[]",
                "components": [
                    {"name": "underlyingAsset", "type": "address"},
                    {
                        "name": "aTokenIncentivesUserData",
                        "type": "tuple",
                        "components": [
                            {"name": "tokenAddress", "type": "address"},
                            {"name": "incentiveControllerAddress", "type": "address"},
                            {
                                "name": "userRewardsInformation",
                                "type": "tuple[]",
                                "components": [
                                    {"name": "rewardTokenSymbol", "type": "string"},
                                    {"name": "rewardOracleAddress", "type": "address"},
                                    {
                                        "name": "rewardTokenAddress",
                                        "type": "address",
                                    },
                                    {
                                        "name": "userUnclaimedRewards",
                                        "type": "uint256",
                                    },
                                    {
                                        "name": "tokenIncentivesUserIndex",
                                        "type": "uint256",
                                    },
                                    {"name": "rewardPriceFeed", "type": "int256"},
                                    {"name": "priceFeedDecimals", "type": "uint8"},
                                    {"name": "rewardTokenDecimals", "type": "uint8"},
                                ],
                            },
                        ],
                    },
                    {
                        "name": "vTokenIncentivesUserData",
                        "type": "tuple",
                        "components": [
                            {"name": "tokenAddress", "type": "address"},
                            {"name": "incentiveControllerAddress", "type": "address"},
                            {
                                "name": "userRewardsInformation",
                                "type": "tuple[]",
                                "components": [
                                    {"name": "rewardTokenSymbol", "type": "string"},
                                    {"name": "rewardOracleAddress", "type": "address"},
                                    {
                                        "name": "rewardTokenAddress",
                                        "type": "address",
                                    },
                                    {
                                        "name": "userUnclaimedRewards",
                                        "type": "uint256",
                                    },
                                    {
                                        "name": "tokenIncentivesUserIndex",
                                        "type": "uint256",
                                    },
                                    {"name": "rewardPriceFeed", "type": "int256"},
                                    {"name": "priceFeedDecimals", "type": "uint8"},
                                    {"name": "rewardTokenDecimals", "type": "uint8"},
                                ],
                            },
                        ],
                    },
                ],
            }
        ],
    },
]


REWARDS_CONTROLLER_ABI = [
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


WRAPPED_TOKEN_GATEWAY_V3_ABI = [
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


WETH_ABI = [
    {
        "type": "function",
        "stateMutability": "payable",
        "name": "deposit",
        "inputs": [],
        "outputs": [],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "withdraw",
        "inputs": [{"name": "amount", "type": "uint256"}],
        "outputs": [],
    },
]


CHAINLINK_AGGREGATOR_ABI = [
    {
        "type": "function",
        "stateMutability": "view",
        "name": "decimals",
        "inputs": [],
        "outputs": [{"type": "uint8"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "latestAnswer",
        "inputs": [],
        "outputs": [{"type": "int256"}],
    },
]
